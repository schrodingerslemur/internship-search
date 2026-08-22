"""End-to-end API and page tests against a real app instance."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Job
from app.models.base import JobStatus, Priority
from tests.conftest import NOW


@pytest.fixture
def client(session):
    """A signed-in client. Every page and API route requires an account."""
    from app.services import auth

    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as c:
        auth.create_account(session, email="tester@example.com", password="a-good-password")
        session.flush()
        c.post("/login", data={"email": "tester@example.com", "password": "a-good-password"})
        yield c


@pytest.fixture
def anon_client(session):
    """A signed-out client, for checking that access is actually gated."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def seeded(session):
    jobs = [
        Job(canonical_job_id="j1", fingerprint="f1", company_name="NVIDIA",
            title="FPGA Design Intern", application_url="https://nvidia.com/apply/1",
            relevance_score=94.0, priority=Priority.APPLY_NOW.value,
            status=JobStatus.NEW.value, is_active=True, location_raw="Santa Clara, CA",
            date_discovered=NOW, date_posted=NOW - timedelta(days=1),
            match_reasons=["FPGA", "SystemVerilog"], concerns=[], skills=["fpga", "rtl"],
            score_breakdown={"role_match": {"value": 90, "weight": 0.25, "weighted": 22.5,
                                            "reasons": [], "concerns": []}}),
        Job(canonical_job_id="j2", fingerprint="f2", company_name="AMD",
            title="Design Verification Intern", application_url="https://amd.com/apply/2",
            relevance_score=88.0, priority=Priority.STRONG_MATCH.value,
            status=JobStatus.NEW.value, is_active=True, location_raw="Austin, TX",
            date_discovered=NOW, date_posted=NOW - timedelta(days=3),
            match_reasons=["UVM"], concerns=[], skills=["uvm"], score_breakdown={}),
        Job(canonical_job_id="j3", fingerprint="f3", company_name="Coca-Cola",
            title="Marketing Intern", application_url="https://cocacola.com/apply/3",
            relevance_score=42.0, priority=Priority.SKIP.value,
            status=JobStatus.NEW.value, is_active=True, location_raw="Atlanta, GA",
            date_discovered=NOW, date_posted=NOW - timedelta(days=2),
            match_reasons=[], concerns=[], skills=[], score_breakdown={}),
    ]
    session.add_all(jobs)
    session.commit()
    return jobs


class TestHealth:
    def test_health(self, client):
        assert client.get("/health").json() == {"status": "ok"}


class TestJobsApi:
    def test_lists_jobs(self, client, seeded):
        data = client.get("/api/jobs").json()
        assert data["total"] == 3
        assert len(data["jobs"]) == 3

    def test_sorted_by_score_by_default(self, client, seeded):
        jobs = client.get("/api/jobs").json()["jobs"]
        scores = [j["score"] for j in jobs]
        assert scores == sorted(scores, reverse=True)

    def test_filter_by_min_score(self, client, seeded):
        data = client.get("/api/jobs?min_score=85").json()
        assert data["total"] == 2

    def test_filter_by_priority(self, client, seeded):
        data = client.get("/api/jobs?priority=apply_now").json()
        assert data["total"] == 1
        assert data["jobs"][0]["company"] == "NVIDIA"

    def test_filter_by_company(self, client, seeded):
        assert client.get("/api/jobs?company=AMD").json()["total"] == 1

    def test_free_text_search(self, client, seeded):
        assert client.get("/api/jobs?q=verification").json()["total"] == 1

    def test_filter_by_skill(self, client, seeded):
        assert client.get("/api/jobs?q=&skill=uvm").json()["total"] == 1

    def test_pagination(self, client, seeded):
        data = client.get("/api/jobs?per_page=2&page=2").json()
        assert data["page"] == 2
        assert len(data["jobs"]) == 1

    def test_detail_includes_sources_and_breakdown(self, client, seeded):
        job = client.get("/api/jobs/1").json()
        assert job["title"] == "FPGA Design Intern"
        assert "listings" in job
        assert "score_breakdown" in job

    def test_detail_by_canonical_id(self, client, seeded):
        assert client.get("/api/jobs/j2").json()["company"] == "AMD"

    def test_missing_job_returns_404(self, client, seeded):
        assert client.get("/api/jobs/99999").status_code == 404


