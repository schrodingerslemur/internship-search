"""Notification channel credentials, configurable from the dashboard.

Originally these lived only in environment variables, which meant configuring
them twice -- once in the GitHub Actions secrets that run the search, once in
the web host that shows the dashboard -- and the dashboard reporting a channel
as unconfigured even though digests were being sent perfectly well.

Stored in the database instead, which both halves already share. Environment
variables remain a fallback, so an existing deployment keeps working and a
value can still be pinned by the host.

The credentials sit in the same database as the job data and are no better
protected than the environment variables they replace. That is the honest
trade for being able to set them up without a redeploy.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.appconfig import get_config, set_config

#: DB key -> the settings attribute it overrides.
FIELDS: dict[str, str] = {
    "smtp_host": "smtp_host",
    "smtp_port": "smtp_port",
    "smtp_user": "smtp_user",
    "smtp_password": "smtp_password",
    "email_from": "email_from",
    "email_to": "email_to",
    "telegram_bot_token": "telegram_bot_token",
    "telegram_chat_id": "telegram_chat_id",
}

#: Never echoed back to the browser; a blank submission means "keep".
SECRET_FIELDS: frozenset[str] = frozenset({"smtp_password", "telegram_bot_token"})


@dataclass
class ChannelConfig:
    """Resolved channel credentials: database first, environment underneath."""

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    email_from: str | None = None
    email_to: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    @property
    def email_sender(self) -> str | None:
        return self.email_from or self.smtp_user

    @property
    def email_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_password and self.email_sender)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def ready_for(self, provider: str) -> bool:
        if provider == "email":
            return self.email_ready
        if provider == "telegram":
            return self.telegram_ready
        return True  # console and file need nothing

    def missing_for(self, provider: str) -> list[str]:
        """Which fields still need filling in, in human terms."""
        if provider == "email":
            labels = {
                "smtp_host": "SMTP host",
                "smtp_password": "SMTP password",
                "email_sender": "From address",
            }
            return [
                label
                for key, label in labels.items()
                if not getattr(self, key, None)
            ]
        if provider == "telegram":
            return [
                label
                for key, label in (("telegram_bot_token", "Bot token"),
                                   ("telegram_chat_id", "Chat ID"))
                if not getattr(self, key, None)
            ]
        return []


def load(session: Session) -> ChannelConfig:
    settings = get_settings()
    values: dict[str, object] = {
        "smtp_starttls": settings.smtp_starttls,
        "smtp_port": settings.smtp_port,
    }
    for key, attr in FIELDS.items():
        stored = get_config(session, f"notify.{key}")
        values[key] = stored if stored else getattr(settings, attr, None)

    try:
        values["smtp_port"] = int(values.get("smtp_port") or 587)
    except (TypeError, ValueError):
        values["smtp_port"] = 587

    return ChannelConfig(**{k: v for k, v in values.items() if k in ChannelConfig.__annotations__})


def save(session: Session, updates: dict[str, str | None]) -> None:
    """Persist submitted values.

    A blank secret means "leave it alone", so the form can show a placeholder
    instead of the real password and still be safe to submit. A blank
    non-secret is a real value: it is how a field gets cleared.
    """
    for key, value in updates.items():
        if key not in FIELDS:
            continue
        cleaned = (value or "").strip()
        if key in SECRET_FIELDS and not cleaned:
            continue
        set_config(session, f"notify.{key}", cleaned)
