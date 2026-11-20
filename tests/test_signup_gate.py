"""Who may sign up (the allowlist) and proving the email is theirs."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.models import User
from app.notify.base import SendResult
from app.services import auth, email_verify


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
def mail(monkeypatch):
    """Turn verification on with a fake mailbox; returns the sent bodies."""
    sent: list[tuple[str, str]] = []

    class FakeProvider:
        def __init__(self, recipient=None):
            self.recipient = recipient

        async def send(self, message):
            sent.append((self.recipient, message.text))
            return SendResult(True, "email")

    settings = get_settings()
    monkeypatch.setattr(settings, "signup_verify_email", True)
    monkeypatch.setattr(type(settings), "smtp_configured", property(lambda self: True))
    monkeypatch.setattr("app.notify.providers.EmailProvider", FakeProvider)
    return sent


def _signup(client, email="ada@example.com"):
    return client.post("/signup", data={"email": email, "password": "a-good-password"})


def _login(client, email="ada@example.com"):
    return client.post("/login", data={"email": email, "password": "a-good-password"})


def _link(sent) -> str:
    return re.search(r"/verify/\S+", sent[-1][1]).group(0)


class TestAllowlist:
    @pytest.fixture(autouse=True)
    def allow(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "signup_allowed_emails", "Ada@Example.com, @cmu.edu")

    def test_the_first_account_is_never_checked(self, client):
        r = _signup(client, "owner@example.com")
        assert r.status_code == 303 and r.headers["location"] == "/"

    def test_an_address_not_on_the_list_is_refused(self, client, session, owner):
        r = _signup(client, "stranger@example.com")
        assert r.status_code == 403 and "invite list" in r.text
        assert session.query(User).count() == 1

    def test_listed_addresses_and_domains_get_in(self, client, owner):
        assert _signup(client, "ada@example.com").status_code == 303
        assert _signup(client, "someone@cmu.edu").status_code == 303

    def test_a_lookalike_domain_does_not(self, client, owner):
        assert _signup(client, "someone@evilcmu.edu").status_code == 403

    def test_the_owner_email_is_always_allowed(self, client, session, monkeypatch):
        auth.create_account(session, email="first@example.com", password="a-good-password")
        session.commit()
        monkeypatch.setattr(get_settings(), "owner_email", "boss@example.com")
        assert _signup(client, "boss@example.com").status_code == 303

    def test_no_list_means_open(self, client, owner, monkeypatch):
        monkeypatch.setattr(get_settings(), "signup_allowed_emails", None)
        assert _signup(client, "anyone@example.com").status_code == 303


class TestVerification:
    def test_a_newcomer_gets_a_link_instead_of_a_session(self, client, owner, mail):
        r = _signup(client)
        assert r.status_code == 200 and "we sent a link" in r.text
        assert auth.SESSION_COOKIE not in r.cookies
        assert [to for to, _ in mail] == ["ada@example.com"]

    def test_they_cannot_sign_in_until_they_click_it(self, client, owner, mail):
        _signup(client)
        r = _login(client)
        assert r.status_code == 403 and "Confirm your email" in r.text
        assert len(mail) == 2  # signing in sends a fresh link

    def test_the_link_signs_them_in_once(self, client, session, owner, mail):
        _signup(client)
        link = _link(mail)
        r = client.get(link)
        assert r.status_code == 303 and r.headers["location"] == "/welcome"
        assert auth.find_by_email(session, "ada@example.com").email_verified_at is not None
        assert client.get(link).status_code == 400
        client.cookies.clear()
        assert _login(client).status_code == 303

    def test_a_forged_link_does_nothing(self, client, session, owner, mail):
        _signup(client)
        assert client.get("/verify/abc.def").status_code == 400
        user = auth.find_by_email(session, "ada@example.com")
        other_key = b"x" * 32
        assert client.get(f"/verify/{email_verify.issue(user, other_key)}").status_code == 400
        assert user.email_verified_at is None

    def test_links_expire(self, client, session, owner, mail):
        _signup(client)
        user = auth.find_by_email(session, "ada@example.com")
        old = email_verify.issue(user, client.app.state.signing_key, now=1.0)
        assert client.get(f"/verify/{old}").status_code == 400

    def test_the_owner_never_verifies(self, client, mail):
        r = _signup(client, "owner@example.com")
        assert r.status_code == 303 and mail == []

    def test_without_email_set_up_signups_are_refused(self, client, session, owner, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "signup_verify_email", True)
        monkeypatch.setattr(type(settings), "smtp_configured", property(lambda self: False))
        assert _signup(client).status_code == 503
        assert session.query(User).count() == 1

    def test_a_password_reset_also_verifies(self, client, session, owner, mail):
        from app.services import password_reset

        _signup(client)
        user = auth.find_by_email(session, "ada@example.com")
        token = password_reset.issue(user, client.app.state.signing_key)
        assert client.post(f"/reset/{token}", data={"password": "brand-new-pass"}).status_code == 303
        assert user.email_verified_at is not None


def test_extracted_text_never_carries_nul(tmp_path):
    """Postgres rejects NUL in text columns; it broke the Welcome upload."""
    from app.services.resumes import extract_text

    f = tmp_path / "resume.txt"
    f.write_bytes(b"Ada\x00 Example\x00")
    assert extract_text(f) == "Ada Example"
