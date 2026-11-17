"""A new account's setup: invite code, documents, short chat, review."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.apply import cloud
from app.apply.agent_llm import ChainedAnswerer
from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.models import ApplicantDocument, OnboardingState, User
from app.services import auth, onboarding, user_llm
from app.services.preferences import load_preferences, load_profile

RESUME = b"""Ada Example
ada@example.com  (412) 555-0199  linkedin.com/in/ada-example  github.com/adaex
Purdue University, B.S. Computer Science, May 2028, GPA: 3.85
Skills: Python, C++, PyTorch, Docker
"""


class FakeModel:
    """Answers each turn from a script; records every prompt it was shown."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete_json(self, system, prompt, **_):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None

    def answer_application_questions(self, **_):
        return {}

    def close(self):
        pass


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def owner(session):
    user = auth.create_account(session, email="owner@example.com", password="a-good-password")
    session.commit()
    return user


@pytest.fixture
def model(monkeypatch):
    """Give everyone except the owner a scripted model."""
    holder: dict = {}

    def answerer_for(session, user_id, **_):
        fake = holder.get("fake")
        return ChainedAnswerer([fake] if fake is not None else [])

    monkeypatch.setattr(user_llm, "answerer_for", answerer_for)
    return holder


def _signup(client, email="ada@example.com", **extra):
    return client.post(
        "/signup", data={"email": email, "password": "a-good-password", "name": "Ada Example", **extra}
    )


def _upload(client, transcript=True):
    files = {"resume": ("resume.txt", RESUME, "text/plain")}
    if transcript:
        files["transcript"] = ("transcript.txt", b"18-240 Digital Systems: A", "text/plain")
    return client.post("/welcome/documents", files=files)


class TestTheDraft:
    def test_only_known_fields_in_their_own_types_get_in(self):
        draft = onboarding.merge(
            {"school": "Purdue"},
            {
                "roles": ["ML Intern", "ML Intern", 3, None],
                "graduation_year": "2028",
                "graduation_month": 13,
                "gpa": "eleven",
                "remote_ok": "yes",
                "password": "hunter2",
                "school": "",
            },
        )
        assert draft == {
            "school": "Purdue",
            "roles": ["ML Intern", "3"],
            "graduation_year": 2028,
            "remote_ok": True,
        }

    def test_a_non_object_update_changes_nothing(self):
        assert onboarding.merge({"school": "Purdue"}, ["roles"]) == {"school": "Purdue"}

    def test_the_resume_alone_fills_contact_details_and_skills(self, session, owner):
        user = auth.create_account(session, email="ada@example.com", password="a-good-password",
                                   name="Ada Example")
        seed = onboarding.seed_draft(user, RESUME.decode())
        assert (seed["first_name"], seed["last_name"]) == ("Ada", "Example")
        assert seed["phone"] == "(412) 555-0199"
        assert seed["gpa"] == 3.85
        assert "linkedin.com/in/ada-example" in seed["linkedin"]
        assert "python" in [s.lower() for s in seed["skills"]]


class TestInvites:
    def test_the_first_account_needs_no_code(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "signup_invite_code", "let-me-in")
        r = _signup(client, "owner@example.com")
        assert r.status_code == 303 and r.headers["location"] == "/"

    def test_after_that_the_code_is_required(self, client, session, owner, monkeypatch):
        monkeypatch.setattr(get_settings(), "signup_invite_code", "let-me-in, second-code")
        assert "Invite code" in client.get("/signup").text
        assert _signup(client).status_code == 403
        assert _signup(client, invite="nope").status_code == 403
        assert session.query(User).count() == 1
        r = _signup(client, invite=" second-code ")
        assert r.status_code == 303 and r.headers["location"] == "/welcome"

    def test_without_a_code_set_signup_stays_open(self, client, owner):
        assert "Invite code" not in client.get("/signup").text
        assert _signup(client).headers["location"] == "/welcome"


