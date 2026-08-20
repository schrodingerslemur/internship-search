"""User, search preferences, candidate profile, and resumes.

Search configuration lives in ``UserPreference.data`` as a validated JSON
document (see ``app.schemas.preferences.SearchPreferences``). Storing it as one
versioned document keeps roles, weights, and thresholds fully user-editable
without a migration every time a knob is added.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """An account. Everything subjective about a job hangs off one of these.

    The job pool is shared -- crawled once, deduplicated once -- while
    preferences, the candidate profile, resumes, digest delivery and the
    application tracker are all per user. Two people searching from the same
    instance never see each other's decisions.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")

    #: scrypt hash; never the password itself. Null means the account cannot
    #: log in yet -- the pre-accounts single user is migrated in this state.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Where this user's digests go. Falls back to ``email`` when unset.
    digest_email: Mapped[str | None] = mapped_column(String(320))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    preference: Mapped[UserPreference | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    profile: Mapped[CandidateProfile | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    resumes: Mapped[list[Resume]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    job_states: Mapped[list[UserJobState]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def notification_email(self) -> str | None:
        return self.digest_email or self.email


class UserJobState(Base, TimestampMixin):
    """One user's relationship to one shared job.

    Status and notification history live here rather than on ``Job`` because
    they are opinions, not facts: your having applied to a posting says
    nothing about whether anyone else has, and must not silence it for them.
    """

    __tablename__ = "user_job_state"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_user_job_state_user_id_job_id"),
        Index("ix_user_job_state_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(String(30), default="new", nullable=False, index=True)

    #: This job scored against *this* user's profile and weights. Null means it
    #: has not been scored for them yet -- a job crawled before they signed up.
    relevance_score: Mapped[float | None] = mapped_column(Float)
    priority: Mapped[str | None] = mapped_column(String(30), index=True)
    match_reasons: Mapped[list | None] = mapped_column(JSON)
    concerns: Mapped[list | None] = mapped_column(JSON)
    missing_requirements: Mapped[list | None] = mapped_column(JSON)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    saved_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(back_populates="job_states")
    job: Mapped["Job"] = relationship()  # noqa: F821, UP037 - string ref: Job is in another module


class UserPreference(Base, TimestampMixin):
    """Versioned search-preference document."""

    __tablename__ = "user_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    #: Serialised SearchPreferences document.
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)

    user: Mapped[User] = relationship(back_populates="preference")


class CandidateProfile(Base, TimestampMixin):
    """The candidate the jobs are being matched against."""

    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    school: Mapped[str | None] = mapped_column(String(300))
    degree: Mapped[str | None] = mapped_column(String(150))
    major: Mapped[str | None] = mapped_column(String(200))
    minor: Mapped[str | None] = mapped_column(String(200))
    graduation_year: Mapped[int | None] = mapped_column(Integer)
    graduation_month: Mapped[int | None] = mapped_column(Integer)
    gpa: Mapped[float | None] = mapped_column(Float)

    technical_skills: Mapped[list] = mapped_column(JSON, default=list)
    programming_languages: Mapped[list] = mapped_column(JSON, default=list)
    hardware_skills: Mapped[list] = mapped_column(JSON, default=list)
    software_skills: Mapped[list] = mapped_column(JSON, default=list)
    tools: Mapped[list] = mapped_column(JSON, default=list)

    research_experience: Mapped[str | None] = mapped_column(Text)
    previous_internships: Mapped[list] = mapped_column(JSON, default=list)
    projects: Mapped[list] = mapped_column(JSON, default=list)
    publications: Mapped[list] = mapped_column(JSON, default=list)
    coursework: Mapped[list] = mapped_column(JSON, default=list)

    preferred_industries: Mapped[list] = mapped_column(JSON, default=list)
    work_authorization: Mapped[str | None] = mapped_column(String(120))
    requires_sponsorship: Mapped[bool | None] = mapped_column(Boolean)
    security_clearance: Mapped[str | None] = mapped_column(String(120))
    preferred_locations: Mapped[list] = mapped_column(JSON, default=list)
    willing_to_relocate: Mapped[bool] = mapped_column(Boolean, default=True)
    summary: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="profile")

    def all_skills(self) -> list[str]:
        """Every skill token from every skill bucket, de-duplicated."""
        merged: list[str] = []
        for bucket in (
            self.technical_skills,
            self.programming_languages,
            self.hardware_skills,
            self.software_skills,
            self.tools,
        ):
            merged.extend(bucket or [])
        seen: set[str] = set()
        out: list[str] = []
        for item in merged:
            key = str(item).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(str(item).strip())
        return out


class Resume(Base, TimestampMixin):
    """An uploaded resume variant used for per-job recommendations."""

    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: hardware, software, quant, general, ...
    kind: Mapped[str] = mapped_column(String(60), default="general")
    filename: Mapped[str | None] = mapped_column(String(400))
    file_path: Mapped[str | None] = mapped_column(String(1000))
    #: Extracted plain text, used for keyword matching against job descriptions.
    text_content: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(back_populates="resumes")


class AppConfig(Base, TimestampMixin):
    """Deployment-owned key/value settings that must survive a restart.

    The session signing key lives here: a free host stops the web service
    whenever it is idle, and a key held only in process memory would log
    everyone out every time it woke up.
    """

    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
