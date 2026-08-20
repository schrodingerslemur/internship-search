"""Small persisted key/value store for values that must outlive a process.

Only for things the deployment itself owns -- the session signing key is the
motivating case. User-facing configuration belongs in ``UserPreference``.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppConfig


def get_config(session: Session, key: str) -> str | None:
    row = session.scalar(select(AppConfig).where(AppConfig.key == key))
    return row.value if row else None


def set_config(session: Session, key: str, value: str) -> None:
    row = session.scalar(select(AppConfig).where(AppConfig.key == key))
    if row is None:
        session.add(AppConfig(key=key, value=value))
    else:
        row.value = value
    session.flush()


def get_or_create_config(session: Session, key: str, factory: Callable[[], str]) -> str:
    """Read a value, creating it once if absent.

    Concurrent web workers can race here; the unique constraint on ``key`` is
    what makes that safe, and the loser re-reads the winner's value.
    """
    existing = get_config(session, key)
    if existing is not None:
        return existing

    value = factory()
    try:
        with session.begin_nested():
            session.add(AppConfig(key=key, value=value))
        session.flush()
        return value
    except Exception:
        session.rollback()
        found = get_config(session, key)
        if found is None:
            raise
        return found
