"""Application tracking, notifications, source registry, and search runs."""

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

from app.models.base import Base, RunStatus, SourceHealth, TimestampMixin


class Application(Base, TimestampMixin):
    """The user's relationship with one canonical job (Kanban card)."""

    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", name="uq_application_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="saved", index=True)

    date_saved: Mapped[datetime | None] = mapped_column(DateTime)
    date_applied: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    date_assessment: Mapped[datetime | None] = mapped_column(DateTime)
    date_interview: Mapped[datetime | None] = mapped_column(DateTime)
    date_offer: Mapped[datetime | None] = mapped_column(DateTime)
    date_rejected: Mapped[datetime | None] = mapped_column(DateTime)
    deadline: Mapped[datetime | None] = mapped_column(DateTime)

    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id", ondelete="SET NULL"))
    resume_version: Mapped[str | None] = mapped_column(String(200))
    cover_letter_version: Mapped[str | None] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    referral: Mapped[str | None] = mapped_column(String(300))
    #: Snapshot of the job's relevance score at the time of application, so that
    #: outcome analytics are not distorted by later re-scoring.
    score_at_apply: Mapped[float | None] = mapped_column(Float)
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime)

    notes: Mapped[list[ApplicationNote]] = relationship(
        back_populates="application", cascade="all, delete-orphan", lazy="selectin"
    )


class ApplicationNote(Base):
    __tablename__ = "application_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    application: Mapped[Application] = relationship(back_populates="notes")


class JobSourceRecord(Base, TimestampMixin):
    """Registry row per job source: health, config, and adaptive yield stats."""

    __tablename__ = "job_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="unknown")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    requires_credentials: Mapped[bool] = mapped_column(Boolean, default=False)
    health: Mapped[str] = mapped_column(String(20), default=SourceHealth.OK.value)
    notes: Mapped[str | None] = mapped_column(Text)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    # ---- Adaptive-search statistics (rolling, never used to disable a source) ----
    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    total_jobs_returned: Mapped[int] = mapped_column(Integer, default=0)
    total_unique_contributed: Mapped[int] = mapped_column(Integer, default=0)
    total_relevant_contributed: Mapped[int] = mapped_column(Integer, default=0)
    total_strong_matches: Mapped[int] = mapped_column(Integer, default=0)
    avg_duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)


class SearchRun(Base):
    """One end-to-end execution of the search pipeline."""

    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(40), default="manual")
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.RUNNING.value, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    queries_generated: Mapped[int] = mapped_column(Integer, default=0)
    sources_attempted: Mapped[int] = mapped_column(Integer, default=0)
    sources_successful: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    sources_unconfigured: Mapped[int] = mapped_column(Integer, default=0)

    raw_jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    jobs_normalized: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_removed: Mapped[int] = mapped_column(Integer, default=0)
    unique_jobs: Mapped[int] = mapped_column(Integer, default=0)
    relevant_jobs: Mapped[int] = mapped_column(Integer, default=0)
    high_priority_jobs: Mapped[int] = mapped_column(Integer, default=0)

    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    updated_jobs: Mapped[int] = mapped_column(Integer, default=0)
    reposted_jobs: Mapped[int] = mapped_column(Integer, default=0)
    expired_jobs: Mapped[int] = mapped_column(Integer, default=0)

    companies_discovered: Mapped[int] = mapped_column(Integer, default=0)
    ats_boards_discovered: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    notifications_sent: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)

    source_stats: Mapped[list[SearchRunSource]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class SearchRunSource(Base):
    """Per-source outcome within one run. Powers the coverage dashboard."""

    __tablename__ = "search_run_sources"
    __table_args__ = (Index("ix_run_source", "run_id", "source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="unknown")
    status: Mapped[str] = mapped_column(String(20), default=SourceHealth.OK.value)
    #: Listings actually returned. Never populated for a source that did not run.
    jobs_returned: Mapped[int] = mapped_column(Integer, default=0)
    unique_contributed: Mapped[int] = mapped_column(Integer, default=0)
    relevant_contributed: Mapped[int] = mapped_column(Integer, default=0)
    queries_run: Mapped[int] = mapped_column(Integer, default=0)
    sub_targets_attempted: Mapped[int] = mapped_column(Integer, default=0)
    sub_targets_successful: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[SearchRun] = relationship(back_populates="source_stats")


class Notification(Base):
    """A dispatched notification and the jobs it contained."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    job_count: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    items: Mapped[list[NotificationItem]] = relationship(
        back_populates="notification", cascade="all, delete-orphan", lazy="selectin"
    )


class NotificationItem(Base):
    """Join row proving a specific job was already sent, preventing repeats."""

    __tablename__ = "notification_items"
    __table_args__ = (Index("ix_notification_items_job", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    #: new, update, deadline -- why this job was included.
    reason: Mapped[str] = mapped_column(String(40), default="new")
    score: Mapped[float | None] = mapped_column(Float)

    notification: Mapped[Notification] = relationship(back_populates="items")