class TestTheChat:
    def test_a_newcomer_is_sent_to_setup_until_it_is_done(self, client, owner, model):
        _signup(client)
        assert client.get("/").headers["location"] == "/welcome"
        client.post("/welcome/skip")
        assert client.get("/").status_code == 200

    def test_the_owner_is_never_sent_to_setup(self, client, owner):
        client.post("/login", data={"email": "owner@example.com", "password": "a-good-password"})
        assert client.get("/").status_code == 200

    def test_from_documents_to_a_finished_search(self, client, session, owner, model):
        fake = model["fake"] = FakeModel([
            {
                "reply": "What kinds of internships are you after?",
                "updates": {"school": "Purdue University", "major": "Computer Science",
                            "graduation_month": 5, "graduation_year": 2028},
                "done": False,
            },
            {
                "reply": "Great, you can review everything next.",
                "updates": {"roles": ["Machine Learning Intern", "Data Science Intern"],
                            "locations": ["Chicago", "Remote"], "us_citizen": True,
                            "requires_sponsorship": False, "seasons": ["Summer 2027"]},
                "done": True,
            },
        ])
        _signup(client)
        assert _upload(client).status_code == 303

        user = session.query(User).filter_by(email="ada@example.com").one()
        state = onboarding.get_state(session, user.id)
        assert state.step == "chat"
        assert "PyTorch" in fake.prompts[0] and "Digital Systems" in fake.prompts[0]
        assert session.query(ApplicantDocument).filter_by(user_id=user.id, kind="transcript").count() == 1
        assert "What kinds of internships" in client.get("/welcome").text

        client.post("/welcome/chat", data={"message": "ML or data science, Chicago or remote"})
        session.expire_all()
        state = onboarding.get_state(session, user.id)
        assert state.step == "review"
        assert "ML or data science" in fake.prompts[1]
        page = client.get("/welcome").text
        assert "Machine Learning Intern" in page and "Save and start searching" in page

        form = {k: "\n".join(v) if isinstance(v, list) else
                ("yes" if v is True else "no" if v is False else str(v))
                for k, v in state.draft.items()}
        form["avoid_companies"] = "Initech"
        r = client.post("/welcome/finish", data=form)
        assert r.status_code == 303
        session.expire_all()

        prefs = load_preferences(session, user)
        assert [r.name for r in prefs.roles] == ["Machine Learning Intern", "Data Science Intern"]
        assert [r.pattern for r in prefs.locations.rules] == ["Chicago"]
        assert prefs.companies.blacklisted == ["Initech"]
        assert prefs.constraints.seasons == ["Summer 2027"]
        assert prefs.constraints.work_authorization == "US Citizen"
        profile = load_profile(session, user)
        assert profile.school == "Purdue University"
        assert "FPGA" not in profile.all_skills(), "nothing of the starting profile survives"
        record = cloud.get_record(session, user).data
        assert record["first_name"] == "Ada" and record["email"] == "ada@example.com"
        assert record["graduation"] == "May 2028" and record["never_apply_to"] == ["Initech"]
        assert client.get("/").status_code == 200

    def test_the_chat_stops_after_five_answers(self, client, session, owner, model):
        model["fake"] = FakeModel([{"reply": f"Question {i}?", "updates": {}, "done": False} for i in range(9)])
        _signup(client)
        _upload(client, transcript=False)
        for i in range(onboarding.MAX_QUESTIONS):
            client.post("/welcome/chat", data={"message": f"answer {i}"})
        user = session.query(User).filter_by(email="ada@example.com").one()
        session.expire_all()
        assert onboarding.get_state(session, user.id).step == "review"

    def test_a_model_that_does_not_answer_keeps_the_message_for_a_retry(
        self, client, session, owner, model
    ):
        model["fake"] = FakeModel([{"reply": "Which roles?", "updates": {}, "done": False}])
        _signup(client)
        _upload(client, transcript=False)
        r = client.post("/welcome/chat", data={"message": "robotics"})
        assert r.status_code == 200 and "Check it under Your model" in r.text
        user = session.query(User).filter_by(email="ada@example.com").one()
        state = onboarding.get_state(session, user.id)
        assert state.messages[-1] == {"role": "user", "text": "robotics"}
        assert state.step == "chat" and "Try again" in r.text

    def test_without_a_model_it_goes_straight_to_the_form(self, client, session, owner, model):
        _signup(client)
        assert "short form instead" in client.get("/welcome").text
        _upload(client, transcript=False)
        page = client.get("/welcome").text
        assert "Save and start searching" in page and "(412) 555-0199" in page
        assert session.query(OnboardingState).one().messages == []
