"""A search run re-scores what it touched, and the whole corpus once a day.

Re-scoring everything every run read every posting's full text out of the
hosted database eight times a day, which used up its monthly transfer
allowance in two weeks.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models import Job, UserJobState
from app.schemas.preferences import default_preferences
from app.schemas.profile import default_profile
from app.services import auth, user_jobs

NOW = datetime(2026, 10, 2, 12, 0)


def _jobs(session, n=3):
    jobs = [
        Job(
            canonical_job_id=f"j-{i}",
            fingerprint=f"fp-{i}",
            company_name="Acme",
            title="Software Engineering Intern",
            title_core="software engineering intern",
            application_url=f"https://example.com/{i}",
            description="Python and SQL for backend services.",
            locations=[],
        )
        for i in range(n)
    ]
    session.add_all(jobs)
    session.flush()
    return jobs


def _scored_at(session, user, job):
    state = session.query(UserJobState).filter_by(user_id=user.id, job_id=job.id).one()
    return state.scored_at


def _run(session, user, ids, now):
    return user_jobs.rescore_for_run(
        session, user, default_preferences(), default_profile(), ids, now=now
    )


def test_first_run_scores_everything(session):
    user = auth.create_account(session, email="a@example.com", password="a-good-password")
    jobs = _jobs(session)
    assert _run(session, user, [], NOW) == len(jobs)


def test_recent_full_pass_scores_only_touched_jobs(session):
    user = auth.create_account(session, email="a@example.com", password="a-good-password")
    jobs = _jobs(session)
    _run(session, user, [], NOW)

    later = NOW + timedelta(hours=3)
    assert _run(session, user, [jobs[0].id], later) == 1
    assert _scored_at(session, user, jobs[0]) == later
    assert _scored_at(session, user, jobs[1]) == NOW


def test_stale_scores_trigger_a_full_pass(session):
    user = auth.create_account(session, email="a@example.com", password="a-good-password")
    jobs = _jobs(session)
    _run(session, user, [], NOW)

    later = NOW + user_jobs.FULL_RESCORE_INTERVAL
    assert _run(session, user, [], later) == len(jobs)
    assert all(_scored_at(session, user, j) == later for j in jobs)