class TestActions:
    def test_save_a_job(self, client, seeded, session):
        from app.services import auth, user_jobs

        response = client.post("/api/jobs/1/status", json={"status": "saved"})
        assert response.json()["status"] == "saved"

        # Recorded against the signed-in account, not the shared job row.
        user = auth.find_by_email(session, "tester@example.com")
        assert user_jobs.status_of(user_jobs.get_state(session, user, 1)) == "saved"

    def test_dismiss_hides_from_default_list(self, client, seeded):
        client.post("/api/jobs/3/status", json={"status": "dismissed"})
        assert client.get("/api/jobs").json()["total"] == 2

    def test_invalid_status_rejected(self, client, seeded):
        assert client.post("/api/jobs/1/status", json={"status": "nonsense"}).status_code == 400

    def test_add_a_note(self, client, seeded):
        response = client.post("/api/jobs/1/notes", json={"body": "Need referral"})
        assert response.json()["ok"] is True

    def test_empty_note_rejected(self, client, seeded):
        assert client.post("/api/jobs/1/notes", json={"body": "  "}).status_code == 400

    def test_counts_reflect_state(self, client, seeded):
        client.post("/api/jobs/1/status", json={"status": "saved"})
        counts = client.get("/api/counts").json()
        assert counts["saved"] == 1
        assert counts["strong"] == 1


class TestPreferencesApi:
    def test_get_preferences(self, client):
        prefs = client.get("/api/preferences").json()
        assert "roles" in prefs and prefs["roles"]

    def test_round_trip_update(self, client):
        prefs = client.get("/api/preferences").json()
        prefs["notifications"]["min_score"] = 55
        prefs["roles"] = [{"name": "Robotics Intern", "weight": 1.0, "enabled": True,
                           "order": 0, "extra_queries": []}]
        assert client.put("/api/preferences", json=prefs).json()["ok"] is True
        updated = client.get("/api/preferences").json()
        assert updated["notifications"]["min_score"] == 55
        assert updated["roles"][0]["name"] == "Robotics Intern"

    def test_invalid_preferences_are_rejected(self, client):
        prefs = client.get("/api/preferences").json()
        # thresholds must descend
        prefs["thresholds"] = {"apply_now": 10, "strong_match": 90,
                               "worth_considering": 70, "maybe": 60}
        assert client.put("/api/preferences", json=prefs).status_code == 422

    def test_profile_round_trip(self, client):
        profile = client.get("/api/profile").json()
        profile["school"] = "MIT"
        assert client.put("/api/profile", json=profile).json()["ok"] is True
        assert client.get("/api/profile").json()["school"] == "MIT"


class TestObservability:
    def test_sources_report_health_and_requirements(self, client):
        sources = client.get("/api/sources").json()
        assert any(s["name"] == "greenhouse" for s in sources)
        adzuna = next(s for s in sources if s["name"] == "adzuna")
        assert adzuna["requires_credentials"] is True
        assert "app_id" in adzuna["required_credentials"]

    def test_coverage_is_honest_before_any_run(self, client):
        coverage = client.get("/api/coverage").json()
        assert coverage["latest_run"] is None

    def test_analytics_endpoint(self, client, seeded):
        data = client.get("/api/analytics").json()
        assert data["summary"]["total_jobs"] == 3
        assert data["outcomes"]["has_enough_data"] is False

    def test_notifications_history_empty(self, client):
        assert client.get("/api/notifications").json() == []


