"""Application configuration, loaded from environment / .env.

Every job-source credential is optional. A source whose credentials are absent
reports itself as unconfigured rather than failing the run -- see
`Settings.source_credentials`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RESUME_DIR = DATA_DIR / "resumes"
CACHE_DIR = DATA_DIR / "cache"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Core ----
    database_url: str = f"sqlite:///{(DATA_DIR / 'internship.db').as_posix()}"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    timezone: str = "America/New_York"
    #: Public URL of the deployed dashboard, used for links inside digests.
    #: Falls back to the local host/port when unset.
    public_base_url: str | None = None

    # ---- Notifications ----
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # ---- Email (SMTP) ----
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    #: Defaults to ``smtp_user`` when unset -- most providers require the
    #: envelope sender to be the authenticated account anyway.
    email_from: str | None = None
    email_to: str | None = None

    # ---- LLM ----
    #: Any OpenAI-compatible chat-completions endpoint. That covers Ollama
    #: running locally (free, private, no key), Groq and OpenRouter's free
    #: tiers, and the paid providers, through one code path -- switching is two
    #: lines of .env rather than a rewrite.
    #:
    #:   Ollama      http://localhost:11434/v1        (no key needed)
    #:   Groq        https://api.groq.com/openai/v1
    #:   OpenRouter  https://openrouter.ai/api/v1
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str | None = None
    llm_model: str = "qwen2.5:7b"
    llm_enabled: bool = False
    llm_max_calls_per_run: int = 120
    #: Seconds to wait on one completion. A local model on CPU is slow, and a
    #: backfill that dies on the first timeout is worse than one that crawls.
    llm_timeout_seconds: float = 120.0
    #: Retained so an existing Anthropic key keeps working without editing .env.
    anthropic_api_key: str | None = None

    # ---- Source credentials ----
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    jsearch_api_key: str | None = None
    serpapi_key: str | None = None
    usajobs_api_key: str | None = None
    usajobs_email: str | None = None
    jooble_api_key: str | None = None

    # ---- Scheduler ----
    scheduler_enabled: bool = True
    morning_digest_enabled: bool = True
    morning_digest_time: str = "08:00"
    afternoon_digest_enabled: bool = True
    afternoon_digest_time: str = "16:00"
    digest_schedule: Literal["all", "weekdays"] = "all"

    # ---- HTTP ----
    http_timeout_seconds: float = 25.0
    http_max_retries: int = 3
    http_max_concurrency: int = 12
    http_rate_limit_delay: float = 0.35
    http_cache_ttl_seconds: int = 900
    user_agent: str = Field(default="internship-search-agent/0.1 (personal job search)")

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        """Accept a hosted provider's connection string verbatim.

        Neon, Supabase and Render all hand out ``postgresql://`` (or the older
        ``postgres://``) URLs, which SQLAlchemy rejects for lack of a driver.
        Rewriting here means the value can be pasted straight from the
        provider's dashboard into a secret without silent breakage.
        """
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # ---- Derived helpers ----

    @property
    def llm_key(self) -> str | None:
        """The credential to send, if any.

        A locally-hosted model needs none, so an absent key is not an error --
        it is the normal case for Ollama, and requiring one would rule out the
        only genuinely free option.
        """
        return self.llm_api_key or self.anthropic_api_key

    @property
    def llm_is_local(self) -> bool:
        host = (self.llm_base_url or "").lower()
        return "localhost" in host or "127.0.0.1" in host or "host.docker.internal" in host

    @property
    def llm_available(self) -> bool:
        """LLM stages run only when explicitly enabled and reachable.

        A remote endpoint needs a key; a local one does not.
        """
        if not self.llm_enabled or not self.llm_base_url:
            return False
        return self.llm_is_local or bool(self.llm_key)

    @property
    def telegram_available(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def email_sender(self) -> str | None:
        return self.email_from or self.smtp_user

    @property
    def smtp_configured(self) -> bool:
        """Whether this deployment can send mail at all.

        Deliberately independent of any particular recipient: the
        forgot-password page must answer identically for an address that has
        an account and one that does not, so it cannot be used to find out
        who has one.
        """
        return bool(self.smtp_host and self.smtp_password and self.email_sender)

    @property
    def email_available(self) -> bool:
        return bool(self.smtp_host and self.smtp_password and self.email_sender and self.email_to)

    def source_credentials(self) -> dict[str, dict[str, str | None]]:
        """Credential bundles keyed by source name.

        A source is considered *configured* when every value in its bundle is
        truthy. Sources absent from this mapping need no credentials at all.
        """
        return {
            "adzuna": {"app_id": self.adzuna_app_id, "app_key": self.adzuna_app_key},
            "jsearch": {"api_key": self.jsearch_api_key},
            "serpapi_google_jobs": {"api_key": self.serpapi_key},
            "usajobs": {"api_key": self.usajobs_api_key, "email": self.usajobs_email},
            "jooble": {"api_key": self.jooble_api_key},
        }

    def is_source_configured(self, name: str) -> bool:
        bundle = self.source_credentials().get(name)
        if bundle is None:
            return True  # no credentials required
        return all(bool(v) for v in bundle.values())


@lru_cache
def get_settings() -> Settings:
    return Settings()


def ensure_dirs() -> None:
    for d in (DATA_DIR, RESUME_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
