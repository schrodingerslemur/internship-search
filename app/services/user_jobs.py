"""Per-user job state.

A job is a shared fact; what you have done about it is not. Status and
notification history live in ``UserJobState`` so that two people searching from
the same instance never affect each other's tracker or digests.

State rows are created lazily. A job nobody has touched needs no row, so the
table stays proportional to decisions made rather than to jobs crawled.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, User, UserJobState
from app.models.base import JobStatus, utcnow

#: Statuses that mean the user has dealt with this job: it stops appearing in
#: digests, but is never deleted -- the tracker is the record of what you did.
ACTED_ON: frozenset[str] = frozenset(
    {
        JobStatus.DISMISSED.value,
        JobStatus.APPLIED.value,
        JobStatus.ASSESSMENT.value,
        JobStatus.INTERVIEW.value,
        JobStatus.OFFER.value,
        JobStatus.REJECTED.value,
    }
)

#: Statuses that survive the staleness sweep: your application history must
#: outlive the posting it refers to.
PROTECTED_FROM_EXPIRY: frozenset[str] = frozenset(
    {
        JobStatus.SAVED.value,
        JobStatus.APPLIED.value,
        JobStatus.ASSESSMENT.value,
        JobStatus.INTERVIEW.value,
        JobStatus.OFFER.value,
    }
)


def get_state(session: Session, user: User, job: Job | int) -> UserJobState | None:
    job_id = job if isinstance(job, int) else job.id
    return session.scalar(
        select(UserJobState).where(
            UserJobState.user_id == user.id, UserJobState.job_id == job_id
        )
    )


def get_or_create_state(session: Session, user: User, job: Job | int) -> UserJobState:
    job_id = job if isinstance(job, int) else job.id
    state = get_state(session, user, job_id)
    if state is None:
        state = UserJobState(user_id=user.id, job_id=job_id, status=JobStatus.NEW.value)
        session.add(state)
        session.flush()
    return state


def states_for(session: Session, user: User, job_ids: list[int]) -> dict[int, UserJobState]:
    """Bulk fetch, so a job list costs one query rather than one per row."""
    if not job_ids:
        return {}
    rows = session.scalars(
        select(UserJobState).where(
            UserJobState.user_id == user.id, UserJobState.job_id.in_(job_ids)
        )
    ).all()
    return {row.job_id: row for row in rows}


def set_status(
    session: Session,
    user: User,
    job: Job,
    status: str,
    *,
    now: datetime | None = None,
) -> UserJobState:
    """Record what this user has decided about this job."""
    now = now or utcnow()
    state = get_or_create_state(session, user, job)
    state.status = status
    if status == JobStatus.SAVED.value and state.saved_at is None:
        state.saved_at = now
    if status == JobStatus.APPLIED.value and state.applied_at is None:
        state.applied_at = now
    session.flush()
    return state


def mark_notified(
    session: Session, user: User, job: Job, *, now: datetime | None = None
) -> UserJobState:
    now = now or utcnow()
    state = get_or_create_state(session, user, job)
    state.notified = True
    state.notified_at = now
    session.flush()
    return state


def status_of(state: UserJobState | None) -> str:
    return state.status if state is not None else JobStatus.NEW.value


def has_acted_on(state: UserJobState | None) -> bool:
    return status_of(state) in ACTED_ON


def acted_on_job_ids(session: Session, user: User) -> set[int]:
    """Jobs this user has dealt with, and so should not be alerted about."""
    rows = session.scalars(
        select(UserJobState.job_id).where(
            UserJobState.user_id == user.id, UserJobState.status.in_(sorted(ACTED_ON))
        )
    ).all()
    return set(rows)


def notified_job_ids(session: Session, user: User) -> set[int]:
    rows = session.scalars(
        select(UserJobState.job_id).where(
            UserJobState.user_id == user.id, UserJobState.notified.is_(True)
        )
    ).all()
    return set(rows)


def status_counts(session: Session, user: User) -> dict[str, int]:
    """Tracker column sizes for this user."""
    from sqlalchemy import func

    rows = session.execute(
        select(UserJobState.status, func.count(UserJobState.id))
        .where(UserJobState.user_id == user.id)
        .group_by(UserJobState.status)
    ).all()
    return {status: count for status, count in rows}


def score_jobs_for_user(
    session: Session,
    user: User,
    jobs: list[Job],
    prefs,
    profile,
    *,
    now: datetime | None = None,
) -> int:
    """Score these jobs against one user's profile, storing the result.

    Scoring is pure CPU over data already in memory, so doing it once per
    account is cheap -- far cheaper than crawling the boards again, which is
    what a second instance per person would cost.
    """
    from app.pipeline.match import score_job
    from app.schemas.job import normalized_from_job_row

    now = now or utcnow()
    if not jobs:
        return 0

    existing = states_for(session, user, [j.id for j in jobs])
    scored = 0

    for job in jobs:
        try:
            candidate = normalized_from_job_row(job)
        except Exception:
            # A malformed stored row must not stop the other jobs being scored.
            continue
        result = score_job(candidate, prefs, profile, now=now)

        state = existing.get(job.id)
        if state is None:
            state = UserJobState(user_id=user.id, job_id=job.id, status=JobStatus.NEW.value)
            session.add(state)
            existing[job.id] = state

        state.relevance_score = result.score
        state.priority = str(result.priority)
        state.match_reasons = result.match_reasons
        state.concerns = result.concerns
        state.missing_requirements = result.missing_requirements
        state.score_breakdown = result.breakdown()
        state.scored_at = now
        scored += 1

    session.flush()
    return scored


def score_of(state: UserJobState | None, job: Job) -> float:
    """This user's score, falling back to the shared one when unscored."""
    if state is not None and state.relevance_score is not None:
        return state.relevance_score
    return job.relevance_score or 0.0


def priority_of(state: UserJobState | None, job: Job) -> str:
    if state is not None and state.priority:
        return state.priority
    return job.priority
