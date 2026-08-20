"""User actions on jobs: save, dismiss, apply, status transitions, notes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Application, ApplicationNote, Job, JobEvent, User
from app.models.base import JobStatus, utcnow
from app.services import user_jobs

log = get_logger("actions")

#: Which timestamp on the Application row each status stamps.
STATUS_DATE_FIELD: dict[str, str] = {
    JobStatus.SAVED.value: "date_saved",
    JobStatus.APPLIED.value: "date_applied",
    JobStatus.ASSESSMENT.value: "date_assessment",
    JobStatus.INTERVIEW.value: "date_interview",
    JobStatus.OFFER.value: "date_offer",
    JobStatus.REJECTED.value: "date_rejected",
}


def get_or_create_application(session: Session, job: Job, user: User) -> Application:
    application = session.scalar(
        select(Application).where(
            Application.job_id == job.id, Application.user_id == user.id
        )
    )
    if application is None:
        state = user_jobs.get_state(session, user, job)
        application = Application(
            user_id=user.id,
            job_id=job.id,
            status=user_jobs.status_of(state),
            deadline=job.deadline,
        )
        session.add(application)
        session.flush()
    return application


def set_status(
    session: Session,
    job: Job,
    status: JobStatus,
    user: User,
    *,
    now: datetime | None = None,
) -> Application:
    """Move a job to a new tracker state for this user, stamping the date.

    The decision is recorded against the user, never against the shared job
    row: someone else applying to a posting must not silence it for you.
    """
    now = now or utcnow()
    previous = user_jobs.status_of(user_jobs.get_state(session, user, job))
    user_jobs.set_status(session, user, job, status.value, now=now)

    application = get_or_create_application(session, job, user)
    application.status = status.value

    field = STATUS_DATE_FIELD.get(status.value)
    if field and getattr(application, field, None) is None:
        setattr(application, field, now)

    if status is JobStatus.APPLIED and application.score_at_apply is None:
        # Freeze the score so outcome analytics are not distorted by re-scoring.
        application.score_at_apply = job.relevance_score

    session.add(
        JobEvent(
            job_id=job.id,
            event_type="status_changed",
            detail=f"{previous} -> {status.value}",
            created_at=now,
        )
    )
    session.flush()
    log.info("actions.status", job_id=job.id, user_id=user.id, status=status.value)
    return application


def add_note(session: Session, job: Job, body: str, user: User) -> ApplicationNote | None:
    body = (body or "").strip()
    if not body:
        return None
    application = get_or_create_application(session, job, user)
    note = ApplicationNote(application_id=application.id, body=body, created_at=utcnow())
    session.add(note)
    session.flush()
    return note


def update_application_fields(
    session: Session, job: Job, data: dict, user: User
) -> Application:
    """Update tracker metadata (resume used, contact, referral, follow-up)."""
    application = get_or_create_application(session, job, user)
    for name in (
        "resume_version",
        "cover_letter_version",
        "contact_name",
        "contact_email",
        "referral",
    ):
        if name in data and data[name] is not None:
            setattr(application, name, str(data[name]).strip() or None)
    if data.get("follow_up_at"):
        from app.pipeline.extract import parse_date

        application.follow_up_at = parse_date(data["follow_up_at"])
    if data.get("resume_id"):
        try:
            application.resume_id = int(data["resume_id"])
        except (TypeError, ValueError):
            pass
    session.flush()
    return application


def get_job(session: Session, job_id: int | str) -> Job | None:
    """Look up a job by numeric id or canonical id."""
    if isinstance(job_id, int) or str(job_id).isdigit():
        job = session.get(Job, int(job_id))
        if job is not None:
            return job
    return session.scalar(select(Job).where(Job.canonical_job_id == str(job_id)))
