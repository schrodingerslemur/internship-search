"""Transport objects moving through the pipeline.

``RawJob``  -- what a source emits, minimally cleaned.
``NormalizedJob`` -- after normalisation: canonical URLs, parsed location,
                     employment type, salary, sponsorship, dedup keys.
``JobCluster`` -- a set of normalized jobs judged to be one underlying position.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.base import EmploymentType, RemoteStatus, SourceKind, SponsorshipStatus


class RawJob(BaseModel):
    """A listing exactly as a source reported it.

    Sources should populate whatever they have and leave the rest ``None``.
    Normalisation, not the source, is responsible for inference.
    """

    model_config = ConfigDict(extra="allow")

    source: str
    source_kind: SourceKind = SourceKind.UNKNOWN
    source_job_id: str
    title: str
    company: str
    url: str | None = None
    apply_url: str | None = None
    location: str | None = None
    locations: list[str] = Field(default_factory=list)
    description: str | None = None
    requirements: str | None = None
    responsibilities: str | None = None
    preferred_qualifications: str | None = None
    employment_type: str | None = None
    remote_status: str | None = None
    salary_raw: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    date_posted: datetime | None = None
    date_updated: datetime | None = None
    deadline: datetime | None = None
    requisition_id: str | None = None
    department: str | None = None
    terms: list[str] = Field(default_factory=list)
    sponsorship_hint: str | None = None
    company_url: str | None = None
    #: Untouched provider payload, retained for debugging and re-parsing.
    raw: dict[str, Any] = Field(default_factory=dict)


class NormalizedJob(BaseModel):
    """A listing after normalisation, carrying every dedup key."""

    model_config = ConfigDict(extra="forbid")

    # provenance
    source: str
    source_kind: SourceKind
    source_job_id: str

    # identity / dedup keys
    canonical_url: str | None = None
    canonical_url_hash: str | None = None
    ats_identity: str | None = None
    fingerprint: str = ""
    content_hash: str = ""

    # core content
    company: str
    company_slug: str
    title: str
    title_core: str
    #: Tokens that must agree before two postings may merge (e.g. verification
    #: vs design). Guards against over-deduplication.
    discriminators: frozenset[str] = frozenset()

    location_raw: str | None = None
    locations: list[str] = Field(default_factory=list)
    city: str | None = None
    state: str | None = None
    country: str | None = None
    location_key: str = ""
    remote_status: RemoteStatus = RemoteStatus.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN

    description: str | None = None
    requirements: str | None = None
    responsibilities: str | None = None
    preferred_qualifications: str | None = None
    #: Shingle set of the description, used for Jaccard similarity.
    description_shingles: frozenset[str] = frozenset()

    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_raw: str | None = None

    url: str | None = None
    apply_url: str | None = None
    date_posted: datetime | None = None
    date_updated: datetime | None = None
    deadline: datetime | None = None
    deadline_is_explicit: bool = False
    requisition_id: str | None = None
    terms: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    degree_requirements: list[str] = Field(default_factory=list)
    experience_required_years: float | None = None
    sponsorship: SponsorshipStatus = SponsorshipStatus.UNKNOWN
    sponsorship_evidence: str | None = None
    company_url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable within-run identity for logging and dedup bookkeeping."""
        return f"{self.source}:{self.source_job_id}"

    def text_blob(self) -> str:
        """All free text, for keyword scanning."""
        parts = [
            self.title,
            self.description or "",
            self.requirements or "",
            self.responsibilities or "",
            self.preferred_qualifications or "",
        ]
        return "\n".join(parts)


class JobCluster(BaseModel):
    """A group of listings resolved to one canonical job."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    members: list[NormalizedJob]
    #: Per-member merge provenance, keyed by ``NormalizedJob.key``.
    merge_methods: dict[str, str] = Field(default_factory=dict)
    merge_confidence: dict[str, float] = Field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def sources(self) -> list[str]:
        return sorted({m.source for m in self.members})


class SourceOutcome(BaseModel):
    """What one source did during a run. Feeds coverage and health reporting."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: str
    kind: SourceKind = SourceKind.UNKNOWN
    status: str = "ok"
    jobs: list[RawJob] = Field(default_factory=list)
    queries_run: int = 0
    sub_targets_attempted: int = 0
    sub_targets_successful: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    #: ATS boards / companies this source revealed for future crawling.
    discovered_boards: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def job_count(self) -> int:
        return len(self.jobs)
