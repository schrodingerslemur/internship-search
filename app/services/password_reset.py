"""Password reset links.

Single-use without storing anything: the token is signed with a key derived
from the app's signing key *and the account's current password hash*. Setting a
new password changes that hash, which changes the derived key, which
invalidates every outstanding link for that account -- including the one just
used, and including any issued earlier and forgotten about.

Short-lived by design. A reset link is the one credential that arrives in an
inbox, so it should stop working long before the email stops existing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from sqlalchemy.orm import Session

from app.models import User

#: Long enough to walk to a laptop, short enough that a stale email is inert.
RESET_MAX_AGE = 60 * 60


def _derived_key(user: User, key: bytes) -> bytes:
    """Per-account signing key that dies with the password it can replace."""
    return hmac.new(key, f"pwreset:{user.id}:{user.password_hash or ''}".encode(), hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue(user: User, key: bytes, *, now: float | None = None) -> str:
    payload = json.dumps(
        {"u": user.id, "t": int(now or time.time())}, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.new(_derived_key(user, key), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify(session: Session, token: str, key: bytes, *, now: float | None = None) -> User | None:
    """The account a reset link belongs to, or None.

    Never raises: mail clients rewrite URLs freely, and a mangled link should
    produce a polite page rather than an error.
    """
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    try:
        payload = _unb64(body)
        provided = _unb64(signature)
        data = json.loads(payload)
        user_id = int(data["u"])
        issued = int(data["t"])
    except (ValueError, TypeError, KeyError):
        return None

    if (now or time.time()) - issued > RESET_MAX_AGE:
        return None

    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None

    expected = hmac.new(_derived_key(user, key), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided):
        return None
    return user


def build_email(user: User, link: str) -> tuple[str, str, str]:
    """Subject, plain body and HTML body for the reset message."""
    subject = "Reset your Internship Search password"
    text = (
        "Someone asked to reset the password for your Internship Search account.\n\n"
        f"{link}\n\n"
        "The link works once and expires in an hour. If this was not you, you can\n"
        "ignore this email -- your password has not changed."
    )
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    html = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f4f5f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:12px;padding:28px 24px;">
<tr><td style="font:700 18px/1.3 {font};color:#111;padding-bottom:10px;">Reset your password</td></tr>
<tr><td style="font:400 14px/1.6 {font};color:#333;padding-bottom:20px;">
Someone asked to reset the password for your Internship Search account.
</td></tr>
<tr><td style="padding-bottom:20px;">
<a href="{link}" style="font:600 14px/1 {font};color:#fff;background:#1a5fb4;padding:12px 18px;border-radius:8px;text-decoration:none;display:inline-block;">Choose a new password</a>
</td></tr>
<tr><td style="font:400 12.5px/1.6 {font};color:#777;">
The link works once and expires in an hour. If this was not you, ignore this
email &mdash; your password has not changed.
</td></tr>
</table></td></tr></table></body></html>"""
    return subject, text, html
