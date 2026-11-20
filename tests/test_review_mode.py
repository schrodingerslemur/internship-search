"""Checking applications before they're sent, and degrees besides the current one."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.apply import cloud, worker
from app.apply.agent import AgentResult
from app.apply.answers import Answer, prepare_answers
from app.apply.greenhouse import Question
from app.apply.profile import ApplicantProfile, clean_education
from app.db import get_db
from app.main import create_app
from app.models import ApplyTask
from app.models.autoapply import QUEUED, REVIEW, SUBMITTED
from app.services import onboarding
from tests.test_autoapply import PROFILE, _draft, account, make_job, worker_db  # noqa: F401 - fixtures


class FakeAnswerer:
    names = ["fake"]

    def complete_json(self, *a, **k):
        return None

    def answer_application_questions(self, **k):
        return {}

    def close(self):
        pass


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr("app.services.user_llm.answerer_for", lambda *a, **k: FakeAnswerer())


def _queue(session, account, *, greenhouse=True, review=True):  # noqa: F811
    account.review_applications = review
    url = None if greenhouse else "https://jobs.lever.co/acme/1/apply"
    job = make_job(session, 1, greenhouse=greenhouse, **({"application_url": url} if url else {}))
    cloud.save_record(session, account, data=PROFILE, resume=("cv.pdf", b"%PDF"))
    task = cloud.enqueue(session, account, job)
    session.commit()
    return task


class TestGreenhouse:
    def test_a_reviewer_gets_a_draft_and_nothing_is_sent(self, session, account, worker_db, monkeypatch, model):  # noqa: F811
        task = _queue(session, account)
        why = Answer(name="q1", label="Why Acme?", type="textarea", required=False, value="Because.", source="ai")
        monkeypatch.setattr(worker, "draft_for", lambda *a, **k: _draft([why]))
        monkeypatch.setattr(worker, "_fill_and_submit", lambda *a, **k: pytest.fail("must not open a browser"))

        assert worker.run(say=lambda _: None) == {REVIEW: 1}
        session.expire_all()
        done = session.get(ApplyTask, task.id)
        assert done.status == REVIEW and done.submitted_at is None
        assert done.answers[0] == {"label": "Why Acme?", "value": "Because.", "source": "ai",
                                   "required": False, "type": "textarea"}

    def test_approving_sends_exactly_the_edited_answers(self, session, account, worker_db, monkeypatch, model):  # noqa: F811
        task = _queue(session, account)
        task.status = REVIEW
        cloud.approve(session, account, task, {"Why Acme?": "My own words."})
        session.commit()
        seen = {}

        def fake_draft(form, profile, **k):
            seen.update(llm=k["llm"], overrides=k["overrides"])
            return _draft([Answer(name="q1", label="Why Acme?", type="textarea", required=False,
                                  value=k["overrides"]["Why Acme?"], source="you")])

        monkeypatch.setattr(worker, "draft_for", fake_draft)
        monkeypatch.setattr(worker, "_fill_and_submit",
                            lambda *a, **k: worker.Outcome(SUBMITTED, "Submitted; Greenhouse confirmed it."))
        assert worker.run(say=lambda _: None) == {SUBMITTED: 1}
        assert seen == {"llm": None, "overrides": {"Why Acme?": "My own words."}}

    def test_feedback_reaches_the_model_and_comes_back_to_check(self, session, account, worker_db, monkeypatch, model):  # noqa: F811
        task = _queue(session, account)
        task.status = REVIEW
        cloud.redraft(session, account, task, "Shorter, and mention FPGAs.")
        session.commit()
        seen = {}
        monkeypatch.setattr(worker, "draft_for", lambda *a, **k: seen.update(k) or _draft([]))
        monkeypatch.setattr(worker, "_fill_and_submit", lambda *a, **k: pytest.fail("must not send"))
        assert worker.run(say=lambda _: None) == {REVIEW: 1}
        assert seen["feedback"] == "Shorter, and mention FPGAs." and seen["llm"] is not None

    def test_without_review_mode_it_still_sends_straight_away(self, session, account, worker_db, monkeypatch, model):  # noqa: F811
        _queue(session, account, review=False)
        monkeypatch.setattr(worker, "draft_for", lambda *a, **k: _draft([]))
        monkeypatch.setattr(worker, "_fill_and_submit", lambda *a, **k: worker.Outcome(SUBMITTED, "ok"))
        assert worker.run(say=lambda _: None) == {SUBMITTED: 1}


def test_the_agent_stops_before_submit_then_sends_what_was_approved(session, account, worker_db, monkeypatch, model):  # noqa: F811
    task = _queue(session, account, greenhouse=False)
    runs = []

    def fake_run(url, answerer, *, briefing, files, recipe, task_id, dry_run):
        runs.append({"dry_run": dry_run, "briefing": briefing})
        if dry_run:
            return AgentResult("ready", "filled", filled=[{"label": "Why us?", "value": "Draft."}]), b"jpg"
        return AgentResult("submitted", "ok"), b"jpg"

    monkeypatch.setattr(worker, "_run_agent", fake_run)
    assert worker.run(say=lambda _: None) == {REVIEW: 1}
    session.expire_all()
    task = session.get(ApplyTask, task.id)
    assert task.status == REVIEW and task.answers[0]["value"] == "Draft."

    cloud.approve(session, account, task, {"Why us?": "Mine."})
    session.commit()
    assert worker.run(say=lambda _: None) == {SUBMITTED: 1}
    assert runs[0]["dry_run"] is True and runs[1]["dry_run"] is False
    assert "- Why us?: Mine." in runs[1]["briefing"]


def test_requeueing_forgets_an_old_approval(session, account):  # noqa: F811
    task = _queue(session, account)
    cloud.approve(session, account, task, {"x": "y"})
    again = cloud.enqueue(session, account, task.job)
    assert again.status == QUEUED and again.approved is False and again.overrides == {}


def test_your_edit_beats_every_other_source():
    profile = ApplicantProfile(first_name="Ada", standing_answers=[])
    q = Question(name="q1", label="Why Acme?", type="textarea", required=True, options=[])
    [a] = prepare_answers([q], profile, overrides={"why acme?": "Mine."})
    assert (a.value, a.source) == ("Mine.", "you")


class TestPages:
    @pytest.fixture
    def client(self, session, account):  # noqa: F811
        app = create_app()
        app.dependency_overrides[get_db] = lambda: session
        session.commit()
        with TestClient(app, follow_redirects=False) as c:
            c.post("/login", data={"email": "me@example.com", "password": "a-good-password"})
            yield c

    def test_the_switch_and_the_draft_form(self, client, session, account):  # noqa: F811
        assert "Send it right away" in client.get("/autoapply").text
        client.post("/autoapply/review-mode", data={"on": "1"})
        session.refresh(account)
        assert account.review_applications is True

        task = _queue(session, account)
        task.status, task.answers = REVIEW, [
            {"label": "Why Acme?", "value": "Because.", "source": "ai", "type": "textarea"},
            {"label": "Resume", "value": "cv.pdf", "source": "profile", "type": "file"},
        ]
        session.commit()
        page = client.get("/autoapply").text
        assert "Ready for you to check (1)" in page and "Approve and send" in page

        r = client.post(f"/autoapply/task/{task.id}/approve",
                        data={"label_1": "Why Acme?", "answer_1": "Edited."})
        assert r.status_code == 303
        session.refresh(task)
        assert task.status == QUEUED and task.approved and task.overrides == {"Why Acme?": "Edited."}

    def test_feedback_needs_words_and_a_waiting_draft(self, client, session, account):  # noqa: F811
        task = _queue(session, account)
        assert "bad=1" in client.post(f"/autoapply/task/{task.id}/feedback", data={"feedback": "x"}).headers["location"]
        task.status = REVIEW
        session.commit()
        assert "bad=1" in client.post(f"/autoapply/task/{task.id}/feedback", data={"feedback": " "}).headers["location"]
        client.post(f"/autoapply/task/{task.id}/feedback", data={"feedback": "Shorter."})
        session.refresh(task)
        assert task.status == QUEUED and task.feedback == "Shorter." and not task.approved


# ---------------------------------------------------------------- degrees


def test_other_degrees_are_cleaned_and_reach_the_facts():
    raw = [{"degree": "Bachelor's", "major": "ECE", "school": "Carnegie Mellon University", "graduation_year": "2026"},
           {"school": ""}]
    edu = clean_education(raw)
    assert edu == [{"school": "Carnegie Mellon University", "degree": "Bachelor's", "discipline": "ECE",
                    "graduation_year": 2026}]
    facts = ApplicantProfile(other_education=edu).facts_text()
    assert "Other degree: Bachelor's in ECE, Carnegie Mellon University (2026)" in facts
    with pytest.raises(ValueError):
        clean_education("Bachelor's at CMU")


def test_the_welcome_form_rows_become_the_profile(session, account):  # noqa: F811
    form = {"edu_degree_1": "Bachelor's", "edu_major_1": "ECE", "edu_school_1": "CMU",
            "edu_graduation_year_1": "2026", "edu_degree_2": "", "edu_major_2": "", "edu_school_2": "",
            "edu_graduation_year_2": "", "school": "CMU", "degree": "Master's"}
    draft = onboarding.draft_from_form(form)
    assert draft["other_education"] == [{"school": "CMU", "degree": "Bachelor's", "major": "ECE",
                                         "graduation_year": 2026}]
    state = onboarding.begin(session, account)
    onboarding.finish(session, account, state, draft)
    data = cloud.get_record(session, account).data
    assert data["other_education"] == [{"school": "CMU", "degree": "Bachelor's", "discipline": "ECE",
                                        "graduation_year": 2026}]
    assert data["degree"] == "Master's"
