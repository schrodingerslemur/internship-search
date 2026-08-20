"""Loading and saving user preferences, profile, and resumes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import CandidateProfile, User, UserPreference
from app.schemas.preferences import SearchPreferences, default_preferences
from app.schemas.profile import CandidateProfileData, default_profile

log = get_logger("preferences")

#: The account created before there were accounts. Still the fallback when no
#: particular user is named, which keeps the CLI and single-user setups working.
DEFAULT_USER_EMAIL = "me@localhost"


def get_or_create_user(session: Session) -> User:
    """The default user: the oldest account, created if there are none."""
    user = session.scalars(select(User).order_by(User.id).limit(1)).first()
    if user is None:
        user = User(email=DEFAULT_USER_EMAIL, name="Me")
        session.add(user)
        session.flush()
        log.info("preferences.user_created", user_id=user.id)
    return user


def _resolve(session: Session, user: User | None) -> User:
    return user if user is not None else get_or_create_user(session)


def initialise_user(session: Session, user: User) -> None:
    """Give a brand-new account its own preferences and profile documents."""
    save_preferences(session, default_preferences(), user=user)
    save_profile(session, default_profile(), user=user)


def all_active_users(session: Session) -> list[User]:
    """Every account that should receive digests, oldest first."""
    return list(
        session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id)).all()
    )


def load_preferences(session: Session, user: User | None = None) -> SearchPreferences:
    """Load preferences, falling back to defaults on first run.

    A stored document that fails validation (e.g. after a schema change) is
    merged onto defaults rather than crashing the run.
    """
    user = _resolve(session, user)
    row = session.scalar(select(UserPreference).where(UserPreference.user_id == user.id))
    if row is None or not row.data:
        prefs = default_preferences()
        save_preferences(session, prefs, user=user)
        return prefs
    try:
        return SearchPreferences.model_validate(row.data)
    except Exception as exc:
        log.warning("preferences.invalid_document", error=str(exc)[:200])
        merged = default_preferences().model_dump()
        if isinstance(row.data, dict):
            merged.update({k: v for k, v in row.data.items() if k in merged})
        try:
            return SearchPreferences.model_validate(merged)
        except Exception:
            return default_preferences()


def save_preferences(
    session: Session, prefs: SearchPreferences, user: User | None = None
) -> UserPreference:
    user = _resolve(session, user)
    row = session.scalar(select(UserPreference).where(UserPreference.user_id == user.id))
    payload = prefs.model_dump(mode="json")
    if row is None:
        row = UserPreference(user_id=user.id, data=payload, version=1)
        session.add(row)
    else:
        row.data = payload
        row.version = (row.version or 1) + 1
    session.flush()
    return row


def load_profile(session: Session, user: User | None = None) -> CandidateProfileData:
    user = _resolve(session, user)
    row = session.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if row is None:
        profile = default_profile()
        save_profile(session, profile, user=user)
        return profile
    return CandidateProfileData.from_orm_profile(row)


def save_profile(
    session: Session, profile: CandidateProfileData, user: User | None = None
) -> CandidateProfile:
    user = _resolve(session, user)
    row = session.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if row is None:
        row = CandidateProfile(user_id=user.id)
        session.add(row)
    for name, value in profile.model_dump().items():
        if hasattr(row, name):
            setattr(row, name, value)
    session.flush()
    return row


def get_profile_row(session: Session, user: User | None = None) -> CandidateProfile | None:
    user = _resolve(session, user)
    return session.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
