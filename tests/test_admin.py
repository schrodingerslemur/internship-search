"""The owner's Admin page: accounts, and who may sign up."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.models import AllowedEmail, ApplyTask, Job, User
from app.services import auth


@pytest.fixture
def app_(session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app):  # runs startup once: the signing key
        yield app


def _client(app_, email=None):
    c = TestClient(app_, follow_redirects=False)
    if email:
        c.post("/login", data={"email": email, "password": "a-good-password"})
    return c


@pytest.fixture
def people(session):
    owner = auth.create_account(session, email="owner@example.com", password="a-good-password")
    friend = auth.create_account(session, email="friend@example.com", password="a-good-password")
    session.commit()
    return owner, friend


def test_only_the_owner_sees_it(app_, people):
    owner_c, friend_c = _client(app_, "owner@example.com"), _client(app_, "friend@example.com")
    page = owner_c.get("/admin")
    assert page.status_code == 200
    assert "friend@example.com" in page.text and "Accounts (2)" in page.text
    assert 'href="/admin"' in owner_c.get("/settings").text
    assert friend_c.get("/admin").status_code == 404
    assert 'href="/admin"' not in friend_c.get("/settings").text
    assert friend_c.post("/admin/allow", data={"entries": "x@y.com"}).status_code == 404


def test_the_list_on_admin_gates_signup(app_, session, people):
    owner_c = _client(app_, "owner@example.com")
    r = owner_c.post("/admin/allow", data={"entries": "Ada@Example.com\n@cmu.edu, nonsense"})
    assert "nonsense" in r.headers["location"]
    assert sorted(e for (e,) in session.query(AllowedEmail.entry)) == ["@cmu.edu", "ada@example.com"]
    anon = _client(app_)
    assert anon.post("/signup", data={"email": "stranger@example.com", "password": "a-good-password"}).status_code == 403
    assert anon.post("/signup", data={"email": "ada@example.com", "password": "a-good-password"}).status_code == 303
    assert anon.post("/signup", data={"email": "bo@cmu.edu", "password": "a-good-password"}).status_code == 303

    row = session.query(AllowedEmail).filter_by(entry="@cmu.edu").one()
    owner_c.post(f"/admin/allow/{row.id}/remove")
    assert anon.post("/signup", data={"email": "cy@cmu.edu", "password": "a-good-password"}).status_code == 403


def test_render_entries_show_and_count(app_, people, monkeypatch):
    monkeypatch.setattr(get_settings(), "signup_allowed_emails", "pal@example.com")
    assert "pal@example.com · Render" in _client(app_, "owner@example.com").get("/admin").text


def test_turning_an_account_off_locks_it_and_cancels_its_queue(app_, session, people):
    _, friend = people
    job = Job(canonical_job_id="j", fingerprint="f", company_name="X", title="Intern", application_url="https://x")
    session.add(job)
    session.flush()
    task = ApplyTask(user_id=friend.id, job_id=job.id, form_key="k", status="queued", queued_at=job.created_at)
    session.add(task)
    session.commit()
    friend_c = _client(app_, "friend@example.com")
    owner_c = _client(app_, "owner@example.com")

    owner_c.post(f"/admin/users/{friend.id}/active", data={"active": "0"})
    assert session.get(User, friend.id).is_active is False
    assert task.status == "cancelled"
    assert friend_c.get("/settings").status_code == 401
    assert _client(app_, "friend@example.com").cookies.get(auth.SESSION_COOKIE) is None

    owner_c.post(f"/admin/users/{friend.id}/active", data={"active": "1"})
    assert _client(app_, "friend@example.com").get("/settings").status_code == 200


def test_delete_needs_the_email_typed_and_never_the_owner(app_, session, people):
    owner, friend = people
    owner_c = _client(app_, "owner@example.com")
    owner_c.post(f"/admin/users/{friend.id}/delete", data={"confirm": "wrong"})
    assert session.get(User, friend.id) is not None
    owner_c.post(f"/admin/users/{friend.id}/delete", data={"confirm": "Friend@example.com"})
    session.expire_all()
    assert session.get(User, friend.id) is None
    assert owner_c.post(f"/admin/users/{owner.id}/delete", data={"confirm": "owner@example.com"}).status_code == 400
