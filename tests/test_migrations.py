"""Data-migration tests.

Schema migrations are checked by running them. Migrations that *move data*
need more than that: the accounts migration lifts every saved, applied and
dismissed decision out of ``jobs`` and into ``user_job_state``, and getting it
wrong would silently erase an application history that cannot be reconstructed.
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config

from app.config import PROJECT_ROOT

ACCOUNTS_REVISION = "c2d3e4f5a6b7"
PRE_ACCOUNTS_REVISION = "b1f2c3d4e5a6"

#: Every NOT NULL column on ``jobs`` at the pre-accounts revision, so a test
#: fixture can insert a row without depending on ORM defaults that describe a
#: newer schema than the one under test.
JOB_DEFAULTS: dict[str, object] = {
    "locations": "[]",
    "remote_status": "unknown",
    "employment_type": "internship",
    "deadline_is_explicit": 0,
    "sponsorship": "unknown",
    "degree_requirements": "[]",
    "terms": "[]",
    "skills": "[]",
    "relevance_score": 80.0,
    "priority": "strong_match",
    "match_reasons": "[]",
    "missing_requirements": "[]",
    "concerns": "[]",
    "score_breakdown": "{}",
    "freshness": "new",
    "is_active": 1,
    "times_reposted": 0,
}


def _alembic_config(db_path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A database at the last revision before accounts existed.

    ``migrations/env.py`` deliberately takes its URL from application settings
    so migrations and the app can never disagree about which database they mean.
    Settings are cached, so the cache has to be dropped either side of pointing
    them at a throwaway file.
    """
    from app.config import get_settings

    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    try:
        command.upgrade(_alembic_config(db_path), PRE_ACCOUNTS_REVISION)
        yield db_path
    finally:
        get_settings.cache_clear()


def _insert_user(conn, email="me@localhost"):
    conn.execute(
        "INSERT INTO users (email, name, timezone, created_at, updated_at) "
        "VALUES (?, 'Me', 'America/New_York', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (email,),
    )


def _insert_job(conn, ident: str, status: str, notified: bool):
    columns = {
        "canonical_job_id": ident,
        "fingerprint": f"fp-{ident}",
        "company_name": "Acme",
        "title": "FPGA Intern",
        "title_core": "fpga intern",
        "application_url": f"https://example.com/{ident}",
        "status": status,
        "notified": int(notified),
        **JOB_DEFAULTS,
    }
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO jobs ({names}, created_at, updated_at) "
        f"VALUES ({placeholders}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        tuple(columns.values()),
    )


class TestAccountsMigration:
    def test_tracker_decisions_survive_the_move(self, legacy_db):
        """Every decision must reappear against the existing account."""
        planted = [
            ("applied", True),
            ("saved", False),
            ("dismissed", True),
            ("interview", True),
            ("offer", False),
            ("rejected", True),
        ]
        with sqlite3.connect(legacy_db) as conn:
            _insert_user(conn)
            for i, (status, notified) in enumerate(planted):
                _insert_job(conn, f"c{i}", status, notified)

        command.upgrade(_alembic_config(legacy_db), ACCOUNTS_REVISION)

        with sqlite3.connect(legacy_db) as conn:
            rows = conn.execute(
                "SELECT j.canonical_job_id, s.status, s.notified "
                "FROM user_job_state s JOIN jobs j ON j.id = s.job_id "
                "ORDER BY j.canonical_job_id"
            ).fetchall()

        assert [(r[1], bool(r[2])) for r in rows] == planted

    def test_untouched_jobs_get_no_state_row(self, legacy_db):
        """State is created by decisions, not by crawling thousands of jobs."""
        with sqlite3.connect(legacy_db) as conn:
            _insert_user(conn)
            _insert_job(conn, "touched", "applied", False)
            for i in range(20):
                _insert_job(conn, f"untouched-{i}", "new", False)

        command.upgrade(_alembic_config(legacy_db), ACCOUNTS_REVISION)

        with sqlite3.connect(legacy_db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM user_job_state").fetchone()[0]
        assert count == 1

    def test_a_notified_but_undecided_job_is_carried_over(self, legacy_db):
        """Otherwise an already-sent job would be re-alerted after upgrading."""
        with sqlite3.connect(legacy_db) as conn:
            _insert_user(conn)
            _insert_job(conn, "seen", "new", True)

        command.upgrade(_alembic_config(legacy_db), ACCOUNTS_REVISION)

        with sqlite3.connect(legacy_db) as conn:
            row = conn.execute("SELECT status, notified FROM user_job_state").fetchone()
        assert row[0] == "new"
        assert bool(row[1]) is True

    def test_migrating_an_empty_install_is_harmless(self, legacy_db):
        """A fresh deployment has no user and no jobs to carry over."""
        command.upgrade(_alembic_config(legacy_db), ACCOUNTS_REVISION)

        with sqlite3.connect(legacy_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM user_job_state").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM app_config").fetchone()[0] == 0

    def test_accounts_gain_their_login_columns(self, legacy_db):
        with sqlite3.connect(legacy_db) as conn:
            _insert_user(conn)

        command.upgrade(_alembic_config(legacy_db), ACCOUNTS_REVISION)

        with sqlite3.connect(legacy_db) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            active = conn.execute("SELECT is_active, password_hash FROM users").fetchone()

        assert {"password_hash", "is_active", "digest_email", "last_login_at"} <= columns
        # The pre-accounts user stays usable but cannot log in until a password
        # is set, which is what the claim flow is for.
        assert bool(active[0]) is True
        assert active[1] is None
