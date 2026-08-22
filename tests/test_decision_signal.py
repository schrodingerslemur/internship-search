"""What the system remembers about a decision, beyond the decision itself.

Nothing here changes what the user sees today. It exists because the learning
layers cannot be built retroactively: a dismissal recorded without a reason, a
save recorded without the score it was made against, and a feed rendered
without a record of what it showed are all permanently unlearnable. These tests
pin the capture, not the consumption.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import FeedImpression, Job
from app.models.base import JobStatus
from app.services import auth, user_jobs


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def account(session):
    user = auth.create_account(session, email="me@example.com", password="a-good-password")
    session.flush()
    return user


@pytest.fixture
def signed_in(client, account):
    client.post("/login", data={"email": "me@example.com", "password": "a-good-password"})
    return client


def make_job(
    session, *, n: int = 1, title: str = "FPGA Design Intern", company: str = "NVIDIA", score: float = 90.0
) -> Job:
    job = Job(
        canonical_job_id=f"job-{n}",
        fingerprint=f"fp-{n}",
        company_name=company,
        title=title,
        location_raw="Santa Clara, CA",
        application_url=f"https://example.com/apply/{n}",
        relevance_score=score,
        priority="strong_match",
    )
    session.add(job)
    session.flush()
    return job


class TestADismissalRecordsWhy:
    def test_a_reason_given_with_the_dismissal_is_stored(self, session, account):
        job = make_job(session)
        user_jobs.set_status(
            session, account, job, JobStatus.DISMISSED.value, reason="wrong_location"
        )
        state = user_jobs.get_state(session, account, job)
        assert state.dismiss_reason == "wrong_location"

    def test_a_reason_can_be_added_after_the_fact(self, session, account):
        """The chips appear in the undo toast, once the job is already gone."""
        job = make_job(session)
        user_jobs.set_status(session, account, job, JobStatus.DISMISSED.value)
        assert user_jobs.get_state(session, account, job).dismiss_reason is None

        user_jobs.set_dismiss_reason(session, account, job, "too_senior")
        assert user_jobs.get_state(session, account, job).dismiss_reason == "too_senior"

    def test_an_unrecognised_reason_is_discarded_rather_than_stored(self, session, account):
        """A stale form must not be able to invent a category to learn from."""
        job = make_job(session)
        user_jobs.set_status(session, account, job, JobStatus.DISMISSED.value, reason="because")
        assert user_jobs.get_state(session, account, job).dismiss_reason is None

    def test_a_reason_is_not_recorded_for_a_job_that_is_not_dismissed(self, session, account):
        job = make_job(session)
        user_jobs.set_status(session, account, job, JobStatus.SAVED.value)
        assert user_jobs.set_dismiss_reason(session, account, job, "bad_timing") is None
        assert user_jobs.get_state(session, account, job).dismiss_reason is None

    def test_restoring_a_job_clears_the_reason(self, session, account):
        """Undo has to undo. A reason left behind would teach from a decision
        the user explicitly took back."""
        job = make_job(session)
        user_jobs.set_status(
            session, account, job, JobStatus.DISMISSED.value, reason="wrong_role"
        )
        user_jobs.set_status(session, account, job, JobStatus.NEW.value)
        state = user_jobs.get_state(session, account, job)
        assert state.dismiss_reason is None
        assert state.dismissed_at is None

    def test_the_chips_are_offered_when_a_job_is_dismissed(self, signed_in, session):
        job = make_job(session)
        page = signed_in.post(
            f"/job/{job.id}/status",
            data={"status": "dismissed", "view": "review"},
            headers={"HX-Request": "true"},
        )
        assert "Wrong location" in page.text
        assert f"/job/{job.id}/dismiss-reason" in page.text

    def test_the_chips_are_not_offered_for_any_other_decision(self, signed_in, session):
        job = make_job(session)
        page = signed_in.post(
            f"/job/{job.id}/status",
            data={"status": "saved", "view": "review"},
            headers={"HX-Request": "true"},
        )
        assert "Wrong location" not in page.text

    def test_tapping_a_chip_records_it(self, signed_in, session, account):
        job = make_job(session)
        signed_in.post(f"/job/{job.id}/status", data={"status": "dismissed", "view": "review"})
        reply = signed_in.post(
            f"/job/{job.id}/dismiss-reason", data={"reason": "no_sponsorship"}
        )
        assert reply.status_code == 200
        session.expire_all()
        assert user_jobs.get_state(session, account, job).dismiss_reason == "no_sponsorship"


class TestADecisionRemembersTheScoreItWasMadeAgainst:
    """Re-scoring rewrites ``relevance_score`` in place. Without a snapshot,
    every past decision silently re-attributes itself to whatever the ranker
    believes today."""

    def test_saving_snapshots_the_score(self, session, account):
        job = make_job(session, score=84.0)
        user_jobs.set_status(session, account, job, JobStatus.SAVED.value)
        assert user_jobs.get_state(session, account, job).score_at_save == 84.0

    def test_dismissing_snapshots_the_score(self, session, account):
        job = make_job(session, score=41.0)
        user_jobs.set_status(session, account, job, JobStatus.DISMISSED.value)
        assert user_jobs.get_state(session, account, job).score_at_dismiss == 41.0

    def test_the_snapshot_survives_a_rescore(self, session, account):
        job = make_job(session, score=84.0)
        user_jobs.set_status(session, account, job, JobStatus.SAVED.value)

        job.relevance_score = 12.0
        session.flush()

        assert user_jobs.get_state(session, account, job).score_at_save == 84.0

    def test_a_bulk_decision_snapshots_too(self, session, account):
        """The bulk path builds state rows that have not been flushed, so it
        cannot reach the job through the relationship the single path uses."""
        jobs = [make_job(session, n=i, score=30.0 + i) for i in range(3)]
        user_jobs.bulk_set_status(
            session, account, [j.id for j in jobs], JobStatus.DISMISSED.value
        )
        for job in jobs:
            state = user_jobs.get_state(session, account, job)
            assert state.score_at_dismiss == job.relevance_score

    def test_the_snapshot_is_written_once_and_not_overwritten(self, session, account):
        """What the ranker thought the first time does not change later."""
        job = make_job(session, score=84.0)
        user_jobs.set_status(session, account, job, JobStatus.SAVED.value)
        user_jobs.set_status(session, account, job, JobStatus.NEW.value)
        job.relevance_score = 20.0
        user_jobs.set_status(session, account, job, JobStatus.SAVED.value)
        assert user_jobs.get_state(session, account, job).score_at_save == 84.0


class TestTheFeedRecordsWhatItShowed:
    """Decisions have no denominator without this, and precision@k is undefined
    without a denominator."""

    def _impressions(self, session) -> list[FeedImpression]:
        from sqlalchemy import select

        return list(session.scalars(select(FeedImpression)).all())

    def test_viewing_the_feed_records_an_impression(self, signed_in, session):
        make_job(session)
        signed_in.get("/")
        rows = self._impressions(session)
        assert len(rows) == 1
        assert rows[0].view == "review"

    def test_the_impression_names_the_jobs_that_were_shown(self, signed_in, session):
        jobs = [make_job(session, n=i) for i in range(3)]
        signed_in.get("/")
        shown = self._impressions(session)[0]
        assert set(shown.job_ids) == {j.id for j in jobs}

    def test_the_impression_keeps_the_scores_as_displayed(self, signed_in, session):
        make_job(session, score=77.0)
        signed_in.get("/")
        shown = self._impressions(session)[0]
        assert shown.scores == [77.0]

    def test_scores_line_up_positionally_with_the_ids(self, signed_in, session):
        for i in range(3):
            make_job(session, n=i, score=50.0 + i * 10)
        signed_in.get("/")
        shown = self._impressions(session)[0]
        by_id = dict(zip(shown.job_ids, shown.scores, strict=True))
        for job_id, score in by_id.items():
            assert session.get(Job, job_id).relevance_score == score

    def test_an_empty_feed_records_nothing(self, signed_in, session):
        signed_in.get("/")
        assert self._impressions(session) == []

    def test_each_view_is_recorded_under_its_own_name(self, signed_in, session):
        job = make_job(session)
        signed_in.post(f"/job/{job.id}/status", data={"status": "saved", "view": "review"})
        signed_in.get("/saved")
        assert any(row.view == "saved" for row in self._impressions(session))
