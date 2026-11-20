"""Email verification links, and who may sign up at all.

A new account cannot sign in until it clicks a link sent to its address, which
proves the person typing the email actually reads that inbox -- otherwise the
allowlist would let anyone in who merely knows an allowed address.

Like password resets, nothing is stored: the token is signed with a key
derived from the account's id, its email and whether it is still unverified,
so a link stops working the moment the account is verified or its email
changes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User
from app.services.password_reset import _b64, _unb64

#: Long enough to read the email tomorrow morning.
VERIFY_MAX_AGE = 3 * 24 * 60 * 60


# --------------------------------------------------------------------------
# Who may sign up
# --------------------------------------------------------------------------


def parse_entries(raw: str) -> list[str]:
    """Addresses or "@domain" entries from text split by commas, spaces or lines."""
    return [e.strip().lower() for e in re.split(r"[,\s]+", raw or "") if e.strip()]


def allowed_entries(session: Session | None = None) -> list[str]:
    """SIGNUP_ALLOWED_EMAILS plus whatever the owner added on /admin."""
    entries = parse_entries(get_settings().signup_allowed_emails or "")
    if session is not None:
        from sqlalchemy import select

        from app.models import AllowedEmail

        entries += [e for e in session.scalars(select(AllowedEmail.entry)).all() if e not in entries]
    return entries


def email_allowed(email: str, session: Session | None = None) -> bool:
    """Whether ``email`` (already normalised) may create an account.

    No list set: everyone. The owner's address is always allowed, so a typo in
    the list can never lock the owner out of re-creating their own account.
    """
    entries = allowed_entries(session)
    if not entries:
        return True
    owner = (get_settings().owner_email or "").strip().lower()
    if owner and email == owner:
        return True
    domain = "@" + email.rpartition("@")[2]
    return any(email == e or (e.startswith("@") and domain == e) for e in entries)


# --------------------------------------------------------------------------
# Verification links
# --------------------------------------------------------------------------


def _derived_key(user: User, key: bytes) -> bytes:
    state = "verified" if user.email_verified_at else "pending"
    return hmac.new(key, f"verify:{user.id}:{user.email}:{state}".encode(), hashlib.sha256).digest()


def issue(user: User, key: bytes, *, now: float | None = None) -> str:
    payload = json.dumps({"u": user.id, "t": int(now or time.time())}, separators=(",", ":")).encode()
    signature = hmac.new(_derived_key(user, key), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify(session: Session, token: str, key: bytes, *, now: float | None = None) -> User | None:
    """The unverified account a link belongs to, or None. Never raises."""
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
    if (now or time.time()) - issued > VERIFY_MAX_AGE:
        return None
    user = session.get(User, user_id)
    if user is None or not user.is_active or user.email_verified_at is not None:
        return None
    expected = hmac.new(_derived_key(user, key), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided):
        return None
    return user


def build_email(user: User, link: str) -> tuple[str, str, str]:
    """Subject, plain body and HTML body for the verification message."""
    subject = "Confirm your email for Internship Search"
    text = (
        "Someone created an Internship Search account with this address.\n\n"
        "Confirm it's you to finish signing up:\n\n"
        f"{link}\n\n"
        "The link expires in three days. If this was not you, ignore this email\n"
        "-- the account cannot be used without it."
    )
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    html = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f4f5f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:12px;padding:28px 24px;">
<tr><td style="font:700 18px/1.3 {font};color:#111;padding-bottom:10px;">Confirm your email</td></tr>
<tr><td style="font:400 14px/1.6 {font};color:#333;padding-bottom:20px;">
Someone created an Internship Search account with this address. Confirm it's you to finish signing up.
</td></tr>
<tr><td style="padding-bottom:20px;">
<a href="{link}" style="font:600 14px/1 {font};color:#fff;background:#1a5fb4;padding:12px 18px;border-radius:8px;text-decoration:none;display:inline-block;">Confirm my email</a>
</td></tr>
<tr><td style="font:400 12.5px/1.6 {font};color:#777;">
The link expires in three days. If this was not you, ignore this email &mdash;
the account cannot be used without it.
</td></tr>
</table></td></tr></table></body></html>"""
    return subject, text, html
