"""Swipe to apply: the deck, the queue, the worker's verdicts, and the pages."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.apply import cloud, worker
from app.apply.answers import Answer, build_draft
from app.apply.profile import ApplicantProfile
from app.db import get_db
from app.main import create_app
from app.models import ApplyTask, Job, JobListing
from app.models.autoapply import FAILED, NEEDS_YOU, QUEUED, RUNNING, SUBMITTED
from app.models.base import Freshness, JobStatus, Priority, utcnow
from app.services import auth, user_jobs

PROFILE = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "555-0100",
    "standing_answers": [{"match": "offers?", "answer": "No"}],
}


@pytest.fixture
def account(session):
    user = auth.create_account(session, email="me@example.com", password="a-good-password")
    session.flush()
    return user


def make_job(session, n: int, *, score: float = 90.0, greenhouse: bool = True, **kwargs) -> Job:
    defaults = dict(
        canonical_job_id=f"job-{n}",
        fingerprint=f"fp-{n}",
        company_name=kwargs.pop("company", "Acme"),
        title=f"FPGA Intern {n}",
        application_url=f"https://example.com/apply/{n}",
        relevance_score=score,
        priority=Priority.STRONG_MATCH.value,
        status=JobStatus.NEW.value,
        freshness=Freshness.NEW.value,
        is_active=True,
        date_discovered=utcnow(),
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    session.add(job)
    session.flush()
    session.add(
        JobListing(
            job_id=job.id,
            source="greenhouse" if greenhouse else "lever",
            source_job_id=f"acme:{n}" if greenhouse else f"x{n}",
        )
    )
    session.flush()
    session.refresh(job)
    return job


# ------------------------------------------------------------------- deck


class TestDeck:
    def test_only_undecided_greenhouse_jobs_best_first(self, session, account):
        low = make_job(session, 1, score=70)
        high = make_job(session, 2, score=95)
        make_job(session, 3, greenhouse=False)
        decided = make_job(session, 4)
        user_jobs.set_status(session, account, decided, JobStatus.DISMISSED.value)
        make_job(session, 5, priority=Priority.SKIP.value)
        make_job(session, 6, is_active=False)

        ids = [c["id"] for c in cloud.deck(session, account)]
        assert ids == [high.id, low.id]

    def test_queued_and_blocked_jobs_leave_the_deck(self, session, account):
        queued = make_job(session, 1)
        make_job(session, 2, company="Never Inc")
        keep = make_job(session, 3)
        cloud.save_record(session, account, data={**PROFILE, "never_apply_to": ["never"]})
        cloud.enqueue(session, account, queued)
        assert [c["id"] for c in cloud.deck(session, account)] == [keep.id]

    def test_exclude_skips_cards_the_client_already_has(self, session, account):
        a = make_job(session, 1, score=95)
        b = make_job(session, 2, score=90)
        assert [c["id"] for c in cloud.deck(session, account, exclude={a.id})] == [b.id]

    def test_a_card_warns_when_you_applied_to_that_employer_before(self, session, account):
        done = make_job(session, 1, company="Acme")
        cloud.mark_applied_elsewhere(session, account, done)
        make_job(session, 2, company="Acme")
        make_job(session, 3, company="Other")
        cards = {c["company"]: c for c in cloud.deck(session, account)}
        assert cards["Acme"]["applied_before"] is True
        assert cards["Other"]["applied_before"] is False

    def test_only_postings_still_live_on_the_board_are_dealt(self, session, account):
        now = utcnow()
        evergreen = make_job(session, 1, date_posted=now - timedelta(days=400), last_seen_at=now)
        make_job(session, 2, date_posted=now - timedelta(days=5), last_seen_at=now - timedelta(days=9))
        never_seen_new = make_job(session, 3, date_posted=now - timedelta(days=10), relevance_score=80)
        make_job(session, 4, date_posted=now - timedelta(days=400))
        assert [c["id"] for c in cloud.deck(session, account)] == [evergreen.id, never_seen_new.id]

    def test_a_card_carries_only_what_it_shows(self, session, account):
        make_job(session, 1, description="x" * 5000, salary_min=None)
        card = cloud.deck(session, account)[0]
        assert len(card["summary"]) <= 361
        assert "Not listed" not in card["facts"]


# ------------------------------------------------------------------ queue


class TestQueue:
    def test_enqueue_saves_the_job_and_is_idempotent(self, session, account):
        job = make_job(session, 1)
        first = cloud.enqueue(session, account, job)
        again = cloud.enqueue(session, account, job)
        assert first.id == again.id
        assert first.status == QUEUED and first.form_key == "greenhouse:acme:1"
        assert user_jobs.status_of(user_jobs.get_state(session, account, job)) == JobStatus.SAVED.value

    def test_a_job_without_a_form_is_refused(self, session, account):
        assert cloud.enqueue(session, account, make_job(session, 1, greenhouse=False)) is None

    def test_cancel_only_while_queued(self, session, account):
        job = make_job(session, 1)
        task = cloud.enqueue(session, account, job)
        task.status = RUNNING
        assert cloud.cancel(session, account, job) is False
        task.status = QUEUED
        assert cloud.cancel(session, account, job) is True

    def test_claim_takes_the_oldest_and_reclaims_abandoned_work(self, session, account):
        old = cloud.enqueue(session, account, make_job(session, 1))
        new = cloud.enqueue(session, account, make_job(session, 2))
        old.queued_at = utcnow() - timedelta(hours=1)
        stale = cloud.enqueue(session, account, make_job(session, 3))
        stale.status, stale.started_at, stale.attempts = RUNNING, utcnow() - timedelta(hours=2), 1

        claimed = cloud.claim_next(session)
        assert claimed.id == old.id and claimed.status == RUNNING and claimed.attempts == 1
        assert stale.status == QUEUED  # handed back for another go
        assert cloud.claim_next(session, skip={stale.id}).id == new.id

    def test_profile_round_trip_and_readiness(self, session, account):
        assert cloud.readiness(None)
        record = cloud.save_record(session, account, data=PROFILE)
        assert cloud.readiness(record) == ["no resume uploaded"]
        record = cloud.save_record(session, account, resume=("cv.pdf", b"%PDF"))
        assert cloud.readiness(record) == []
        profile = cloud.profile_from_record(record, "/tmp/cv.pdf")
        assert profile.full_name == "Ada Lovelace"
        assert profile.standing_answers[0].applies_to("Any other offers?")

    def test_unknown_profile_fields_are_dropped_and_bad_shapes_refused(self):
        assert "nope" not in cloud.clean_profile_data({**PROFILE, "nope": 1})
        with pytest.raises((TypeError, ValueError)):
            cloud.clean_profile_data({"standing_answers": [{"wrong": "shape"}]})
        with pytest.raises(ValueError):
            cloud.clean_profile_data(["not", "an", "object"])

    def test_dispatch_without_a_token_relies_on_the_schedule(self, session):
        assert cloud.dispatch_worker(session) == "scheduled"


# ----------------------------------------------------------------- worker


@pytest.fixture
def worker_db(engine, monkeypatch):
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def scope():
        s = factory()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(worker, "session_scope", scope)
    return factory


def _draft(answers):
    return build_draft(
        key="greenhouse:acme:1", canonical_job_id="job-1", company="Acme", title="Intern",
        form_url="https://example.com/form", answers=answers,
    )


class TestWorker:
    def _queue(self, session, account, *, resume=True):
        job = make_job(session, 1)
        cloud.save_record(session, account, data=PROFILE, resume=("cv.pdf", b"%PDF") if resume else None)
        task = cloud.enqueue(session, account, job)
        session.commit()
        return task, job

    def test_an_incomplete_profile_hands_the_task_back(self, session, account, worker_db):
        task, _ = self._queue(session, account, resume=False)
        tally = worker.run(say=lambda _: None)
        session.expire_all()
        assert tally == {NEEDS_YOU: 1}
        assert "resume" in session.get(ApplyTask, task.id).detail

    def test_an_unanswerable_required_question_is_never_guessed(self, session, account, worker_db, monkeypatch):
        task, _ = self._queue(session, account)
        open_q = Answer(name="q1", label="Describe your thesis", type="textarea", required=True)
        monkeypatch.setattr(worker, "draft_for", lambda *a, **k: _draft([open_q]))
        monkeypatch.setattr(worker, "_fill_and_submit", lambda *a, **k: pytest.fail("must not open a browser"))

        worker.run(say=lambda _: None)
        session.expire_all()
        done = session.get(ApplyTask, task.id)
        assert done.status == NEEDS_YOU
        assert done.left_for_you == ["Describe your thesis"]

    def test_a_confirmed_submission_marks_the_job_applied(self, session, account, worker_db, monkeypatch):
        task, job = self._queue(session, account)
        seen = {}

        def fake_submit(draft, profile, *, dry_run, task_id):
            seen["resume_name"] = profile.resume_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            return worker.Outcome(SUBMITTED, "Submitted; Greenhouse confirmed it.", b"jpg")

        filled = Answer(name="first_name", label="First Name", type="text", required=True,
                        value="Ada", source="profile")
        monkeypatch.setattr(worker, "draft_for", lambda *a, **k: _draft([filled]))
        monkeypatch.setattr(worker, "_fill_and_submit", fake_submit)

        assert worker.run(say=lambda _: None) == {SUBMITTED: 1}
        session.expire_all()
        done = session.get(ApplyTask, task.id)
        assert done.submitted_at is not None and done.screenshot == b"jpg"
        assert done.answers[0]["value"] == "Ada"
        assert seen["resume_name"] == "cv.pdf"
        state = user_jobs.get_state(session, account, session.get(Job, job.id))
        assert state.status == JobStatus.APPLIED.value

    def test_a_closed_posting_fails_without_retrying(self, session, account, worker_db, monkeypatch):
        import httpx

        task, _ = self._queue(session, account)

        def gone(*a, **k):
            request = httpx.Request("GET", "https://boards-api.greenhouse.io")
            raise httpx.HTTPStatusError("gone", request=request, response=httpx.Response(404, request=request))

        monkeypatch.setattr(worker, "draft_for", gone)
        worker.run(say=lambda _: None)
        session.expire_all()
        assert session.get(ApplyTask, task.id).status == FAILED

    def test_dry_run_leaves_the_task_queued_and_does_not_loop(self, session, account, worker_db, monkeypatch):
        task, _ = self._queue(session, account)
        monkeypatch.setattr(worker, "draft_for", lambda *a, **k: _draft([]))
        monkeypatch.setattr(
            worker, "_fill_and_submit",
            lambda *a, **k: worker.Outcome(QUEUED, "Dry run: filled in, not submitted.", None),
        )
        assert worker.run(dry_run=True, say=lambda _: None) == {QUEUED: 1}
        session.expire_all()
        assert session.get(ApplyTask, task.id).status == QUEUED


def test_security_code_extraction():
    from app.apply.inbox import extract_code

    body = "<p>Copy and paste this code into the security code field on your application:</p><h1>Ab3dEf9H</h1>"
    assert extract_code(body) == "Ab3dEf9H"
    assert extract_code("Please complete your application today") is None


def test_claude_answers_are_parsed_and_unconfident_ones_kept_as_such():
    from app.apply.agent_llm import parse_answers

    out = parse_answers({"answers": [
        {"name": "q1", "answer": "Because RTL.", "confident": True},
        {"name": "q2", "answer": None, "confident": False},
        {"answer": "no name"},
    ]})
    assert out == {
        "q1": {"answer": "Because RTL.", "confident": True},
        "q2": {"answer": None, "confident": False},
    }


# ------------------------------------------------------------------ pages


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def signed_in(client, account):
    client.post("/login", data={"email": "me@example.com", "password": "a-good-password"})
    return client


class TestPages:
    def test_swipe_page_embeds_the_deck(self, signed_in, session):
        make_job(session, 1, company="Cardco")
        page = signed_in.get("/swipe")
        assert page.status_code == 200
        assert "Cardco" in page.text and "Add your details and resume" in page.text

    def test_yes_queues_no_dismisses_undo_restores(self, signed_in, session, account):
        yes, no = make_job(session, 1), make_job(session, 2)

        assert signed_in.post(f"/swipe/{yes.id}", data={"decision": "yes"}).json()["ok"]
        task = session.query(ApplyTask).filter_by(job_id=yes.id).one()
        assert task.status == QUEUED

        assert signed_in.post(f"/swipe/{no.id}", data={"decision": "no"}).json()["ok"]
        assert user_jobs.get_state(session, account, no).status == JobStatus.DISMISSED.value

        assert signed_in.post(f"/swipe/{yes.id}", data={"decision": "undo"}).json()["ok"]
        session.refresh(task)
        assert task.status == "cancelled"
        assert user_jobs.get_state(session, account, yes).status == JobStatus.NEW.value

        more = signed_in.get("/swipe/more", params={"exclude": str(no.id)}).json()
        assert [c["id"] for c in more["cards"]] == [yes.id]

    def test_undo_is_refused_once_the_agent_started(self, signed_in, session):
        job = make_job(session, 1)
        signed_in.post(f"/swipe/{job.id}", data={"decision": "yes"})
        session.query(ApplyTask).filter_by(job_id=job.id).one().status = RUNNING
        session.flush()
        assert signed_in.post(f"/swipe/{job.id}", data={"decision": "undo"}).json()["ok"] is False

    def test_already_applied_records_it_without_queueing(self, signed_in, session, account):
        job = make_job(session, 1)
        assert signed_in.post(f"/swipe/{job.id}", data={"decision": "applied"}).json()["ok"]
        assert user_jobs.get_state(session, account, job).status == JobStatus.APPLIED.value
        assert session.query(ApplyTask).count() == 0
        assert cloud.deck(session, account) == []

        assert signed_in.post(f"/swipe/{job.id}", data={"decision": "undo"}).json()["ok"]
        assert user_jobs.get_state(session, account, job).status == JobStatus.NEW.value

    def test_profile_upload_and_status_page(self, signed_in, session, account):
        r = signed_in.post(
            "/autoapply/profile",
            data={"profile_json": json.dumps(PROFILE)},
            files={"resume": ("cv.pdf", b"%PDF-1.4", "application/pdf")},
        )
        assert r.status_code == 303 and "bad" not in r.headers["location"]
        record = cloud.get_record(session, account)
        assert record.resume_filename == "cv.pdf" and record.data["first_name"] == "Ada"

        bad = signed_in.post("/autoapply/profile", data={"profile_json": "{not json"})
        assert "bad=1" in bad.headers["location"]

        page = signed_in.get("/autoapply")
        assert page.status_code == 200 and "ready" in page.text

    def test_status_page_never_loads_screenshot_bytes(self, session, account):
        from sqlalchemy import inspect

        task = cloud.enqueue(session, account, make_job(session, 1))
        task.screenshot = b"jpeg"
        session.commit()
        session.expunge_all()
        [loaded] = cloud.tasks_for(session, account)
        assert loaded.has_screenshot is True
        assert "screenshot" in inspect(loaded).unloaded

    def test_old_screenshots_are_pruned(self, session, account):
        task = cloud.enqueue(session, account, make_job(session, 1))
        task.screenshot, task.finished_at = b"jpeg", utcnow() - timedelta(days=30)
        session.flush()
        assert cloud.prune_screenshots(session) == 1
        session.refresh(task)
        assert task.screenshot is None
    def test_another_users_screenshot_is_not_reachable(self, signed_in, session):
        other = auth.create_account(session, email="you@example.com", password="another-password")
        job = make_job(session, 1)
        task = cloud.enqueue(session, other, job)
        task.screenshot = b"secret"
        session.flush()
        assert signed_in.get(f"/autoapply/task/{task.id}/screenshot").status_code == 404


def test_profile_dataclass_still_constructs_from_example():
    """The status page seeds its editor from the example profile."""
    data = json.loads(cloud.profile_json(None))
    assert ApplicantProfile(**{k: v for k, v in data.items() if k != "standing_answers"})
