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
#: Submitted, and Greenhouse emailed a security code: the worker holds the
#: form open while the user types the code into the dashboard.
WAITING_CODE = "waiting_code"

OPEN_STATUSES = (QUEUED, RUNNING, WAITING_CODE)


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
    #: greenhouse:{board}:{id} -- the form, independent of which listing found it --
    #: or agent:{format} for a form the general agent works out (app/apply/agent.py).
    form_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Where the general agent starts. Greenhouse forms are addressed by form_key.
    apply_url: Mapped[str | None] = mapped_column(Text)
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
    def by_agent(self) -> bool:
        return not self.form_key.startswith("greenhouse:")

    @property
    def form_url(self) -> str:
        from app.apply.greenhouse import GreenhouseJob

        if self.by_agent:
            return self.apply_url or ""
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


class ApplyRecipe(Base, TimestampMixin):
    """How to apply on one format (Lever, Workday, a company's own site), in
    the agent's own words.

    Written by the agent after every attempt on that format and read before the
    next one, so a format is worked out once rather than on every form. Shared
    across users: it describes the site, never the applicant.
    """

    __tablename__ = "apply_recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: app.apply.formats.format_of key: "lever", "workday", "site:nvidia.com".
    format_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Step-by-step how-to, rewritten by the agent after each attempt.
    notes: Mapped[str] = mapped_column(Text, default="")
    successes: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    #: submitted, needs_you, failed: how the latest attempt ended.
    last_outcome: Mapped[str | None] = mapped_column(String(20))
    last_url: Mapped[str | None] = mapped_column(Text)


class ApplicantDocument(Base, TimestampMixin):
    """A file beyond the resume that some forms require: a transcript, a
    cover letter. One per kind; the worker attaches it wherever a form's
    upload asks for that kind, or for one of the exact questions in ``labels``.
    """

    __tablename__ = "applicant_documents"
    __table_args__ = (UniqueConstraint("user_id", "kind", name="uq_applicant_document_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: One of app.apply.answers.DOCUMENT_KINDS.
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Question labels this file was uploaded to answer, matched exactly, for
    #: uploads whose wording names no kind ("Additional attachment").
    labels: Mapped[list] = mapped_column(JSON, default=list)
