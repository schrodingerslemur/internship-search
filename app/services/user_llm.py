"""Bring your own model: each user's provider, and the client built from it.

The rule this module exists to enforce: **a user's work runs on that user's
model, never on the deployment's.** The deployment's own credentials
(``CLAUDE_CODE_OAUTH_TOKEN``, ``LLM_API_KEY``) belong to its owner and are
used for the owner's account alone, plus the shared job enrichment, which
costs the same however many people sign up.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, UserLlmSetting, utcnow
from app.services import secretbox


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    #: OpenAI-compatible base URL; None for the Claude API (its own SDK) or
    #: for "other", where the user supplies one.
    base_url: str | None
    default_model: str
    #: Where to make a key, linked from the page.
    key_url: str
    blurb: str
    #: Good enough for the general web agent (Lever, Workday, ...), which runs
    #: up to thirty long turns per application.
    strong_agent: bool = False


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        Provider(
            "groq",
            "Groq",
            "https://api.groq.com/openai/v1",
            "openai/gpt-oss-120b",
            "https://console.groq.com/keys",
            "Free, no card. Sign in with Google, click Create API Key, paste it here.",
        ),
        Provider(
            "anthropic",
            "Claude (Anthropic API)",
            None,
            "claude-opus-5-5",
            "https://console.anthropic.com/settings/keys",
            "Pay as you go with your own Anthropic account. Best for applying on Lever, "
            "Workday and company sites.",
            strong_agent=True,
        ),
        Provider(
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-2.5-flash",
            "https://aistudio.google.com/apikey",
            "Free tier with a Google account. Click Create API key in AI Studio.",
        ),
        Provider(
            "openai_compatible",
            "Other (OpenAI-compatible)",
            None,
            "",
            "",
            "OpenAI, OpenRouter, Together or anything that speaks the OpenAI chat API: "
            "give its base URL and model.",
        ),
    )
}


# ------------------------------------------------------------- who is who


def owner_id(session: Session) -> int | None:
    """The deployment owner's user id: OWNER_EMAIL, else the first account."""
    email = (get_settings().owner_email or "").strip().lower()
    if email:
        found = session.scalar(select(User.id).where(User.email == email))
        if found is not None:
            return found
    return session.scalar(select(User.id).order_by(User.id).limit(1))


def is_owner(session: Session, user_id: int) -> bool:
    return owner_id(session) == user_id


# --------------------------------------------------------- saved settings


def get(session: Session, user_id: int) -> UserLlmSetting | None:
    return session.scalar(select(UserLlmSetting).where(UserLlmSetting.user_id == user_id))


class SetupError(ValueError):
    """What the user typed cannot be saved; the message says why."""


def save(
    session: Session,
    user: User,
    *,
    provider: str,
    api_key: str,
    model: str = "",
    base_url: str = "",
) -> UserLlmSetting:
    """Store (or replace) this user's model. An empty key keeps the saved one."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise SetupError("Pick a provider.")
    if not secretbox.available():
        raise SetupError(
            "This server cannot store keys yet (its SECRETS_KEY is not set). "
            "Ask whoever runs it to set one."
        )
    model = (model or "").strip() or spec.default_model
    base_url = (base_url or "").strip().rstrip("/")
    if provider == "openai_compatible":
        if not base_url.startswith("https://"):
            raise SetupError("Give the provider's base URL, starting with https://.")
        if not model:
            raise SetupError("Give the model name the provider expects.")
    else:
        base_url = ""
    api_key = (api_key or "").strip()
    row = get(session, user.id)
    if not api_key and (row is None or row.provider != provider):
        raise SetupError("Paste your API key.")
    if row is None:
        row = UserLlmSetting(user_id=user.id, provider=provider, model=model, key_sealed="")
        session.add(row)
    row.provider, row.model, row.base_url = provider, model[:120], base_url or None
    if api_key:
        row.key_sealed = secretbox.seal(api_key)
        row.key_hint = api_key[-4:]
    row.checked_ok, row.check_detail, row.checked_at = None, None, None
    session.flush()
    return row


def forget(session: Session, user_id: int) -> None:
    row = get(session, user_id)
    if row is not None:
        session.delete(row)
        session.flush()


# ---------------------------------------------------------- the clients


def client_from(row: UserLlmSetting | None, *, max_calls: int = 60):
    """A client with ``complete_json`` and ``answer_application_questions``,
    or None when there is no usable saved model."""
    if row is None:
        return None
    key = secretbox.unseal(row.key_sealed)
    if not key:
        return None
    if row.provider == "anthropic":
        from app.pipeline.anthropic_client import AnthropicClient

        return AnthropicClient(key, model=row.model, max_calls=max_calls)
    spec = PROVIDERS.get(row.provider)
    base_url = row.base_url or (spec.base_url if spec else None)
    if not base_url:
        return None
    from app.pipeline.llm import LlmClient

    return LlmClient(base_url=base_url, model=row.model, api_key=key, max_calls=max_calls)


def check(session: Session, row: UserLlmSetting) -> tuple[bool, str]:
    """Make one tiny call with the saved key and record whether it worked."""
    client = client_from(row, max_calls=3)
    if client is None:
        ok, detail = False, "The saved key could not be read; paste it again."
    else:
        try:
            ok, detail = client.health()
        finally:
            client.close()
    row.checked_ok, row.check_detail, row.checked_at = ok, detail[:300], utcnow()
    session.flush()
    return ok, detail


def answerer_for(session: Session, user_id: int, *, max_calls: int = 60):
    """What thinks for this user in the worker.

    The owner gets the deployment's writers (their Claude subscription, then
    the deployment's API model). Everyone else gets their own model or
    nothing: a ChainedAnswerer with no writers, which leaves every question it
    would have written for the user to answer.
    """
    from app.apply.agent_llm import ChainedAnswerer, apply_answerer

    if is_owner(session, user_id):
        return apply_answerer(max_calls=max_calls)
    client = client_from(get(session, user_id), max_calls=max_calls)
    return ChainedAnswerer([client] if client is not None else [])
