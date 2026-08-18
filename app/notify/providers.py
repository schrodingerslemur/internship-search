"""Concrete notification providers.

Telegram is the shipped phone channel. Console and file providers exist so the
notification path is fully testable and usable before any credentials are set.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.config import DATA_DIR, get_settings
from app.logging_setup import get_logger
from app.models.base import utcnow
from app.notify.base import NotificationMessage, NotificationProvider, SendResult

log = get_logger("notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
#: Telegram hard-limits a message to 4096 characters.
TELEGRAM_LIMIT = 4000


class TelegramProvider(NotificationProvider):
    """Sends to a Telegram chat via the Bot API."""

    name = "telegram"
    display_name = "Telegram"
    required_config = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        settings = get_settings()
        self.token = token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id

    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, message: NotificationMessage) -> SendResult:
        if not self.is_configured():
            return SendResult(False, self.name, error="TELEGRAM_BOT_TOKEN/CHAT_ID not configured")

        body = message.rich_text or message.text
        if len(body) > TELEGRAM_LIMIT:
            body = body[: TELEGRAM_LIMIT - 20].rstrip() + "\n…"

        payload = {
            "chat_id": self.chat_id,
            "text": body,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(TELEGRAM_API.format(token=self.token), json=payload)
        except httpx.HTTPError as exc:
            return SendResult(False, self.name, error=f"transport error: {exc}")

        if response.status_code != 200:
            # Retry once as plain text: a stray '<' in a job title can make
            # Telegram reject otherwise valid HTML.
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    fallback = await client.post(
                        TELEGRAM_API.format(token=self.token),
                        json={
                            "chat_id": self.chat_id,
                            "text": message.text[:TELEGRAM_LIMIT],
                            "disable_web_page_preview": True,
                        },
                    )
                if fallback.status_code == 200:
                    return SendResult(True, self.name, detail="sent as plain text")
            except httpx.HTTPError:
                pass
            return SendResult(False, self.name, error=f"HTTP {response.status_code}: {response.text[:200]}")

        return SendResult(True, self.name)


class ConsoleProvider(NotificationProvider):
    """Logs the digest. Useful for dry runs and local development."""

    name = "console"
    display_name = "Console (log only)"

    async def send(self, message: NotificationMessage) -> SendResult:
        log.info("notify.console", subject=message.subject, body=message.text)
        return SendResult(True, self.name)


class FileProvider(NotificationProvider):
    """Appends notifications to a JSONL file, so nothing is lost unconfigured."""

    name = "file"
    display_name = "File (data/notifications.jsonl)"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (DATA_DIR / "notifications.jsonl")

    async def send(self, message: NotificationMessage) -> SendResult:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": utcnow().isoformat(),
                            "subject": message.subject,
                            "text": message.text,
                            "job_ids": message.job_ids,
                        }
                    )
                    + "\n"
                )
        except OSError as exc:
            return SendResult(False, self.name, error=str(exc))
        return SendResult(True, self.name)


#: Everything the notification engine can dispatch to.
PROVIDERS: dict[str, type[NotificationProvider]] = {
    TelegramProvider.name: TelegramProvider,
    ConsoleProvider.name: ConsoleProvider,
    FileProvider.name: FileProvider,
}


def get_provider(name: str) -> NotificationProvider:
    """Instantiate a provider, falling back to the file provider.

    Falling back rather than failing means a digest is never lost just because
    Telegram is not set up yet.
    """
    cls = PROVIDERS.get(name)
    if cls is None:
        log.warning("notify.unknown_provider", provider=name)
        return FileProvider()
    provider = cls()
    if not provider.is_configured():
        log.warning("notify.provider_unconfigured", provider=name)
        return FileProvider()
    return provider


def provider_catalog() -> list[dict[str, object]]:
    out = []
    for name, cls in PROVIDERS.items():
        instance = cls()
        out.append(
            {
                "name": name,
                "display_name": cls.display_name,
                "configured": instance.is_configured(),
                "required_config": list(cls.required_config),
            }
        )
    return out
