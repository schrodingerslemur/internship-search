"""Signed one-click action links for digest emails.

A digest is read on a phone, often on a device that is not signed in. Making
you log in before you can dismiss a job means you do not dismiss it, and the
next digest has to decide all over again whether to mention it.

So each button carries a token that names exactly one (user, job, action) and
nothing else. It is signed with the same key as the session cookie, expires,
and cannot be edited into a different action or a different job -- changing any
field invalidates the signature.

Deliberately narrow. A token cannot sign you in, cannot read anything, and
cannot do anything to a job other than the single action it names.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

#: Actions a link may carry. Anything else is refused, so a token can never be
#: edited into, say, deleting an account.
ALLOWED_ACTIONS: frozenset[str] = frozenset({"applied", "saved", "dismissed"})

#: Long enough to act on a digest days later; short enough that a forwarded
#: email stops working eventually.
TOKEN_MAX_AGE = 60 * 60 * 24 * 30


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue(user_id: int, job_id: int, action: str, key: bytes, *, now: float | None = None) -> str:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unsupported action: {action}")
    payload = json.dumps(
        {"u": user_id, "j": job_id, "a": action, "t": int(now or time.time())},
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify(token: str, key: bytes, *, now: float | None = None) -> dict | None:
    """The action a token authorises, or None if it is not trustworthy.

    Never raises: a mangled link (mail clients wrap and re-encode URLs freely)
    should show a polite failure page, not a traceback.
    """
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    try:
        payload = _unb64(body)
        expected = _unb64(signature)
    except (ValueError, TypeError):
        return None

    if not hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).digest(), expected):
        return None

    try:
        data = json.loads(payload)
        result = {
            "user_id": int(data["u"]),
            "job_id": int(data["j"]),
            "action": str(data["a"]),
            "issued_at": int(data["t"]),
        }
    except (ValueError, KeyError, TypeError):
        return None

    if result["action"] not in ALLOWED_ACTIONS:
        return None
    if (now or time.time()) - result["issued_at"] > TOKEN_MAX_AGE:
        return None
    return result
