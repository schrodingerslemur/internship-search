"""Accounts: passwords, sessions, and per-user isolation.

The isolation tests are the point of the whole feature: two people share the
job pool but must never see each other's decisions.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Job, User
from app.models.base import JobStatus
from app.services import auth, user_jobs


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def two_users(session):
    brendan = auth.create_account(session, email="brendan@example.com", password="a-good-password")
    friend = auth.create_account(session, email="friend@example.com", password="another-password")
    session.flush()
    return brendan, friend


@pytest.fixture
def a_job(session):
    job = Job(
        canonical_job_id="shared-1",
        fingerprint="fp-shared",
        company_name="NVIDIA",
        title="FPGA Design Intern",
        application_url="https://example.com/apply",
        relevance_score=94.0,
    )
    session.add(job)
    session.flush()
    return job


class TestPasswords:
    def test_round_trip(self):
        stored = auth.hash_password("correct horse battery staple")
        assert auth.verify_password("correct horse battery staple", stored)
        assert not auth.verify_password("wrong", stored)

    def test_hash_is_salted(self):
        """Two accounts with the same password must not share a hash."""
        assert auth.hash_password("same") != auth.hash_password("same")

    def test_the_password_is_never_stored(self):
        assert "hunter2" not in auth.hash_password("hunter2")

    @pytest.mark.parametrize("stored", [None, "", "garbage", "scrypt$bad", "md5$1$2$3$4$5"])
    def test_malformed_hashes_are_rejected_not_crashed(self, stored):
        assert auth.verify_password("anything", stored) is False

    def test_short_passwords_are_refused(self):
        assert auth.password_problem("short") is not None
        assert auth.password_problem("long-enough-password") is None


class TestSessions:
    KEY = b"test-signing-key"

    def test_round_trip(self, session):
        user = auth.create_account(session, email="a@example.com", password="a-good-password")
        token = auth.issue_session(user, self.KEY)
        assert auth.read_session(token, self.KEY) == user.id

    def test_a_tampered_cookie_is_rejected(self, session):
        user = auth.create_account(session, email="b@example.com", password="a-good-password")
        token = auth.issue_session(user, self.KEY)
        body, _, signature = token.partition(".")
        forged = body[:-2] + "XY." + signature
        assert auth.read_session(forged, self.KEY) is None

    def test_a_cookie_signed_with_another_key_is_rejected(self, session):
        user = auth.create_account(session, email="c@example.com", password="a-good-password")
        token = auth.issue_session(user, b"someone-elses-key")
        assert auth.read_session(token, self.KEY) is None

    def test_an_expired_cookie_is_rejected(self, session):
        user = auth.create_account(session, email="d@example.com", password="a-good-password")
        issued = time.time() - auth.SESSION_MAX_AGE - 10
        token = auth.issue_session(user, self.KEY, now=issued)
        assert auth.read_session(token, self.KEY) is None

    @pytest.mark.parametrize("token", [None, "", "nonsense", "no-dot", "a.b"])
    def test_junk_cookies_log_you_out_rather_than_erroring(self, token):
        assert auth.read_session(token, self.KEY) is None

    def test_the_signing_key_is_stable_across_calls(self, session):
        """A free host restarts constantly; a per-process key would log everyone out."""
        assert auth.get_signing_key(session) == auth.get_signing_key(session)


class TestAuthentication:
    def test_correct_credentials(self, session):
        auth.create_account(session, email="e@example.com", password="a-good-password")
        assert auth.authenticate(session, "e@example.com", "a-good-password") is not None

    def test_wrong_password(self, session):
        auth.create_account(session, email="f@example.com", password="a-good-password")
        assert auth.authenticate(session, "f@example.com", "nope") is None

    def test_unknown_account(self, session):
        assert auth.authenticate(session, "nobody@example.com", "whatever") is None

    def test_email_is_case_insensitive(self, session):
        auth.create_account(session, email="Mixed@Example.com", password="a-good-password")
        assert auth.authenticate(session, "mixed@example.com", "a-good-password") is not None

    def test_a_new_account_gets_its_own_preferences(self, session):
        from app.services.preferences import load_preferences

        user = auth.create_account(session, email="g@example.com", password="a-good-password")
        assert load_preferences(session, user=user) is not None


class TestPerUserJobState:
    """The core promise: your decisions are yours."""

    def test_applying_does_not_affect_the_other_user(self, session, two_users, a_job):
        brendan, friend = two_users
        user_jobs.set_status(session, brendan, a_job, JobStatus.APPLIED.value)

        assert user_jobs.status_of(user_jobs.get_state(session, brendan, a_job)) == "applied"
        assert user_jobs.status_of(user_jobs.get_state(session, friend, a_job)) == "new"

    def test_a_dismissed_job_is_only_silenced_for_the_dismisser(self, session, two_users, a_job):
        brendan, friend = two_users
        user_jobs.set_status(session, brendan, a_job, JobStatus.DISMISSED.value)

        assert a_job.id in user_jobs.acted_on_job_ids(session, brendan)
        assert a_job.id not in user_jobs.acted_on_job_ids(session, friend)

    def test_notification_history_is_per_user(self, session, two_users, a_job):
        brendan, friend = two_users
        user_jobs.mark_notified(session, brendan, a_job)

        assert user_jobs.notified_job_ids(session, brendan) == {a_job.id}
        assert user_jobs.notified_job_ids(session, friend) == set()

    def test_the_job_row_itself_is_shared(self, session, two_users, a_job):
        """One crawl serves everyone -- that is the point of a shared pool."""
        brendan, friend = two_users
        user_jobs.set_status(session, brendan, a_job, JobStatus.APPLIED.value)
        user_jobs.set_status(session, friend, a_job, JobStatus.SAVED.value)

        assert session.query(Job).count() == 1

    def test_applied_at_is_stamped_once(self, session, two_users, a_job):
        brendan, _ = two_users
        first = user_jobs.set_status(session, brendan, a_job, JobStatus.APPLIED.value)
        stamped = first.applied_at
        user_jobs.set_status(session, brendan, a_job, JobStatus.INTERVIEW.value)
        user_jobs.set_status(session, brendan, a_job, JobStatus.APPLIED.value)
        assert user_jobs.get_state(session, brendan, a_job).applied_at == stamped

    def test_untouched_jobs_need_no_row(self, session, two_users, a_job):
        assert user_jobs.get_state(session, two_users[0], a_job) is None

    def test_status_counts_are_scoped_to_one_user(self, session, two_users, a_job):
        brendan, friend = two_users
        user_jobs.set_status(session, brendan, a_job, JobStatus.APPLIED.value)
        assert user_jobs.status_counts(session, brendan) == {"applied": 1}
        assert user_jobs.status_counts(session, friend) == {}


class TestWebAuthFlow:
    def test_dashboard_redirects_when_signed_out(self, client):
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_api_returns_401_rather_than_redirecting(self, client):
        """An XHR redirected to an HTML login form only produces a confusing error."""
        assert client.get("/api/jobs").status_code == 401

    def test_login_page_is_reachable_signed_out(self, client):
        assert client.get("/login").status_code == 200

    def test_health_stays_open(self, client):
        assert client.get("/health").status_code == 200

    def test_signup_then_reach_the_dashboard(self, client):
        response = client.post(
            "/signup",
            data={"email": "new@example.com", "password": "a-good-password", "name": "New"},
        )
        assert response.status_code == 303
        assert auth.SESSION_COOKIE in response.cookies

        assert client.get("/").status_code == 200

    def test_signup_rejects_a_short_password(self, client):
        response = client.post("/signup", data={"email": "x@example.com", "password": "short"})
        assert response.status_code == 400
        assert "at least" in response.text.lower()

    def test_signup_rejects_a_duplicate_email(self, client, session):
        auth.create_account(session, email="taken@example.com", password="a-good-password")
        response = client.post(
            "/signup", data={"email": "taken@example.com", "password": "a-good-password"}
        )
        assert response.status_code == 409

    def test_login_with_bad_credentials_stays_on_the_form(self, client, session):
        auth.create_account(session, email="real@example.com", password="a-good-password")
        response = client.post(
            "/login", data={"email": "real@example.com", "password": "wrong"}
        )
        assert response.status_code == 401
        assert "do not match" in response.text

    def test_logout_clears_the_session(self, client):
        client.post(
            "/signup", data={"email": "bye@example.com", "password": "a-good-password"}
        )
        assert client.get("/").status_code == 200

        client.post("/logout")
        assert client.get("/").status_code == 303

    def test_the_legacy_single_user_can_claim_its_account(self, client, session):
        """The pre-accounts user has no password; signing up with its email adopts it.

        Otherwise the existing tracker would be stranded behind an account
        nobody could ever log into.
        """
        legacy = User(email="me@localhost", name="Me")
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id

        response = client.post(
            "/signup", data={"email": "me@localhost", "password": "a-good-password"}
        )
        assert response.status_code == 303

        claimed = session.query(User).filter(User.email == "me@localhost").one()
        assert claimed.id == legacy_id, "must adopt the existing row, not create a second"
        assert claimed.password_hash is not None


class TestDigestIsolation:
    """Two accounts, one job pool, independent digests."""

    async def test_each_user_is_alerted_independently(self, session, two_users, a_job, tmp_path):
        from app.models.base import NotificationKind
        from app.notify.engine import send_digest
        from app.schemas.preferences import NotificationRules

        brendan, friend = two_users
        rules = NotificationRules(provider="file", min_score=50.0)

        await send_digest(session, rules, NotificationKind.MORNING_DIGEST, user=brendan)
        assert user_jobs.notified_job_ids(session, brendan) == {a_job.id}
        assert user_jobs.notified_job_ids(session, friend) == set()

        # The friend has not been told yet, so the same job is still news to him.
        _, result = await send_digest(session, rules, NotificationKind.MORNING_DIGEST, user=friend)
        assert result.ok
        assert user_jobs.notified_job_ids(session, friend) == {a_job.id}

    async def test_one_users_application_does_not_silence_the_other(
        self, session, two_users, a_job
    ):
        from app.models.base import JobStatus, NotificationKind
        from app.notify.digest import select_jobs_for_digest
        from app.notify.engine import send_digest
        from app.schemas.preferences import NotificationRules

        brendan, friend = two_users
        rules = NotificationRules(provider="file", min_score=50.0)

        user_jobs.set_status(session, brendan, a_job, JobStatus.APPLIED.value)

        assert select_jobs_for_digest(session, rules, user=brendan).is_empty
        friend_selection = select_jobs_for_digest(session, rules, user=friend)
        assert [j.id for j in friend_selection.jobs] == [a_job.id]

        _, result = await send_digest(session, rules, NotificationKind.MORNING_DIGEST, user=friend)
        assert result.ok

    async def test_thresholds_are_per_user(self, session, two_users, a_job):
        """A picky account and a permissive one see different digests."""
        from app.notify.digest import select_jobs_for_digest
        from app.schemas.preferences import NotificationRules

        brendan, friend = two_users
        picky = NotificationRules(provider="file", min_score=99.0)
        permissive = NotificationRules(provider="file", min_score=50.0)

        assert select_jobs_for_digest(session, picky, user=brendan).is_empty
        assert not select_jobs_for_digest(session, permissive, user=friend).is_empty

    def test_each_account_gets_its_own_digest_address(self, session, two_users):
        from app.notify.providers import EmailProvider, get_provider

        brendan, friend = two_users
        brendan.digest_email = "brendan@andrew.cmu.edu"
        session.flush()

        assert brendan.notification_email == "brendan@andrew.cmu.edu"
        # Falls back to the login address when no separate one is set.
        assert friend.notification_email == "friend@example.com"

        provider = get_provider("email", recipient=brendan.notification_email)
        if isinstance(provider, EmailProvider):
            assert provider.recipients == ["brendan@andrew.cmu.edu"]


class TestClaimingTheLegacyAccount:
    """Upgrading an instance that predates accounts must not strand its data."""

    def _legacy_user_with_history(self, session):
        from app.models import Application
        from app.services.preferences import get_or_create_user

        user = get_or_create_user(session)  # the pre-accounts 'me@localhost' row
        job = Job(
            canonical_job_id="legacy-1",
            fingerprint="fp-legacy",
            company_name="NVIDIA",
            title="FPGA Intern",
            application_url="https://example.com/legacy",
        )
        session.add(job)
        session.flush()
        user_jobs.set_status(session, user, job, JobStatus.APPLIED.value)
        session.add(Application(user_id=user.id, job_id=job.id, status="applied"))
        session.flush()
        return user, job

    def test_first_signup_adopts_the_existing_tracker(self, client, session):
        legacy, job = self._legacy_user_with_history(session)
        legacy_id = legacy.id

        response = client.post(
            "/signup",
            data={"email": "brendan@andrew.cmu.edu", "password": "a-good-password", "name": "B"},
        )
        assert response.status_code == 303

        users = session.query(User).all()
        assert len(users) == 1, "must adopt the legacy row, not create a second account"
        assert users[0].id == legacy_id
        assert users[0].email == "brendan@andrew.cmu.edu"
        assert users[0].password_hash is not None
        # And the application history came with it.
        assert user_jobs.status_of(user_jobs.get_state(session, users[0], job)) == "applied"

    def test_the_second_signup_creates_a_separate_account(self, client, session):
        self._legacy_user_with_history(session)
        client.post("/signup", data={"email": "first@example.com", "password": "a-good-password"})
        client.post("/logout")

        response = client.post(
            "/signup", data={"email": "friend@example.com", "password": "another-password"}
        )
        assert response.status_code == 303
        assert session.query(User).count() == 2, "the friend must get their own account"

    def test_an_established_instance_is_never_claimable(self, session):
        """Once anyone has a password, no signup can adopt an existing account."""
        auth.create_account(session, email="owner@example.com", password="a-good-password")
        assert auth.claimable_legacy_account(session) is None

    def test_claiming_requires_exactly_one_passwordless_account(self, session):
        from app.services.preferences import get_or_create_user

        get_or_create_user(session)
        assert auth.claimable_legacy_account(session) is not None

        session.add(User(email="second@example.com", name="Second"))
        session.flush()
        assert auth.claimable_legacy_account(session) is None
