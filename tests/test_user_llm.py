"""Bring your own model: keys sealed, each user's work on their own model only."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.apply import cloud, worker
from app.apply.agent_llm import ChainedAnswerer
from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.models import ApplyTask, UserLlmSetting
from app.models.autoapply import NEEDS_YOU, QUEUED
from app.pipeline.anthropic_client import AnthropicClient
from app.pipeline.llm import LlmClient
from app.services import auth, secretbox, user_llm
from tests.test_autoapply import PROFILE, _draft, make_job


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(get_settings(), "secrets_key", Fernet.generate_key().decode())


@pytest.fixture
def owner(session):
    user = auth.create_account(session, email="owner@example.com", password="a-good-password")
    session.flush()
    return user


@pytest.fixture
def friend(session, owner):
    user = auth.create_account(session, email="friend@example.com", password="a-good-password")
    session.flush()
    return user


class TestSecretbox:
    def test_a_sealed_key_opens_and_is_not_stored_in_the_clear(self, secrets):
        token = secretbox.seal("gsk_secret123")
        assert "gsk_secret123" not in token
        assert secretbox.unseal(token) == "gsk_secret123"

    def test_a_different_server_key_cannot_open_it(self, secrets, monkeypatch):
        token = secretbox.seal("gsk_secret123")
        monkeypatch.setattr(get_settings(), "secrets_key", Fernet.generate_key().decode())
        assert secretbox.unseal(token) is None

    def test_without_secrets_key_nothing_is_saved(self, session, friend, monkeypatch):
        monkeypatch.setattr(get_settings(), "secrets_key", None)
        with pytest.raises(user_llm.SetupError, match="SECRETS_KEY"):
            user_llm.save(session, friend, provider="groq", api_key="gsk_x")


class TestSaving:
    def test_groq_gets_its_default_model_and_a_hint_not_the_key(self, session, friend, secrets):
        row = user_llm.save(session, friend, provider="groq", api_key="gsk_abcdef9876")
        assert row.model == "openai/gpt-oss-120b"
        assert row.key_hint == "9876"
        assert "gsk_abcdef9876" not in row.key_sealed

    def test_an_empty_key_keeps_the_saved_one(self, session, friend, secrets):
        user_llm.save(session, friend, provider="groq", api_key="gsk_first1111")
        row = user_llm.save(session, friend, provider="groq", api_key="", model="llama-3.3-70b-versatile")
        assert secretbox.unseal(row.key_sealed) == "gsk_first1111"
        assert row.model == "llama-3.3-70b-versatile"

    def test_switching_provider_needs_a_new_key(self, session, friend, secrets):
        user_llm.save(session, friend, provider="groq", api_key="gsk_first1111")
        with pytest.raises(user_llm.SetupError, match="key"):
            user_llm.save(session, friend, provider="anthropic", api_key="")

    def test_other_provider_needs_an_https_base_url(self, session, friend, secrets):
        with pytest.raises(user_llm.SetupError, match="https"):
            user_llm.save(session, friend, provider="openai_compatible", api_key="k", model="m",
                          base_url="http://insecure.example.com/v1")


class TestWhoseModel:
    def test_the_first_account_is_the_owner_unless_owner_email_says_otherwise(
        self, session, owner, friend, monkeypatch
    ):
        assert user_llm.owner_id(session) == owner.id
        monkeypatch.setattr(get_settings(), "owner_email", "friend@example.com")
        assert user_llm.owner_id(session) == friend.id

    def test_the_owner_uses_the_deployment_writers(self, session, owner, monkeypatch):
        sentinel = ChainedAnswerer([])
        monkeypatch.setattr("app.apply.agent_llm.apply_answerer", lambda **_: sentinel)
        assert user_llm.answerer_for(session, owner.id) is sentinel

    def test_anyone_else_never_touches_the_deployment_writers(self, session, friend, monkeypatch):
        monkeypatch.setattr(
            "app.apply.agent_llm.apply_answerer", lambda **_: pytest.fail("the owner's quota")
        )
        assert user_llm.answerer_for(session, friend.id).names == []

    def test_a_saved_groq_key_builds_a_client_on_that_key(self, session, friend, secrets):
        user_llm.save(session, friend, provider="groq", api_key="gsk_friend")
        (client,) = user_llm.answerer_for(session, friend.id).answerers
        assert isinstance(client, LlmClient)
        assert client.base_url == "https://api.groq.com/openai/v1"
        assert client._key == "gsk_friend"
        assert client.enabled

    def test_a_saved_anthropic_key_builds_a_claude_api_client(self, session, friend, secrets):
        user_llm.save(session, friend, provider="anthropic", api_key="sk-ant-friend")
        (client,) = user_llm.answerer_for(session, friend.id).answerers
        assert isinstance(client, AnthropicClient)
        assert client.model == "claude-opus-5-5"


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


def _queue(session, user, n=1, **job_kwargs):
    job = make_job(session, n, **job_kwargs)
    cloud.save_record(session, user, data=PROFILE, resume=("cv.pdf", b"%PDF"))
    task = cloud.enqueue(session, user, job)
    session.commit()
    return task


class TestWorker:
    def test_a_friends_task_runs_on_their_model_and_never_reads_the_owners_inbox(
        self, session, owner, friend, secrets, worker_db, monkeypatch
    ):
        user_llm.save(session, friend, provider="groq", api_key="gsk_friend")
        task = _queue(session, friend)
        monkeypatch.setattr(
            "app.apply.agent_llm.apply_answerer", lambda **_: pytest.fail("the owner's quota")
        )
        seen = {}

        def fake_draft(form, profile, *, llm, **_):
            seen["key"] = llm.answerers[0]._key
            seen["inbox"] = worker._INBOX_ALLOWED.get()
            return _draft([])

        monkeypatch.setattr(worker, "draft_for", fake_draft)
        monkeypatch.setattr(worker, "_fill_and_submit", lambda *a, **k: worker.Outcome(NEEDS_YOU, "stop"))
        worker.run(say=lambda _: None)
        assert seen == {"key": "gsk_friend", "inbox": False}
        assert session.get(ApplyTask, task.id) is not None

    def test_the_owners_task_may_read_the_inbox(self, session, owner, worker_db, monkeypatch):
        _queue(session, owner)
        monkeypatch.setattr("app.apply.agent_llm.apply_answerer", lambda **_: ChainedAnswerer([]))
        seen = {}

        def fake_draft(*a, **k):
            seen["inbox"] = worker._INBOX_ALLOWED.get()
            return _draft([])

        monkeypatch.setattr(worker, "draft_for", fake_draft)
        monkeypatch.setattr(worker, "_fill_and_submit", lambda *a, **k: worker.Outcome(NEEDS_YOU, "stop"))
        worker.run(say=lambda _: None)
        assert seen == {"inbox": True}

    def test_a_user_at_their_daily_limit_waits_without_using_an_attempt(
        self, session, owner, friend, worker_db, monkeypatch
    ):
        task = _queue(session, friend)
        monkeypatch.setattr(get_settings(), "autoapply_user_daily_limit", 0)
        monkeypatch.setattr(worker, "draft_for", lambda *a, **k: pytest.fail("over the limit"))
        assert worker.run(say=lambda _: None) == {}
        session.expire_all()
        t = session.get(ApplyTask, task.id)
        assert (t.status, t.attempts) == (QUEUED, 0)

    def test_the_agent_without_a_model_hands_back_with_a_way_forward(
        self, session, owner, friend, worker_db, monkeypatch
    ):
        task = _queue(session, friend, greenhouse=False, application_url="https://jobs.lever.co/acme/1")
        assert task is not None and task.by_agent
        monkeypatch.setattr(worker, "_run_agent", lambda *a, **k: pytest.fail("no model, no browser"))
        assert worker.run(say=lambda _: None) == {NEEDS_YOU: 1}
        session.expire_all()
        assert "Your model" in session.get(ApplyTask, task.id).detail


class TestPage:
    @pytest.fixture
    def signed_in(self, session, friend):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: session
        with TestClient(app, follow_redirects=False) as c:
            c.post("/login", data={"email": "friend@example.com", "password": "a-good-password"})
            yield c

    def test_saving_tests_the_key_and_never_shows_it_again(
        self, signed_in, session, friend, secrets, monkeypatch
    ):
        monkeypatch.setattr(LlmClient, "health", lambda self: (True, f"{self.model} at {self.base_url}"))
        r = signed_in.post("/model", data={"provider": "groq", "api_key": "gsk_supersecret42"})
        assert r.status_code == 303 and "works" in r.headers["location"]
        page = signed_in.get("/model?notice=works").text
        assert "gsk_supersecret42" not in page
        assert "et42" in page  # the last four characters, and nothing more
        row = session.query(UserLlmSetting).filter_by(user_id=friend.id).one()
        assert row.checked_ok is True

    def test_a_bad_key_is_saved_but_reported(self, signed_in, session, friend, secrets, monkeypatch):
        monkeypatch.setattr(LlmClient, "health", lambda self: (False, "Groq rejected this key."))
        r = signed_in.post("/model", data={"provider": "groq", "api_key": "gsk_wrong"})
        assert "failed" in r.headers["location"]
        assert "Groq rejected this key." in signed_in.get(r.headers["location"]).text

    def test_remove_forgets_the_key(self, signed_in, session, friend, secrets, monkeypatch):
        monkeypatch.setattr(LlmClient, "health", lambda self: (True, "ok"))
        signed_in.post("/model", data={"provider": "groq", "api_key": "gsk_x"})
        signed_in.post("/model/delete")
        assert session.query(UserLlmSetting).count() == 0
