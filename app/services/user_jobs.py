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

from app.logging_setup import get_logger
from app.models import Job, User, UserJobState
from app.models.base import JobStatus, utcnow

log = get_logger("user_jobs")

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


#: The six things "no" can mean, as (code, label) pairs shown on the card.
#:
#: They are not interchangeable: "wrong location" argues for widening the
#: location filter, "too senior" for tightening the seniority gate, and "not
#: this company" for neither. A dismissal recorded without one of these is a
#: data point that cannot be acted on, because every correction it might imply
#: contradicts another. Six is the most that fits on one row of chips and still
#: gets tapped; a seventh would be a dropdown, and a dropdown is a dialog.
DISMISS_REASONS: tuple[tuple[str, str], ...] = (
    ("wrong_role", "Wrong role"),
    ("wrong_location", "Wrong location"),
    ("too_senior", "Too senior"),
    ("no_sponsorship", "Won't sponsor"),
    ("not_this_company", "Not this company"),
    ("bad_timing", "Bad timing"),
)

DISMISS_REASON_CODES: frozenset[str] = frozenset(code for code, _ in DISMISS_REASONS)

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


def stamp_status(
    state: UserJobState, status: str, now: datetime, job: Job | None = None
) -> None:
    """Set the status and its arrival timestamp on an already-loaded row.

    Returning a job to NEW is a restore, and a restore must genuinely undo the
    dismissal: leaving ``dismissed_at`` set would keep the job in the Dismissed
    list forever, which is the one thing undo has to fix.

    ``job`` is the row the score is snapshotted from when the state itself has
    no per-user score yet. It is passed explicitly because the bulk path builds
    state rows that have not been flushed, and an unflushed row's ``job``
    relationship is empty -- so a bulk dismissal would silently record no score
    at all, which is precisely the decision signal this exists to keep.
    """
    state.status = status
    if status == JobStatus.NEW.value:
        # A restore undoes the dismissal completely, reason included: leaving
        # one behind would teach the learning layers from a decision the user
        # has explicitly taken back.
        state.dismissed_at = None
        state.dismiss_reason = None
    field = STATUS_TIMESTAMP.get(status)
    if field and getattr(state, field, None) is None:
        setattr(state, field, now)
    _snapshot_score(state, status, job)


#: Which snapshot column each decision fills. Applications are snapshotted by
#: the tracker (``Application.score_at_apply``); these are the two that were
#: missing.
SCORE_SNAPSHOT: dict[str, str] = {
    JobStatus.SAVED.value: "score_at_save",
    JobStatus.DISMISSED.value: "score_at_dismiss",
}


def _snapshot_score(state: UserJobState, status: str, job: Job | None = None) -> None:
    """Freeze the score this decision was made against.

    Written once and never overwritten: the question a snapshot answers is
    "what did the ranker think when the user chose this", and re-saving a job
    later does not change what it thought the first time.
    """
    field = SCORE_SNAPSHOT.get(status)
    if not field or getattr(state, field, None) is not None:
        return
    score = state.relevance_score
    if score is None:
        source = job if job is not None else state.job
        score = source.relevance_score if source is not None else None
    if score is not None:
        setattr(state, field, float(score))


def set_status(
    session: Session,
    user: User,
    job: Job,
    status: str,
    *,
    now: datetime | None = None,
    reason: str | None = None,
) -> UserJobState:
    """Record what this user has decided about this job.

    ``reason`` is only meaningful for a dismissal and is always optional --
    triage is never interrupted to collect it. An unrecognised code is dropped
    rather than stored, so a stale form cannot poison the signal.
    """
    now = now or utcnow()
    state = get_or_create_state(session, user, job)
    stamp_status(state, status, now, job)
    if status == JobStatus.DISMISSED.value and reason in DISMISS_REASON_CODES:
        state.dismiss_reason = reason
    session.flush()
    return state


def set_dismiss_reason(
    session: Session, user: User, job: Job, reason: str
) -> UserJobState | None:
    """Attach a reason to a dismissal that has already happened.

    Separate from :func:`set_status` because the reason is collected *after*
    the fact -- the chips appear in the undo toast, once the job is already
    gone from the list. Recording it must not resurrect a state row for a job
    that was never dismissed, so a missing row is left missing.
    """
    if reason not in DISMISS_REASON_CODES:
        return None
    state = get_state(session, user, job)
    if state is None or state.status != JobStatus.DISMISSED.value:
        return None
    state.dismiss_reason = reason
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


def rescore_all_for_user(
    session: Session,
    user: User,
    prefs=None,
    profile=None,
    *,
    now: datetime | None = None,
) -> int:
    """Re-score this user's whole active corpus against current preferences.

    Called whenever preferences or the profile change. Without it a stored
    score is a snapshot of the settings that were in force when the job was
    crawled: removing a target role changed what future searches looked for
    and left every job already in the database ranked as though the role were
    still wanted, which is indistinguishable from the setting being ignored.

    Scoring is pure CPU over rows already being loaded, so at corpus sizes in
    the low tens of thousands this is comfortably inline.
    """
    from app.services.preferences import load_preferences, load_profile

    jobs = list(session.scalars(select(Job).where(Job.is_active.is_(True))).all())
    if not jobs:
        return 0
    return score_jobs_for_user(
        session,
        user,
        jobs,
        prefs if prefs is not None else load_preferences(session, user=user),
        profile if profile is not None else load_profile(session, user=user),
        now=now,
    )


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
            # The breakdown, concerns and missing requirements have to come
            # from the same place the score did. Showing this user's score
            # beside the shared row's component bars is how a detail page ends
            # up explaining a number it is not displaying.
            "breakdown": (state.score_breakdown if state else None) or job.score_breakdown or {},
            "concerns": (state.concerns if state else None) or job.concerns or [],
            "missing_requirements": (
                (state.missing_requirements if state else None)
                or job.missing_requirements
                or []
            ),
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
        stamp_status(state, status, now, job)
        changed += 1

    session.flush()
    return changed


#: How many rows of a feed count as "shown". The list is paginated well below
#: this, so in practice it records the whole page; the cap exists so a future
#: longer page cannot write an unbounded row.
IMPRESSION_TOP_K = 50


def record_impression(
    session: Session,
    user: User,
    view: str,
    jobs: list[Job],
    *,
    total_available: int | None = None,
    now: datetime | None = None,
) -> None:
    """Log what this feed just put in front of the user.

    Decisions alone have no denominator: they say what was chosen but not what
    was on offer, and precision@k is undefined without both. This is the
    cheapest possible record of the offer -- ranked ids and their scores at
    display time, appended once per rendered feed.

    Never allowed to break a page. Rendering the feed is the user's actual
    goal; instrumenting it is not, so a failure here is swallowed rather than
    turning a working list into a 500.
    """
    if not jobs:
        return
    from app.models import FeedImpression

    top = jobs[:IMPRESSION_TOP_K]
    states = states_for(session, user, [j.id for j in top])
    try:
        # A savepoint, not a bare try: a failed flush leaves the session
        # unusable, and rolling the whole request back would discard the very
        # decision the user just made in order to save a log line about it.
        with session.begin_nested():
            session.add(
                FeedImpression(
                    user_id=user.id,
                    view=view,
                    shown_at=now or utcnow(),
                    job_ids=[j.id for j in top],
                    scores=[score_of(states.get(j.id), j) for j in top],
                    total_available=(
                        total_available if total_available is not None else len(jobs)
                    ),
                )
            )
    except Exception:  # pragma: no cover - instrumentation must not break a page
        log.warning("impression.not_recorded", user_id=user.id, view=view)
