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
