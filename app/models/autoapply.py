"""Auto-apply: the queue a right swipe adds to, and who is applying.

Both live in the database because the worker runs in the cloud (GitHub
Actions) and the dashboard runs somewhere else again; the database is the one
place both can reach. It is private and already holds the account, so the
applicant's contact details and resume are no more exposed here than they
would be in a CI secret -- which could not hold a PDF anyway (48 KB limit).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

QUEUED = "queued"
RUNNING = "running"
SUBMITTED = "submitted"
#: The worker stopped short and handed the application back: a question it
#: would not guess, a bot check, a form that refused the submission.
NEEDS_YOU = "needs_you"
#: The posting is gone, or the form could not be read at all.
FAILED = "failed"
#: The user withdrew the swipe before the worker reached it.
CANCELLED = "cancelled"

OPEN_STATUSES = (QUEUED, RUNNING)


class ApplyTask(Base, TimestampMixin):
    """One approved application, from swipe to submission."""

    __tablename__ = "apply_tasks"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_apply_task_user_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: greenhouse:{board}:{id} -- the form, independent of which listing found it.
    form_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: One human sentence: why it stopped, or what happened.
    detail: Mapped[str | None] = mapped_column(Text)
    #: Required questions the worker would not answer on the user's behalf.
    left_for_you: Mapped[list] = mapped_column(JSON, default=list)
    #: Exactly what was entered, so the user can see what went out in their name.
    answers: Mapped[list] = mapped_column(JSON, default=list)
    #: JPEG of the page when the worker finished with it.
    screenshot: Mapped[bytes | None] = mapped_column(LargeBinary)

    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    job: Mapped["Job"] = relationship()  # noqa: F821, UP037 - Job is in another module

    @property
    def form_url(self) -> str:
        from app.apply.greenhouse import GreenhouseJob

        _, token, job_id = self.form_key.split(":", 2)
        return GreenhouseJob(token, job_id).form_url


class ApplicantRecord(Base, TimestampMixin):
    """The applicant profile and resume the cloud worker applies with."""

    __tablename__ = "applicant_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    #: The ApplicantProfile fields (app/apply/profile.py), minus resume_path.
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    resume_filename: Mapped[str | None] = mapped_column(String(300))
    resume_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
