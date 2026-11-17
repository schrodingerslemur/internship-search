"""A user's own Anthropic API key, behind the same two methods as LlmClient.

Everything that thinks for a user calls ``complete_json(system, prompt)`` or
``answer_application_questions(...)``; this is the Claude API version of both,
through the official SDK. Like LlmClient, a failure returns None and never
raises: the deterministic parts of the app keep working without a model.
"""

from __future__ import annotations

import anthropic

from app.logging_setup import get_logger
from app.pipeline.llm import APPLY_SYSTEM, _apply_prompt, _parse_json

log = get_logger("anthropic_client")

DEFAULT_MODEL = "claude-opus-5-5"

#: Models that take server-side refusal fallbacks ("default" routing): a
#: declined request is re-run on a fallback model inside the same call.
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    """``complete_json`` and ``answer_application_questions`` on the Claude API."""

    def __init__(
        self, api_key: str, *, model: str = DEFAULT_MODEL, max_calls: int = 60, timeout: float = 180
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.max_calls = max_calls
        self.calls = 0
        self.enabled = bool(api_key)
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    @property
    def budget_left(self) -> int:
        return max(0, self.max_calls - self.calls)

    def _create(self, system: str, prompt: str, max_tokens: int):
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.model in FALLBACK_MODELS:
            return self._client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        return self._client.messages.create(**kwargs)

    def complete_json(self, system: str, prompt: str, *, max_tokens: int = 16000, **_) -> dict | None:
        if not self.enabled or self.budget_left <= 0:
            return None
        self.calls += 1
        try:
            response = self._create(system, prompt, max_tokens)
        except anthropic.AuthenticationError:
            log.warning("anthropic.bad_key", model=self.model)
            return None
        except anthropic.NotFoundError:
            log.warning("anthropic.unknown_model", model=self.model)
            return None
        except anthropic.RateLimitError:
            log.warning("anthropic.rate_limited", model=self.model)
            return None
        except anthropic.APIStatusError as exc:
            log.warning("anthropic.http_error", status=exc.status_code, model=self.model)
            return None
        except anthropic.APIConnectionError:
            log.warning("anthropic.unreachable", model=self.model)
            return None
        if response.stop_reason == "refusal":
            log.warning("anthropic.refused", model=self.model)
            return None
        text = "".join(block.text for block in response.content if block.type == "text")
        return _parse_json(text)

    def answer_application_questions(
        self, *, facts: str, resume: str, posting: str, questions: list[dict]
    ) -> dict[str, dict]:
        from app.apply.agent_llm import parse_answers

        return parse_answers(
            self.complete_json(APPLY_SYSTEM, _apply_prompt(facts, resume, posting, questions))
        )

    def health(self) -> tuple[bool, str]:
        """One tiny call: does this key work with this model?"""
        if not self.enabled:
            return False, "No key."
        try:
            response = self._create('Reply with JSON only: {"ok": true}', "Say ok.", 256)
        except anthropic.AuthenticationError:
            return False, "Anthropic rejected this key."
        except anthropic.PermissionDeniedError:
            return False, "This key is not allowed to use that model."
        except anthropic.NotFoundError:
            return False, f"Anthropic does not know the model {self.model!r}."
        except anthropic.RateLimitError:
            return False, "The key works but is rate-limited or out of credit right now."
        except anthropic.APIStatusError as exc:
            return False, f"Anthropic answered HTTP {exc.status_code}."
        except anthropic.APIConnectionError:
            return False, "Could not reach Anthropic."
        text = "".join(b.text for b in response.content if b.type == "text")
        if _parse_json(text) is None:
            return False, f"{self.model} did not return usable JSON."
        return True, f"{self.model} on the Claude API"

    def close(self) -> None:
        self._client.close()
