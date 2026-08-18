"""Loading and saving user preferences, profile, and resumes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import CandidateProfile, User, UserPreference
from app.schemas.preferences import SearchPreferences, default_preferences
from app.schemas.profile import CandidateProfileData, default_profile

log = get_logger("preferences")

#: This is a single-user application; everything hangs off one user row.
DEFAULT_USER_EMAIL = "me@localhost"


def get_or_create_user(session: Session) -> User:
    user = session.scalars(select(User).order_by(User.id).limit(1)).first()
    if user is None:
        user = User(email=DEFAULT_USER_EMAIL, name="Me")
        session.add(user)
        session.flush()
        log.info("preferences.user_created", user_id=user.id)
    return user


def load_preferences(session: Session) -> SearchPreferences:
    """Load preferences, falling back to defaults on first run.

    A stored document that fails validation (e.g. after a schema change) is
    merged onto defaults rather than crashing the run.
    """
    user = get_or_create_user(session)
    row = session.scalar(select(UserPreference).where(UserPreference.user_id == user.id))
    if row is None or not row.data:
        prefs = default_preferences()
        save_preferences(session, prefs)
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


def save_preferences(session: Session, prefs: SearchPreferences) -> UserPreference:
    user = get_or_create_user(session)
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


def load_profile(session: Session) -> CandidateProfileData:
    user = get_or_create_user(session)
    row = session.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if row is None:
        profile = default_profile()
        save_profile(session, profile)
        return profile
    return CandidateProfileData.from_orm_profile(row)


def save_profile(session: Session, profile: CandidateProfileData) -> CandidateProfile:
    user = get_or_create_user(session)
    row = session.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if row is None:
        row = CandidateProfile(user_id=user.id)
        session.add(row)
    for name, value in profile.model_dump().items():
        if hasattr(row, name):
            setattr(row, name, value)
    session.flush()
    return row


def get_profile_row(session: Session) -> CandidateProfile | None:
    user = get_or_create_user(session)
    return session.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
