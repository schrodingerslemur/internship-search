"""Accounts: password hashing and signed session cookies.

Both are stdlib. ``hashlib.scrypt`` is a memory-hard KDF that ships with
Python, and an HMAC-signed cookie needs no server-side session store -- which
matters here because the dashboard runs on a host that sleeps and restarts
freely, and a logged-in user should survive that.

The signing key is persisted in the database rather than generated per process.
On a free host the web service is stopped whenever it is idle; a per-process
key would silently log everyone out several times a day.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import User
from app.models.base import utcnow

log = get_logger("auth")

#: scrypt parameters. n=2**14 keeps a single hash near ~50ms on a small host,
#: which is slow enough to be unpleasant to brute-force and fast enough that a
#: sleeping free-tier dyno still logs someone in promptly.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16

SESSION_COOKIE = "internship_session"
#: Long enough not to nag a daily user; short enough to bound a stolen cookie.
SESSION_MAX_AGE = 60 * 60 * 24 * 30

MIN_PASSWORD_LENGTH = 8


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(digest).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${digest_b64}"


def verify_password(password: str, stored: str | None) -> bool:
    """Check a password against a stored hash. Never raises on malformed input."""
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password: str) -> str | None:
    """Human-readable reason a password is unacceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


# --------------------------------------------------------------------------
# Signing key
# --------------------------------------------------------------------------


def get_signing_key(session: Session) -> bytes:
    """The persisted cookie-signing key, created once on first use."""
    from app.services.appconfig import get_or_create_config

    return get_or_create_config(
        session, "session_signing_key", lambda: secrets.token_urlsafe(48)
    ).encode("utf-8")


# --------------------------------------------------------------------------
# Session cookies
# --------------------------------------------------------------------------


def _sign(payload: bytes, key: bytes) -> str:
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return "{}.{}".format(
        base64.urlsafe_b64encode(payload).decode().rstrip("="),
        base64.urlsafe_b64encode(signature).decode().rstrip("="),
    )


def _unpad(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(user: User, key: bytes, *, now: float | None = None) -> str:
    """Mint a signed cookie.

    Identity *and* display name travel inside it. The signature is what makes
    that safe, and it means rendering the account menu costs no database query
    -- which matters when the database is a network hop away and every page
    would otherwise pay for it.
    """
    payload = json.dumps(
        {
            "uid": user.id,
            "email": user.email,
            "name": user.name,
            "iat": int(now or time.time()),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return _sign(payload, key)


def read_claims(token: str | None, key: bytes, *, now: float | None = None) -> dict | None:
    """Verified cookie contents, or None if it is missing, forged or expired."""
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    try:
        payload = _unpad(body)
        expected = _unpad(signature)
    except (ValueError, TypeError):
        return None

    if not hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).digest(), expected):
        return None

    try:
        data = json.loads(payload)
        data["uid"] = int(data["uid"])
        issued = int(data["iat"])
    except (ValueError, KeyError, TypeError):
        return None

    if (now or time.time()) - issued > SESSION_MAX_AGE:
        return None
    return data


def read_session(token: str | None, key: bytes, *, now: float | None = None) -> int | None:
    """The user id a cookie vouches for, or None.

    None for anything suspicious rather than raising, so a malformed or
    tampered cookie logs the visitor out instead of erroring the request.
    """
    claims = read_claims(token, key, now=now)
    return claims["uid"] if claims else None


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


def normalise_email(email: str) -> str:
    return email.strip().lower()


def find_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == normalise_email(email)))


def create_account(
    session: Session, *, email: str, password: str, name: str | None = None
) -> User:
    """Create an account with its own preferences and profile."""
    from app.services.preferences import initialise_user

    user = User(
        email=normalise_email(email),
        name=name or normalise_email(email).split("@")[0],
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(user)
    session.flush()
    initialise_user(session, user)
    log.info("auth.account_created", user_id=user.id)
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = find_by_email(session, email)
    if user is None or not user.is_active:
        # Hash anyway, so a missing account and a wrong password take the same
        # time and cannot be told apart by timing.
        verify_password(password, hash_password("dummy"))
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = utcnow()
    session.flush()
    return user


def get_user(session: Session, user_id: int) -> User | None:
    user = session.get(User, user_id)
    return user if (user and user.is_active) else None


def claimable_legacy_account(session: Session) -> User | None:
    """The pre-accounts user, if it is still unclaimed.

    Before accounts existed the app created a single ``me@localhost`` row and
    hung everything off it. That row owns the existing tracker, so the first
    person to sign up adopts it -- otherwise their applications would be
    stranded behind an account with no password that nobody can ever log into.

    Only ever returns a row when *no* account has a password yet, so this
    cannot hand an established instance to a stranger.
    """
    users = list(session.scalars(select(User).order_by(User.id)).all())
    if len(users) != 1:
        return None
    return users[0] if users[0].password_hash is None else None


def claim_account(session: Session, user: User, *, email: str, password: str, name: str | None):
    """Give the legacy account real credentials, keeping its id and its data."""
    user.email = normalise_email(email)
    user.password_hash = hash_password(password)
    if name:
        user.name = name
    user.is_active = True
    session.flush()
    log.info("auth.legacy_account_claimed", user_id=user.id)
    return user


def all_accounts(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.id)).all())


def account_count(session: Session) -> int:
    from sqlalchemy import func

    return session.scalar(select(func.count(User.id))) or 0
