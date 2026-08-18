"""Declarative base, shared mixins, and domain enumerations."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class StrEnum(str, enum.Enum):  # noqa: UP042 - enum.StrEnum needs 3.11+ semantics we do not want
    """String-valued enum that serialises as its value.

    Deliberately not ``enum.StrEnum``: these values are persisted, and mixing
    in ``str`` explicitly keeps comparison behaviour identical across the
    Python versions this app supports.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class RemoteStatus(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    INTERNSHIP = "internship"
    CO_OP = "co_op"
    NEW_GRAD = "new_grad"
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    UNKNOWN = "unknown"


class SponsorshipStatus(StrEnum):
    """Never inferred. UNKNOWN is the default and is not the same as NOT_OFFERED."""

    OFFERED = "offered"
    NOT_OFFERED = "not_offered"
    CITIZENSHIP_REQUIRED = "citizenship_required"
    SECURITY_CLEARANCE_REQUIRED = "security_clearance_required"
    UNKNOWN = "unknown"


class Priority(StrEnum):
    APPLY_NOW = "apply_now"
    STRONG_MATCH = "strong_match"
    WORTH_CONSIDERING = "worth_considering"
    MAYBE = "maybe"
    SKIP = "skip"

    @property
    def emoji(self) -> str:
        return {
            "apply_now": "\U0001f525",
            "strong_match": "⭐",
            "worth_considering": "\U0001f44d",
            "maybe": "\U0001f7e1",
            "skip": "❌",
        }[self.value]

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class JobStatus(StrEnum):
    """User-facing pipeline state for a canonical job."""

    NEW = "new"
    SAVED = "saved"
    APPLIED = "applied"
    ASSESSMENT = "assessment"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    DISMISSED = "dismissed"
    EXPIRED = "expired"


#: Ordered Kanban columns for the application tracker.
KANBAN_ORDER: tuple[JobStatus, ...] = (
    JobStatus.NEW,
    JobStatus.SAVED,
    JobStatus.APPLIED,
    JobStatus.ASSESSMENT,
    JobStatus.INTERVIEW,
    JobStatus.OFFER,
)

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.REJECTED, JobStatus.DISMISSED, JobStatus.EXPIRED}
)


class Freshness(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    REPOSTED = "reposted"
    OLD = "old"
    EXPIRED = "expired"


class SourceKind(StrEnum):
    """Determines canonical-URL preference. Order matters -- see url_rank()."""

    COMPANY_CAREERS = "company_careers"
    ATS = "ats"
    JOB_BOARD = "job_board"
    AGGREGATOR = "aggregator"
    CURATED_LIST = "curated_list"
    UNKNOWN = "unknown"


#: Lower rank == more authoritative application URL (requirement: canonical URL).
SOURCE_KIND_RANK: dict[str, int] = {
    SourceKind.COMPANY_CAREERS.value: 0,
    SourceKind.ATS.value: 1,
    SourceKind.JOB_BOARD.value: 2,
    SourceKind.AGGREGATOR.value: 3,
    SourceKind.CURATED_LIST.value: 4,
    SourceKind.UNKNOWN.value: 5,
}


class SourceHealth(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNCONFIGURED = "unconfigured"
    DISABLED = "disabled"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class NotificationKind(StrEnum):
    MORNING_DIGEST = "morning_digest"
    AFTERNOON_DIGEST = "afternoon_digest"
    UPDATE_ALERT = "update_alert"
    DEADLINE_ALERT = "deadline_alert"
    MANUAL = "manual"
    TEST = "test"
