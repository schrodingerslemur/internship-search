"""Declarative base, shared mixins, and domain enumerations."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, String, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column

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


# --------------------------------------------------------------------------
# Oversized-value guard
#
# SQLite ignores VARCHAR(n) limits; PostgreSQL enforces them. A job title or
# location longer than its column therefore passes every local test and then
# aborts an entire production run -- and because the whole batch is one flush,
# a single freak listing loses every other job found that day.
#
# Free text is clamped to fit. Identity values are NOT: truncating two distinct
# requisition ids down to a shared prefix would make the deduplicator treat
# different openings as the same job, which is the one thing it must never do.
# Those columns are given generous lengths in the schema instead.
# --------------------------------------------------------------------------

#: Never truncated -- equality on these decides whether two listings are one job.
IDENTITY_COLUMNS: frozenset[str] = frozenset(
    {
        "ats_identity",
        "board_token",
        "canonical_job_id",
        "canonical_url",
        "canonical_url_hash",
        "content_hash",
        "fingerprint",
        "left_key",
        "requisition_id",
        "right_key",
        "slug",
        "source_job_id",
    }
)

#: mapper -> ((attribute, limit), ...), computed once per mapper.
_CLAMPABLE: dict[Mapper, tuple[tuple[str, int], ...]] = {}


def _clampable_columns(mapper: Mapper) -> tuple[tuple[str, int], ...]:
    cached = _CLAMPABLE.get(mapper)
    if cached is not None:
        return cached

    found = []
    for prop in mapper.column_attrs:
        column = prop.columns[0]
        if column.key in IDENTITY_COLUMNS or column.name in IDENTITY_COLUMNS:
            continue
        if isinstance(column.type, String) and column.type.length:
            found.append((prop.key, column.type.length))
    result = tuple(found)
    _CLAMPABLE[mapper] = result
    return result


def _clamp_oversized_strings(mapper: Mapper, _connection, target) -> None:
    for attribute, limit in _clampable_columns(mapper):
        value = getattr(target, attribute, None)
        if isinstance(value, str) and len(value) > limit:
            # Ellipsis rather than a hard cut, so a truncated value is
            # visibly truncated in the dashboard rather than silently wrong.
            setattr(target, attribute, value[: limit - 1] + "…")


# Listening on the Mapper class itself covers every mapped class, including
# ones defined later; `propagate` is only meaningful for a single-class target.
event.listen(Mapper, "before_insert", _clamp_oversized_strings)
event.listen(Mapper, "before_update", _clamp_oversized_strings)


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