class TestPages:
    @pytest.mark.parametrize(
        "path", ["/", "/tracker", "/coverage", "/analytics", "/settings", "/profile"]
    )
    def test_pages_render(self, client, seeded, path):
        response = client.get(path)
        assert response.status_code == 200
        assert "<html" in response.text.lower()

    def test_dashboard_shows_jobs(self, client, seeded):
        body = client.get("/").text
        assert "NVIDIA" in body
        assert "FPGA Design Intern" in body

    def test_job_detail_page(self, client, seeded):
        body = client.get("/job/1").text
        assert "FPGA Design Intern" in body
        assert "Why this is a match" in body

    def test_job_detail_404(self, client, seeded):
        assert client.get("/job/9999").status_code == 404

    def test_dashboard_list_is_deduplicated_not_per_listing(self, client, seeded, session):
        """Even filtered by source, the list shows canonical jobs."""
        from app.models import JobListing

        for source in ("greenhouse", "linkedin", "indeed"):
            session.add(JobListing(job_id=1, source=source, source_job_id=f"{source}-1"))
        session.commit()
        data = client.get("/api/jobs?source=linkedin").json()
        assert data["total"] == 1
        assert data["jobs"][0]["source_count"] == 3

    def test_settings_form_saves(self, client):
        response = client.post(
            "/settings",
            data={
                "roles": "FPGA Engineer Intern | 2.0\nRobotics Intern",
                "keywords_positive": "FPGA\nVerilog",
                "keywords_negative": "senior",
                "keywords_exclude": "",
                "locations": "Boston | 9 | Cambridge, MA",
                "companies_preferred": "NVIDIA",
                "companies_blacklisted": "",
                "companies_monitored": "",
                "company_types": "Semiconductor",
                "internship_only": "on",
                "seasons": "Summer 2026",
                "max_experience_years": "2",
                "notification_min_score": "75",
                "notification_max_jobs": "5",
                "notification_provider": "file",
                "notifications_enabled": "on",
                "schedule_enabled": "on",
                "timezone": "America/New_York",
                "morning_time": "07:30",
                "afternoon_time": "17:00",
                "cadence": "weekdays",
                "max_ats_boards": "300",
                "max_queries": "40",
                "min_score_to_store": "25",
                "remote_bonus": "7",
                "other_us_bonus": "2",
                **{f"weight_{n}": "10" for n in
                   ("role_match", "technical_skills", "candidate_fit", "location",
                    "company_preference", "freshness", "internship_constraints")},
                **{f"threshold_{n}": v for n, v in
                   (("apply_now", "90"), ("strong_match", "80"),
                    ("worth_considering", "70"), ("maybe", "60"))},
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        prefs = client.get("/api/preferences").json()
        assert prefs["notifications"]["min_score"] == 75
        assert prefs["schedule"]["morning_time"] == "07:30"
        assert prefs["schedule"]["cadence"] == "weekdays"
        assert [r["name"] for r in prefs["roles"]] == ["FPGA Engineer Intern", "Robotics Intern"]
        assert prefs["roles"][0]["weight"] == 2.0


class TestAccessControl:
    """Session auth replaced HTTP basic auth; the guarantee is unchanged."""

    def test_pages_require_signing_in(self, anon_client):
        response = anon_client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_api_requires_signing_in(self, anon_client):
        assert anon_client.get("/api/jobs").status_code == 401

    def test_health_check_stays_open(self, anon_client):
        """The platform health check runs unauthenticated; gating it fails deploys."""
        assert anon_client.get("/health").status_code == 200

    def test_static_assets_stay_open(self, anon_client):
        """The login page needs its own stylesheet before anyone is signed in."""
        assert anon_client.get("/static/htmx.min.js").status_code in (200, 404)

    def test_a_signed_in_client_gets_through(self, client):
        assert client.get("/").status_code == 200


class TestFirstBootSeeding:
    """Seeding must run once per database, and never block the port binding."""

    @pytest.fixture
    def bound_db(self, session, monkeypatch):
        """Point bootstrap's own session_scope at the test database."""
        import contextlib

        import app.db as db

        @contextlib.contextmanager
        def scope():
            yield session

        monkeypatch.setattr(db, "session_scope", scope)

    def test_empty_registry_needs_seeding(self, bound_db):
        import app.services.bootstrap as bootstrap

        assert bootstrap.needs_seed() is True

    def test_populated_registry_is_not_reseeded(self, session, bound_db):
        """An ephemeral runner pointed at a seeded database must not re-seed."""
        import app.services.bootstrap as bootstrap
        from app.models import AtsBoard

        for i in range(bootstrap.MIN_SEEDED_BOARDS):
            session.add(AtsBoard(provider="greenhouse", board_token=f"board-{i}"))
        session.flush()
        assert bootstrap.needs_seed() is False

    def test_unreadable_database_does_not_start_a_seed(self, monkeypatch):
        """A broken database is a reason to stop, not to fetch curated lists."""
        import contextlib

        import app.db as db
        import app.services.bootstrap as bootstrap

        @contextlib.contextmanager
        def boom():
            raise RuntimeError("no such table: ats_boards")
            yield

        monkeypatch.setattr(db, "session_scope", boom)
        assert bootstrap.needs_seed() is False
        assert bootstrap.schedule_seed() is None

    async def test_seed_failure_is_swallowed_so_startup_survives(self, monkeypatch, bound_db):
        import app.services.bootstrap as bootstrap

        async def unreachable():
            raise RuntimeError("curated lists unreachable")

        monkeypatch.setattr(bootstrap, "seed_registry", unreachable)
        await bootstrap.seed_if_needed()  # must not raise


class TestDatabaseUrlNormalisation:
    """A hosted provider's connection string must work as pasted."""

    def _url(self, value: str) -> str:
        from app.config import Settings

        return Settings(database_url=value, _env_file=None).database_url

    def test_neon_style_url_gains_a_driver(self):
        assert self._url("postgresql://u:p@ep-x.neon.tech/db?sslmode=require") == (
            "postgresql+psycopg://u:p@ep-x.neon.tech/db?sslmode=require"
        )

    def test_legacy_postgres_scheme_is_handled(self):
        assert self._url("postgres://u:p@host/db").startswith("postgresql+psycopg://")

    def test_explicit_driver_is_left_alone(self):
        url = "postgresql+psycopg://u:p@host/db"
        assert self._url(url) == url

    def test_sqlite_is_untouched(self):
        assert self._url("sqlite:///./data/internship.db") == "sqlite:///./data/internship.db"


class TestBulkActions:
    """Clearing a screenful in one action, rather than one card at a time."""

    def _user(self, session):
        from app.services import auth

        return auth.find_by_email(session, "tester@example.com")

    def test_bulk_dismiss_applies_to_every_selected_job(self, client, seeded, session):
        from app.services import user_jobs

        response = client.post(
            "/jobs/bulk",
            data={"job_ids": ["1", "2"], "status": "dismissed", "redirect_to": "/"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        user = self._user(session)
        assert user_jobs.status_of(user_jobs.get_state(session, user, 1)) == "dismissed"
        assert user_jobs.status_of(user_jobs.get_state(session, user, 2)) == "dismissed"
        # Untouched jobs stay untouched.
        assert user_jobs.get_state(session, user, 3) is None

    def test_bulk_action_is_scoped_to_the_signed_in_user(self, client, seeded, session):
        from app.services import auth, user_jobs

        other = auth.create_account(session, email="other@example.com", password="a-good-password")
        client.post("/jobs/bulk", data={"job_ids": ["1"], "status": "applied"})

        assert user_jobs.get_state(session, other, 1) is None

    def test_an_invalid_status_changes_nothing(self, client, seeded, session):
        from app.services import user_jobs

        client.post("/jobs/bulk", data={"job_ids": ["1"], "status": "nonsense"})
        assert user_jobs.get_state(session, self._user(session), 1) is None

    def test_selecting_nothing_is_harmless(self, client, seeded, session):
        from app.models import UserJobState

        response = client.post("/jobs/bulk", data={"status": "dismissed"}, follow_redirects=False)
        assert response.status_code == 303
        assert session.query(UserJobState).count() == 0

    def test_non_numeric_ids_are_ignored(self, client, seeded, session):
        from app.models import UserJobState
        from app.services import user_jobs

        client.post("/jobs/bulk", data={"job_ids": ["1", "abc", "../etc"], "status": "saved"})
        assert user_jobs.status_of(user_jobs.get_state(session, self._user(session), 1)) == "saved"
        assert session.query(UserJobState).count() == 1

    def test_bulk_dismissed_jobs_leave_the_default_list(self, client, seeded):
        before = client.get("/api/jobs").json()["total"]
        client.post("/jobs/bulk", data={"job_ids": ["1", "2"], "status": "dismissed"})
        assert client.get("/api/jobs").json()["total"] == before - 2

    def test_repeating_a_bulk_action_reports_nothing_changed(self, client, seeded, session):
        from app.services import user_jobs

        user = self._user(session)
        first = user_jobs.bulk_set_status(session, user, [1, 2], "saved")
        second = user_jobs.bulk_set_status(session, user, [1, 2], "saved")
        assert (first, second) == (2, 0)


class TestChannelSetup:
    """Choosing a provider and configuring it are one action.

    Previously credentials lived only in environment variables, so the
    dashboard reported email as unconfigured while the scheduled runs -- which
    had the variables -- were sending digests perfectly well.
    """

    def _form(self, **overrides):
        base = {
            "notifications_enabled": "on",
            "notification_provider": "email",
            "notification_min_score": "70",
            "notification_max_jobs": "7",
        }
        base.update(overrides)
        return base

    def test_enabling_an_unconfigured_provider_is_refused(self, client, session):
        """Digests stay off, and the message says which credentials are missing."""
        from app.services.preferences import load_preferences

        response = client.post("/settings", data=self._form(), follow_redirects=False)
        assert response.status_code == 303
        location = response.headers["location"]
        assert "notice_bad=1" in location
        assert "smtp" in location.lower()
        assert load_preferences(session).notifications.enabled is False

    def test_an_unconfigured_channel_does_not_discard_the_rest_of_the_form(
        self, client, session
    ):
        """The regression that made every other setting look ignored.

        Refusing the notification channel used to roll the whole transaction
        back, so on an instance with no credentials nothing on the settings
        page could ever be saved -- deleting a target role appeared to do
        nothing, because the deletion really was being thrown away.
        """
        from app.services.preferences import load_preferences

        response = client.post(
            "/settings",
            data=self._form(roles="FPGA Engineer Intern\nRTL Design Intern"),
            follow_redirects=False,
        )
        assert response.status_code == 303

        prefs = load_preferences(session)
        assert [r.name for r in prefs.roles] == ["FPGA Engineer Intern", "RTL Design Intern"]
        # ...and the channel was still refused, rather than quietly accepted.
        assert prefs.notifications.enabled is False

    def test_supplying_credentials_in_the_same_submission_succeeds(self, client, session):
        from app.services import notify_config

        response = client.post(
            "/settings",
            data=self._form(
                smtp_host="smtp.gmail.com",
                smtp_port="587",
                smtp_user="me@gmail.com",
                smtp_password="app-password",
                digest_email="me@andrew.cmu.edu",
            ),
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "saved=1" in response.headers["location"]

        channel = notify_config.load(session)
        assert channel.email_ready
        assert channel.smtp_host == "smtp.gmail.com"

    def test_credentials_are_read_back_from_the_database(self, session):
        """The web host and the scheduled run share a database, not an env."""
        from app.services import notify_config

        notify_config.save(
            session,
            {"smtp_host": "smtp.example.com", "smtp_password": "secret", "smtp_user": "a@b.c"},
        )
        assert notify_config.load(session).email_ready

    def test_a_blank_secret_keeps_the_stored_one(self, session):
        """The form shows a placeholder, so submitting it must not wipe it."""
        from app.services import notify_config

        notify_config.save(
            session,
            {"smtp_host": "smtp.example.com", "smtp_user": "a@b.c", "smtp_password": "secret"},
        )
        notify_config.save(session, {"smtp_host": "smtp.example.com", "smtp_password": ""})

        assert notify_config.load(session).smtp_password == "secret"

    def test_a_blank_non_secret_does_clear_the_field(self, session):
        from app.services import notify_config

        notify_config.save(session, {"smtp_user": "a@b.c"})
        notify_config.save(session, {"smtp_user": ""})
        assert not notify_config.load(session).smtp_user

    def test_missing_fields_are_named_in_human_terms(self, session):
        from app.services import notify_config

        missing = notify_config.load(session).missing_for("email")
        assert "SMTP host" in missing
        assert "SMTP password" in missing

    def test_channels_needing_no_setup_are_always_ready(self, session):
        from app.services import notify_config

        channel = notify_config.load(session)
        assert channel.ready_for("file")
        assert channel.ready_for("console")
        assert channel.missing_for("file") == []

    def test_disabled_notifications_skip_the_requirement(self, client, session):
        """Turning notifications off must not demand credentials first."""
        response = client.post(
            "/settings",
            data=self._form(notifications_enabled="", notification_provider="email"),
            follow_redirects=False,
        )
        assert "saved=1" in response.headers["location"]

    def test_the_notifications_page_reports_the_real_state(self, client, session):
        """Channel readiness is reported where the channel is configured."""
        from app.services import notify_config

        assert "still needs" in client.get("/notifications").text

        notify_config.save(
            session,
            {"smtp_host": "smtp.example.com", "smtp_user": "a@b.c", "smtp_password": "secret"},
        )
        client.post(
            "/settings",
            data=self._form(
                smtp_host="smtp.example.com", smtp_user="a@b.c", smtp_password="secret"
            ),
        )
        assert "is set up and ready" in client.get("/notifications").text


class TestActingFromAJobsOwnPage:
    """Every control on the detail page must actually do something.

    The action bar is shared with the feed, where the response replaces the
    job's card. The detail page has no card, so the buttons pointed at an
    element that was not on the page -- and HTMX aborts on a target it cannot
    resolve *before* sending anything. The result was a page of controls that
    changed nothing and gave no sign of having been pressed.
    """

    def _detail(self, client, job_id):
        return client.get(f"/job/{job_id}").text

    def test_every_hx_target_on_the_page_exists_on_the_page(self, client, seeded):
        import re

        body = self._detail(client, 1)
        targets = set(re.findall(r'hx-target="(#[^"]+)"', body))
        assert targets, "the page should have at least one htmx control"
        for target in targets:
            assert f'id="{target[1:]}"' in body, f"{target} matches nothing on the page"

    def test_dismissing_from_the_detail_page_changes_the_status(self, client, seeded, session):
        from app.services import auth, user_jobs

        response = client.post(
            "/job/1/status",
            data={"status": "dismissed", "view": "review", "surface": "detail"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200

        user = auth.find_by_email(session, "tester@example.com")
        assert user_jobs.status_of(user_jobs.get_state(session, user, 1)) == "dismissed"

    def test_the_response_redraws_the_block_the_buttons_target(self, client, seeded):
        response = client.post(
            "/job/1/status",
            data={"status": "dismissed", "view": "review", "surface": "detail"},
            headers={"HX-Request": "true"},
        )
        assert 'id="job-status-1"' in response.text
        # ...and it reflects the new state rather than the old one.
        assert "Restore to review" in response.text

    def test_the_action_is_confirmed_and_undoable(self, client, seeded):
        response = client.post(
            "/job/1/status",
            data={"status": "dismissed", "view": "review", "surface": "detail"},
            headers={"HX-Request": "true"},
        )
        assert "toast" in response.text
        assert "Undo" in response.text

    @pytest.mark.parametrize("stage", ["applied", "assessment", "interview", "offer", "rejected"])
    def test_each_application_stage_can_be_set_from_the_detail_page(
        self, client, seeded, session, stage
    ):
        from app.services import auth, user_jobs

        response = client.post(
            "/job/1/status",
            data={"status": stage, "view": "applied", "surface": "detail"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        user = auth.find_by_email(session, "tester@example.com")
        assert user_jobs.status_of(user_jobs.get_state(session, user, 1)) == stage

    def test_the_feed_still_swaps_a_whole_card(self, client, seeded):
        """The fix must not change what the feed does."""
        response = client.post(
            "/job/1/status",
            data={"status": "saved", "view": "review", "surface": "feed"},
            headers={"HX-Request": "true"},
        )
        assert 'id="job-1"' in response.text
        assert 'id="job-status-1"' not in response.text

    def test_opening_an_application_asks_on_the_page_that_asked(self, client, seeded):
        response = client.post(
            "/job/1/opened",
            data={"view": "review", "surface": "detail"},
            headers={"HX-Request": "true"},
        )
        assert "Did you apply?" in response.text
        # The follow-up's own buttons must resolve too, or the question is a
        # dead end.
        assert 'hx-target="#job-status-1"' in response.text
        assert 'id="job-status-1"' in response.text

    def test_saying_not_yet_from_the_detail_page_returns_the_block(self, client, seeded):
        client.post(
            "/job/1/opened",
            data={"view": "review", "surface": "detail"},
            headers={"HX-Request": "true"},
        )
        response = client.post(
            "/job/1/not-applied",
            data={"view": "review", "surface": "detail"},
            headers={"HX-Request": "true"},
        )
        assert 'id="job-status-1"' in response.text
        assert "Did you apply?" not in response.text


class TestSettingsPageSplit:
    """Search preferences and notification setup live on separate pages.

    They share one preference document and one POST handler, so the risk is
    that saving one page silently resets the other's checkboxes -- an absent
    checkbox and an unticked one look identical in a form post.
    """

    def test_saving_search_preferences_leaves_notifications_alone(self, client, session):
        from app.services.preferences import load_preferences, save_preferences

        prefs = load_preferences(session)
        prefs.notifications.send_when_empty = True
        prefs.schedule.morning_time = "06:30"
        save_preferences(session, prefs)

        # The search page carries no notification fields at all.
        client.post(
            "/settings",
            data={
                "_section": ["search", "location", "constraints", "ranking", "scope"],
                "roles": "FPGA Engineer Intern",
            },
        )

        after = load_preferences(session)
        assert after.notifications.send_when_empty is True
        assert after.schedule.morning_time == "06:30"
        assert [r.name for r in after.roles] == ["FPGA Engineer Intern"]

    def test_country_restriction_saves_from_the_form(self, client, session):
        from app.services.preferences import load_preferences

        client.post(
            "/settings",
            data={
                "_section": ["search", "location", "constraints", "ranking", "scope"],
                "roles": "FPGA Engineer Intern",
                "allowed_countries": ["US", "CA"],
            },
        )
        assert load_preferences(session).locations.allowed_countries == ["US", "CA"]

        # Selecting nothing means anywhere, not "no countries allowed".
        client.post(
            "/settings",
            data={
                "_section": ["search", "location", "constraints", "ranking", "scope"],
                "roles": "FPGA Engineer Intern",
            },
        )
        assert load_preferences(session).locations.allowed_countries == []

    def test_notifications_page_renders(self, client):
        assert client.get("/notifications").status_code == 200


class TestCredentialNormalisation:
    """Google displays app passwords with spaces, and people paste them that way.

    The fixture below is a made-up sixteen-character string in the shape Google
    uses. Never put a real credential in a test: it is committed, pushed and
    public, and no amount of history rewriting un-publishes it.
    """

    #: Shaped like a Gmail app password. Deliberately not one.
    SPACED = "abcd efgh ijkl mnop"
    COMPACT = "abcdefghijklmnop"

    def test_a_gmail_app_password_pasted_with_spaces_is_compacted(self, session):
        from app.services import notify_config

        notify_config.save(session, {"smtp_password": self.SPACED})
        assert notify_config.load(session).smtp_password == self.COMPACT

    def test_stray_whitespace_around_a_host_is_removed(self, session):
        from app.services import notify_config

        notify_config.save(session, {"smtp_host": "  smtp.gmail.com  "})
        assert notify_config.load(session).smtp_host == "smtp.gmail.com"

    def test_an_address_copied_with_a_trailing_space_still_works(self, session):
        from app.services import notify_config

        notify_config.save(session, {"smtp_user": "me@gmail.com "})
        assert notify_config.load(session).smtp_user == "me@gmail.com"

    def test_a_correct_password_is_left_untouched(self, session):
        from app.services import notify_config

        notify_config.save(session, {"smtp_password": self.COMPACT})
        assert notify_config.load(session).smtp_password == self.COMPACT

    def test_blank_still_means_unchanged(self, session):
        """Normalising must not turn a whitespace-only submission into a wipe."""
        from app.services import notify_config

        notify_config.save(session, {"smtp_password": "real-password"})
        notify_config.save(session, {"smtp_password": "   "})
        assert notify_config.load(session).smtp_password == "real-password"
