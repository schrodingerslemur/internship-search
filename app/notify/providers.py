"""Concrete notification providers.

Email (SMTP) and Telegram are the shipped delivery channels. Console and file
providers exist so the notification path is fully testable and usable before
any credentials are set.
"""

from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
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

    def __init__(self, token: str | None = None, chat_id: str | None = None, config=None) -> None:
        source = config or get_settings()
        self.token = token or source.telegram_bot_token
        self.chat_id = chat_id or source.telegram_chat_id

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


class EmailProvider(NotificationProvider):
    """Sends the digest as a multipart HTML email over SMTP.

    Plain SMTP rather than a vendor API, so any provider works: a Gmail
    account with an app password, a university relay, Fastmail, SES. The
    message is multipart/alternative, so a client that refuses HTML still
    shows the full text digest.
    """

    name = "email"
    display_name = "Email (SMTP)"
    required_config = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")

    def __init__(self, recipient: str | None = None, config=None) -> None:
        # ``config`` is the resolved channel configuration (database over
        # environment). Falling back to settings keeps every existing caller
        # -- and a deployment that configures nothing in the UI -- working.
        source = config or get_settings()
        self.host = source.smtp_host
        self.port = source.smtp_port
        self.user = source.smtp_user
        self.password = source.smtp_password
        self.starttls = getattr(source, "smtp_starttls", True)
        self.sender = source.email_sender
        # Each account's digest goes to that account's own address; EMAIL_TO is
        # the deployment-wide fallback for a user who has not set one.
        raw = recipient or source.email_to or ""
        # A comma-separated list is accepted so a digest can go to two inboxes.
        self.recipients = [r.strip() for r in raw.split(",") if r.strip()]

    def is_configured(self) -> bool:
        return bool(self.host and self.password and self.sender and self.recipients)

    def _build(self, message: NotificationMessage) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = message.subject or "Internship Search digest"
        mail["From"] = formataddr(("Internship Search", self.sender or ""))
        mail["To"] = ", ".join(self.recipients)
        mail["Date"] = formatdate(localtime=True)
        mail["Message-ID"] = make_msgid(domain="internship-search.local")
        # Groups digests into one Gmail thread instead of flooding the inbox
        # with a separate conversation twice a day.
        mail["References"] = "<internship-search-digest@internship-search.local>"
        mail.set_content(message.text)
        if message.html:
            mail.add_alternative(message.html, subtype="html")
        return mail

    def _send_sync(self, mail: EmailMessage) -> None:
        """Blocking SMTP conversation, run off the event loop by :meth:`send`."""
        context = ssl.create_default_context()
        timeout = 30
        # Port 465 is implicit TLS; everything else negotiates STARTTLS.
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=timeout, context=context) as server:
                if self.user:
                    server.login(self.user, self.password or "")
                server.send_message(mail)
            return

        with smtplib.SMTP(self.host, self.port, timeout=timeout) as server:
            server.ehlo()
            if self.starttls:
                server.starttls(context=context)
                server.ehlo()
            if self.user:
                server.login(self.user, self.password or "")
            server.send_message(mail)

    async def send(self, message: NotificationMessage) -> SendResult:
        if not self.is_configured():
            return SendResult(False, self.name, error="SMTP_HOST/USER/PASSWORD/EMAIL_TO not configured")

        mail = self._build(message)
        try:
            await asyncio.to_thread(self._send_sync, mail)
        except smtplib.SMTPAuthenticationError as exc:
            # Overwhelmingly the failure mode: a normal password was used
            # where the provider requires an app password.
            return SendResult(
                False,
                self.name,
                error=f"SMTP auth rejected ({exc.smtp_code}); an app password is usually required",
            )
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(False, self.name, error=f"{type(exc).__name__}: {exc}")

        return SendResult(True, self.name, detail=f"sent to {len(self.recipients)} recipient(s)")


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
    EmailProvider.name: EmailProvider,
    ConsoleProvider.name: ConsoleProvider,
    FileProvider.name: FileProvider,
}


#: Tried in order when the configured provider cannot send. Email first: a
#: deployment whose SMTP credentials are set should reach the inbox even if
#: preferences still name a channel that was never finished being set up.
FALLBACK_ORDER: tuple[str, ...] = (EmailProvider.name, TelegramProvider.name)


def get_provider(
    name: str, *, recipient: str | None = None, config=None
) -> NotificationProvider:
    """Instantiate a provider, degrading to a real channel and then to a file.

    Degrading rather than failing means a digest is never lost just because
    the selected channel was never finished being set up.
    """
    def build(provider_cls) -> NotificationProvider:
        # Only the email provider is addressed; a chat channel has one target.
        if provider_cls is EmailProvider:
            return provider_cls(recipient, config=config)
        if provider_cls is TelegramProvider:
            return provider_cls(config=config)
        return provider_cls()

    cls = PROVIDERS.get(name)
    if cls is None:
        log.warning("notify.unknown_provider", provider=name)
    else:
        provider = build(cls)
        if provider.is_configured():
            return provider
        log.warning("notify.provider_unconfigured", provider=name)

    for fallback in FALLBACK_ORDER:
        if fallback == name:
            continue
        candidate = build(PROVIDERS[fallback])
        if candidate.is_configured():
            log.info("notify.provider_fallback", requested=name, using=fallback)
            return candidate

    return FileProvider()


def provider_catalog(config=None) -> list[dict[str, object]]:
    out = []
    for name, cls in PROVIDERS.items():
        if cls is EmailProvider:
            instance = cls(config=config)
        elif cls is TelegramProvider:
            instance = cls(config=config)
        else:
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
