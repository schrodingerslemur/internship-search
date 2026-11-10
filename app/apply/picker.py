"""Which jobs to prepare: the best open matches that have a Greenhouse form."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.apply.greenhouse import GreenhouseJob, from_listing
from app.apply.profile import ApplicantProfile
from app.models import Job, User, UserJobState
from app.models.base import Priority, utcnow
from app.services.user_jobs import ACTED_ON

#: Only jobs the ranker is confident about. Anything weaker is the digest's
#: business, where a person decides; this acts on their behalf.
ELIGIBLE_PRIORITIES = (Priority.APPLY_NOW.value, Priority.STRONG_MATCH.value)


@dataclass
class Candidate:
    job: Job
    form: GreenhouseJob
    score: float


def greenhouse_form(job: Job) -> GreenhouseJob | None:
    for listing in job.listings:
        found = from_listing(
            listing.source, listing.source_job_id, [listing.apply_url, listing.url]
        )
        if found:
            return found
    return from_listing("", None, [job.application_url, job.posting_url])


def pick(
    session: Session,
    user: User,
    profile: ApplicantProfile,
    *,
    limit: int,
    skip_keys: set[str],
) -> list[Candidate]:
    if limit <= 0:
        return []
    state = UserJobState
    priority = func.coalesce(state.priority, Job.priority)
    score = func.coalesce(state.relevance_score, Job.relevance_score)
    now = utcnow()

    rows = session.execute(
        select(Job, score)
        .outerjoin(state, and_(state.job_id == Job.id, state.user_id == user.id))
        .where(
            Job.is_active.is_(True),
            priority.in_(ELIGIBLE_PRIORITIES),
            func.coalesce(state.status, "new").not_in(sorted(ACTED_ON)),
        )
        .order_by(score.desc())
        .limit(400)
    ).all()

    out: list[Candidate] = []
    for job, job_score in rows:
        if job.deadline and job.deadline < now:
            continue
        if profile.excluded(job.company_name):
            continue
        form = greenhouse_form(job)
        if form is None or form.key in skip_keys:
            continue
        out.append(Candidate(job=job, form=form, score=float(job_score or 0)))
        if len(out) >= limit:
            break
    return out
