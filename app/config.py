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
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5"
    llm_enabled: bool = False
    llm_max_calls_per_run: int = 120

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
    def llm_available(self) -> bool:
        """LLM stages run only when explicitly enabled AND a key is present."""
        return bool(self.llm_enabled and self.anthropic_api_key)

    @property
    def telegram_available(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def email_sender(self) -> str | None:
        return self.email_from or self.smtp_user

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
