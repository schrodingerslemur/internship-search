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

#: What the primary feed shows. A job you have not decided about yet, plus one
#: you deliberately kept: saving is a bookmark, not a disposal, so a saved job
#: stays in front of you until you apply or dismiss it.
NEEDS_REVIEW: frozenset[str] = frozenset(
    {JobStatus.NEW.value, JobStatus.SAVED.value}
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


#: status -> the column recording when it was first reached. First reached, not
#: last: "applied on the 3rd" must not become "applied today" because the row
#: was touched again.
STATUS_TIMESTAMP: dict[str, str] = {
    JobStatus.SAVED.value: "saved_at",
    JobStatus.APPLIED.value: "applied_at",
    JobStatus.DISMISSED.value: "dismissed_at",
}


def stamp_status(state: UserJobState, status: str, now: datetime) -> None:
    """Set the status and its arrival timestamp on an already-loaded row.

    Returning a job to NEW is a restore, and a restore must genuinely undo the
    dismissal: leaving ``dismissed_at`` set would keep the job in the Dismissed
    list forever, which is the one thing undo has to fix.
    """
    state.status = status
    if status == JobStatus.NEW.value:
        state.dismissed_at = None
    field = STATUS_TIMESTAMP.get(status)
    if field and getattr(state, field, None) is None:
        setattr(state, field, now)


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
    stamp_status(state, status, now)
    session.flush()
    return state


def mark_opened(
    session: Session, user: User, job: Job, *, now: datetime | None = None
) -> UserJobState:
    """Note that the user opened the application page -- nothing more.

    Opening a posting is not applying to it, and the status is left exactly as
    it was. All this buys is the right to ask "did you apply?" afterwards.
    """
    state = get_or_create_state(session, user, job)
    state.opened_at = now or utcnow()
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


def view_for(session: Session, user: User, jobs: list[Job]) -> dict[int, dict]:
    """Per-user presentation data for a page of jobs, in one query.

    The dashboard must show *your* score and *your* status, not whatever the
    shared row happens to hold, and it must do so without a query per card.
    """
    states = states_for(session, user, [j.id for j in jobs])

    def entry(job: Job) -> dict:
        state = states.get(job.id)
        return {
            "score": score_of(state, job),
            "priority": priority_of(state, job),
            "status": status_of(state),
            "reasons": (state.match_reasons if state else None) or job.match_reasons or [],
            "notified": bool(state.notified) if state else False,
            "saved_at": state.saved_at if state else None,
            "applied_at": state.applied_at if state else None,
            "dismissed_at": state.dismissed_at if state else None,
            # Opened but still undecided: the card asks whether it went through
            # rather than guessing from the click.
            "awaiting_answer": bool(
                state
                and state.opened_at
                and state.status in NEEDS_REVIEW
            ),
        }

    return {job.id: entry(job) for job in jobs}


def bulk_set_status(
    session: Session, user: User, job_ids: list[int], status: str, *, now: datetime | None = None
) -> int:
    """Apply one decision to many jobs at once. Returns how many changed.

    Clearing a screenful in one action is the difference between triaging a
    digest and abandoning it.
    """
    if not job_ids or status not in {s.value for s in JobStatus}:
        return 0

    now = now or utcnow()
    jobs = session.scalars(select(Job).where(Job.id.in_(job_ids))).all()
    existing = states_for(session, user, [j.id for j in jobs])

    changed = 0
    for job in jobs:
        state = existing.get(job.id)
        if state is None:
            state = UserJobState(user_id=user.id, job_id=job.id, status=JobStatus.NEW.value)
            session.add(state)
        if state.status == status:
            continue
        stamp_status(state, status, now)
        changed += 1

    session.flush()
    return changed
