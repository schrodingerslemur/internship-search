"""Read the security code some Greenhouse boards email before accepting an application.

Greenhouse sends "Copy and paste this code into the security code field" to the
applicant's address after Submit is clicked. With IMAP access to that mailbox
(a Gmail app password works -- the same one the digest already sends with),
the worker reads the code and finishes the submission itself.
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger("autoapply.inbox")

_TOKEN = re.compile(r"\b[A-Za-z0-9]{8}\b")
_TAGS = re.compile(r"<[^>]+>")


def credentials() -> tuple[str, str, str] | None:
    s = get_settings()
    user = s.apply_imap_user or s.smtp_user
    password = s.apply_imap_password or s.smtp_password
    host = s.apply_imap_host
    if not host and s.smtp_host:
        host = re.sub(r"^smtp\.", "imap.", s.smtp_host)
    if not (host and user and password):
        return None
    return host, user, password


def extract_code(text: str) -> str | None:
    """The 8-character code that follows the word "code" in the email.

    Ordinary 8-letter words ("security", "Lovelace") are skipped: a code
    carries a digit, or capitals past its first letter.
    """
    text = _TAGS.sub(" ", text or "")
    at = text.lower().find("code")
    if at < 0:
        return None
    for token in _TOKEN.findall(text[at:]):
        if any(c.isdigit() for c in token) or any(c.isupper() for c in token[1:]):
            return token
    return None


def _body(message: email.message.Message) -> str:
    parts = message.walk() if message.is_multipart() else [message]
    out = []
    for part in parts:
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            out.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    return "\n".join(out)


def wait_for_security_code(
    *, since: datetime, timeout_s: float = 120, poll_s: float = 6
) -> str | None:
    creds = credentials()
    if creds is None:
        return None
    host, user, password = creds
    deadline = time.monotonic() + timeout_s
    floor = since - timedelta(minutes=1)
    while time.monotonic() < deadline:
        try:
            code = _check_once(host, user, password, floor)
        except (imaplib.IMAP4.error, OSError) as exc:
            log.warning("inbox.failed", error=type(exc).__name__)
            return None
        if code:
            return code
        time.sleep(poll_s)
    return None


def _check_once(host: str, user: str, password: str, floor: datetime) -> str | None:
    with imaplib.IMAP4_SSL(host) as box:
        box.login(user, password)
        box.select("INBOX", readonly=True)
        day = floor.strftime("%d-%b-%Y")
        _, data = box.search(None, f'(SINCE "{day}" FROM "greenhouse")')
        ids = (data[0] or b"").split()[-5:]
        for msg_id in reversed(ids):
            _, parts = box.fetch(msg_id, "(RFC822)")
            message = email.message_from_bytes(parts[0][1])
            try:
                sent = parsedate_to_datetime(message.get("Date"))
            except (TypeError, ValueError):
                continue
            if sent.tzinfo and sent < floor.astimezone(sent.tzinfo):
                continue
            code = extract_code(_body(message))
            if code:
                return code
    return None
