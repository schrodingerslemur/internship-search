"""User actions on jobs: save, dismiss, apply, status transitions, notes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Application, ApplicationNote, Job, JobEvent
from app.models.base import JobStatus, utcnow

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


def get_or_create_application(session: Session, job: Job) -> Application:
    application = session.scalar(select(Application).where(Application.job_id == job.id))
    if application is None:
        application = Application(job_id=job.id, status=job.status, deadline=job.deadline)
        session.add(application)
        session.flush()
    return application


def set_status(
    session: Session, job: Job, status: JobStatus, *, now: datetime | None = None
) -> Application:
    """Move a job to a new tracker state, stamping the relevant date."""
    now = now or utcnow()
    previous = job.status
    job.status = status.value

    application = get_or_create_application(session, job)
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
    log.info("actions.status", job_id=job.id, status=status.value)
    return application


def add_note(session: Session, job: Job, body: str) -> ApplicationNote | None:
    body = (body or "").strip()
    if not body:
        return None
    application = get_or_create_application(session, job)
    note = ApplicationNote(application_id=application.id, body=body, created_at=utcnow())
    session.add(note)
    session.flush()
    return note


def update_application_fields(session: Session, job: Job, data: dict) -> Application:
    """Update tracker metadata (resume used, contact, referral, follow-up)."""
    application = get_or_create_application(session, job)
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
