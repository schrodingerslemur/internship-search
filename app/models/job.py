"""Canonical jobs, per-source listings, scores, and change events.

A ``Job`` is the *underlying position*, independent of the websites it was found
on. Every website that advertised it becomes a ``JobListing`` row pointing at
the same job. The user sees one job; the system knows it came from four places.
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

from app.models.base import (
    Base,
    EmploymentType,
    Freshness,
    JobStatus,
    Priority,
    RemoteStatus,
    SponsorshipStatus,
    TimestampMixin,
)


class Job(Base, TimestampMixin):
    """A canonical, deduplicated job posting."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_fingerprint", "fingerprint"),
        Index("ix_jobs_status_score", "status", "relevance_score"),
        Index("ix_jobs_active_posted", "is_active", "date_posted"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Stable public identifier, safe to reference from notifications.
    canonical_job_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    #: Deterministic dedup fingerprint (company, title, location, employment type).
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Strongest dedup key when present, e.g. greenhouse:acme:12345
    ats_identity: Mapped[str | None] = mapped_column(String(500), index=True)

    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Title reduced to its comparable core (seniority and req-id noise stripped).
    title_core: Mapped[str] = mapped_column(String(500), default="")

    location_raw: Mapped[str | None] = mapped_column(String(400))
    locations: Mapped[list] = mapped_column(JSON, default=list)
    city: Mapped[str | None] = mapped_column(String(150))
    state: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(100))
    remote_status: Mapped[str] = mapped_column(String(20), default=RemoteStatus.UNKNOWN.value)
    employment_type: Mapped[str] = mapped_column(String(20), default=EmploymentType.UNKNOWN.value)

    description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)
    preferred_qualifications: Mapped[str | None] = mapped_column(Text)
    responsibilities: Mapped[str | None] = mapped_column(Text)

    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(10))
    salary_period: Mapped[str | None] = mapped_column(String(20))
    salary_raw: Mapped[str | None] = mapped_column(String(300))

    #: Best available application URL, elected across all listings.
    application_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    posting_url: Mapped[str | None] = mapped_column(String(1000))

    date_posted: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    date_discovered: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    date_updated_source: Mapped[datetime | None] = mapped_column(DateTime)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    #: True only when a deadline was explicitly stated. Never inferred.
    deadline_is_explicit: Mapped[bool] = mapped_column(Boolean, default=False)

    sponsorship: Mapped[str] = mapped_column(String(40), default=SponsorshipStatus.UNKNOWN.value)
    sponsorship_evidence: Mapped[str | None] = mapped_column(Text)
    experience_required_years: Mapped[float | None] = mapped_column(Float)
    degree_requirements: Mapped[list] = mapped_column(JSON, default=list)
    #: Internship terms mentioned, e.g. Summer 2026.
    terms: Mapped[list] = mapped_column(JSON, default=list)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    # Workday uses the full job slug as its requisition id, which routinely runs
    # past 120 characters. Never truncated -- see IDENTITY_COLUMNS.
    requisition_id: Mapped[str | None] = mapped_column(String(500), index=True)

    # ---- Scoring and triage ----
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    priority: Mapped[str] = mapped_column(String(30), default=Priority.SKIP.value, index=True)
    match_reasons: Mapped[list] = mapped_column(JSON, default=list)
    missing_requirements: Mapped[list] = mapped_column(JSON, default=list)
    concerns: Mapped[list] = mapped_column(JSON, default=list)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_assessment: Mapped[dict | None] = mapped_column(JSON)

    # ---- Model-read facts ----
    #: Structured facts a language model read out of the prose, kept separate
    #: from the vocabulary-matched `skills` so the two are always tellable
    #: apart: one is a regex hit, the other is a model's reading, and a user
    #: deciding whether to trust a score deserves to know which.
    enrichment: Mapped[dict | None] = mapped_column(JSON)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: Which model produced it, so a bad batch can be found and re-run.
    enrichment_model: Mapped[str | None] = mapped_column(String(120))
    #: The content hash the enrichment was read from. When the posting changes,
    #: the facts are stale and the job becomes eligible again.
    enrichment_hash: Mapped[str | None] = mapped_column(String(64))

    # ---- Lifecycle ----
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.NEW.value, index=True)
    freshness: Mapped[str] = mapped_column(String(20), default=Freshness.NEW.value, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    #: Set once the job stops appearing in crawls of a source that once had it.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime)
    times_reposted: Mapped[int] = mapped_column(Integer, default=0)
    #: Content hash used to detect material changes between runs.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    #: True once the job has appeared in any notification.
    notified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_run_id: Mapped[int | None] = mapped_column(Integer, index=True)

    listings: Mapped[list[JobListing]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    @property
    def source_count(self) -> int:
        return len(self.listings)

    @property
    def source_names(self) -> list[str]:
        return sorted({listing.source for listing in self.listings})


class JobListing(Base, TimestampMixin):
    """One website advertisement of a canonical job (the source mapping)."""

    __tablename__ = "job_listings"
    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_listing_source_id"),
        Index("ix_job_listings_job", "job_id"),
        Index("ix_job_listings_url_hash", "canonical_url_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(40), default="unknown")
    source_job_id: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000))
    canonical_url: Mapped[str | None] = mapped_column(String(1000))
    canonical_url_hash: Mapped[str | None] = mapped_column(String(64))
    apply_url: Mapped[str | None] = mapped_column(String(1000))
    ats_identity: Mapped[str | None] = mapped_column(String(500), index=True)

    title_raw: Mapped[str | None] = mapped_column(String(500))
    company_raw: Mapped[str | None] = mapped_column(String(300))
    location_raw: Mapped[str | None] = mapped_column(String(400))
    date_posted: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: How this listing merged in: exact_id, url, ats_identity, fingerprint,
    #: similarity, or llm.
    merge_method: Mapped[str | None] = mapped_column(String(40))
    merge_confidence: Mapped[float | None] = mapped_column(Float)
    raw_payload: Mapped[dict | None] = mapped_column(JSON)

    job: Mapped[Job] = relationship(back_populates="listings")


class JobEvent(Base):
    """Audit trail of everything that happened to a job."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job_created", "job_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    #: discovered, updated, reposted, expired, status_changed, notified, merged
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[dict | None] = mapped_column(JSON)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    job: Mapped[Job] = relationship(back_populates="events")


class DedupDecision(Base):
    """Record of non-trivial merge decisions, for debuggability and tuning."""

    __tablename__ = "dedup_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    left_key: Mapped[str] = mapped_column(String(500), nullable=False)
    right_key: Mapped[str] = mapped_column(String(500), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    #: same, different, or uncertain
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
