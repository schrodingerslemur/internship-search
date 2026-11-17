"""Seal and open the API keys users bring, with a key the database never sees.

``SECRETS_KEY`` is a Fernet key set in the host's environment (Render, and the
GitHub Actions secrets for the worker). A copy of the database alone -- a
leaked backup, a shared Neon branch -- holds only ciphertext. Generate one with

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

and set the same value everywhere the app or the worker runs. Changing it
makes every saved key unreadable; users then paste theirs again.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class SecretsUnavailable(RuntimeError):
    """SECRETS_KEY is unset or malformed, so no key can be saved or read."""


def _fernet() -> Fernet:
    key = (get_settings().secrets_key or "").strip()
    if not key:
        raise SecretsUnavailable("SECRETS_KEY is not set on this server")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise SecretsUnavailable("SECRETS_KEY is not a valid Fernet key") from exc


def available() -> bool:
    try:
        _fernet()
    except SecretsUnavailable:
        return False
    return True


def seal(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def unseal(token: str) -> str | None:
    """The secret, or None when it was sealed under a different SECRETS_KEY."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, SecretsUnavailable):
        return None
