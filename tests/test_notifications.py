"""Notification selection rules, digest rendering, and delivery."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import Job, Notification, NotificationItem
from app.models.base import Freshness, JobStatus, NotificationKind, Priority
from app.notify.base import NotificationMessage
from app.notify.digest import build_digest, build_empty_digest, select_jobs_for_digest
from app.notify.engine import send_digest
from app.notify.providers import FileProvider, TelegramProvider, get_provider
from tests.conftest import NOW


def make_job(session, **kwargs) -> Job:
    defaults = dict(
        canonical_job_id=kwargs.pop("cid", f"cid-{kwargs.get('title', 'x')}-{id(kwargs)}"),
        fingerprint="fp",
        company_name="NVIDIA",
        title="FPGA Design Intern",
        application_url="https://example.com/apply",
        relevance_score=92.0,
        priority=Priority.APPLY_NOW.value,
        status=JobStatus.NEW.value,
        freshness=Freshness.NEW.value,
        is_active=True,
        location_raw="Santa Clara, CA",
        date_discovered=NOW,
        date_posted=NOW - timedelta(days=1),
        match_reasons=["Strong FPGA and SystemVerilog match"],
        concerns=[],
        skills=["fpga"],
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    session.add(job)
    session.flush()
    return job


@pytest.fixture
def rules(prefs):
    return prefs.notifications


class TestSelection:
    def test_high_scoring_new_job_is_selected(self, session, rules):
        make_job(session, cid="a")
        selection = select_jobs_for_digest(session, rules, now=NOW)
        assert len(selection.jobs) == 1

    def test_below_threshold_is_not_selected(self, session, rules):
        make_job(session, cid="b", relevance_score=55.0, priority=Priority.SKIP.value)
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty

    def test_threshold_is_configurable(self, session, rules):
        make_job(session, cid="c", relevance_score=72.0)
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty
        rules.min_score = 70
        assert len(select_jobs_for_digest(session, rules, now=NOW).jobs) == 1

    def test_dismissed_job_is_never_sent(self, session, rules):
        make_job(session, cid="d", status=JobStatus.DISMISSED.value)
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty

    def test_applied_job_is_never_sent(self, session, rules):
        make_job(session, cid="e", status=JobStatus.APPLIED.value)
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty

    def test_inactive_job_is_never_sent(self, session, rules):
        make_job(session, cid="f", is_active=False)
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty

    def test_max_jobs_per_notification_is_respected(self, session, rules):
        for i in range(12):
            make_job(session, cid=f"g{i}", title=f"FPGA Intern {i}")
        rules.max_jobs_per_notification = 5
        assert len(select_jobs_for_digest(session, rules, now=NOW).jobs) == 5

    def test_higher_priority_jobs_come_first(self, session, rules):
        make_job(session, cid="low", title="Lower", relevance_score=82.0,
                 priority=Priority.STRONG_MATCH.value)
        make_job(session, cid="high", title="Higher", relevance_score=95.0,
                 priority=Priority.APPLY_NOW.value)
        selection = select_jobs_for_digest(session, rules, now=NOW)
        assert selection.jobs[0].title == "Higher"


class TestRepeatSuppression:
    def _mark_notified(self, session, job, when=None):
        notification = Notification(
            kind=str(NotificationKind.MORNING_DIGEST), provider="file", status="sent",
            job_count=1, created_at=when or NOW, sent_at=when or NOW,
        )
        session.add(notification)
        session.flush()
        session.add(NotificationItem(notification_id=notification.id, job_id=job.id, reason="new"))
        session.flush()

    def test_already_notified_job_is_not_resent(self, session, rules):
        job = make_job(session, cid="h")
        self._mark_notified(session, job)
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty

    def test_materially_updated_job_can_be_resent_after_cooldown(self, session, rules):
        job = make_job(session, cid="i")
        self._mark_notified(session, job, when=NOW - timedelta(hours=100))
        job.freshness = Freshness.UPDATED.value
        session.flush()
        selection = select_jobs_for_digest(session, rules, now=NOW)
        assert len(selection.jobs) == 1
        assert selection.reasons[job.id] == "update"

    def test_update_within_cooldown_is_suppressed(self, session, rules):
        job = make_job(session, cid="j")
        self._mark_notified(session, job, when=NOW - timedelta(hours=2))
        job.freshness = Freshness.UPDATED.value
        session.flush()
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty

    def test_updates_can_be_disabled(self, session, rules):
        job = make_job(session, cid="k")
        self._mark_notified(session, job, when=NOW - timedelta(hours=100))
        job.freshness = Freshness.UPDATED.value
        session.flush()
        rules.notify_on_updates = False
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty

    def test_same_job_on_a_new_source_is_not_a_new_notification(self, session, rules):
        """Moving from Indeed to LinkedIn must not re-alert."""
        job = make_job(session, cid="l")
        self._mark_notified(session, job)
        job.freshness = Freshness.OLD.value  # re-seen, not materially changed
        session.flush()
        assert select_jobs_for_digest(session, rules, now=NOW).is_empty


class TestDeadlineUrgency:
    def test_closing_soon_is_flagged(self, session, rules):
        job = make_job(session, cid="m", deadline=NOW + timedelta(days=2),
                       deadline_is_explicit=True)
        selection = select_jobs_for_digest(session, rules, now=NOW)
        assert selection.reasons[job.id] == "deadline"

    def test_distant_deadline_is_not_urgent(self, session, rules):
        job = make_job(session, cid="n", deadline=NOW + timedelta(days=60),
                       deadline_is_explicit=True)
        selection = select_jobs_for_digest(session, rules, now=NOW)
        assert selection.reasons[job.id] == "new"

    def test_deadline_is_not_invented(self, session, rules):
        job = make_job(session, cid="o", deadline=None)
        from app.web.templating import deadline_badge

        assert deadline_badge(job)["level"] == "none"


class TestDigestRendering:
    def test_digest_contains_the_essentials(self, session, rules):
        make_job(session, cid="p")
        selection = select_jobs_for_digest(session, rules, now=NOW)
        message = build_digest(selection, NotificationKind.MORNING_DIGEST, now=NOW)
        assert "NVIDIA" in message.text
        assert "FPGA Design Intern" in message.text
        assert "92/100" in message.text
        assert "Santa Clara" in message.text

    def test_digest_is_concise(self, session, rules):
        for i in range(30):
            make_job(session, cid=f"q{i}", title=f"FPGA Intern {i}")
        selection = select_jobs_for_digest(session, rules, now=NOW)
        message = build_digest(selection, NotificationKind.MORNING_DIGEST, now=NOW)
        assert len(selection.jobs) <= rules.max_jobs_per_notification
        assert len(message.text) < 4000  # fits a single Telegram message

    def test_multi_source_count_is_shown(self, session, rules):
        from app.models import JobListing

        job = make_job(session, cid="r")
        for source in ("greenhouse", "linkedin", "adzuna"):
            session.add(JobListing(job_id=job.id, source=source, source_job_id=f"{source}-1"))
        session.flush()
        session.refresh(job)
        selection = select_jobs_for_digest(session, rules, now=NOW)
        message = build_digest(selection, NotificationKind.MORNING_DIGEST, now=NOW)
        assert "3 sources" in message.text

    def test_empty_digest_message(self):
        message = build_empty_digest(NotificationKind.MORNING_DIGEST, now=NOW)
        assert "No strong new matches" in message.text

    def test_html_variant_escapes_titles(self, session, rules):
        make_job(session, cid="s", title="FPGA <Intern> & Co")
        selection = select_jobs_for_digest(session, rules, now=NOW)
        message = build_digest(selection, NotificationKind.MORNING_DIGEST, now=NOW)
        assert "&lt;Intern&gt;" in message.rich_text


class TestDispatch:
    async def test_sending_records_history_and_marks_jobs(self, session, rules, tmp_path):
        make_job(session, cid="t")
        rules.provider = "file"
        notification, result = await send_digest(
            session, rules, NotificationKind.MORNING_DIGEST, now=NOW
        )
        assert result.ok
        assert notification.status == "sent"
        assert notification.job_count == 1
        job = session.query(Job).first()
        assert job.notified is True

    async def test_second_run_sends_nothing_new(self, session, rules):
        make_job(session, cid="u")
        rules.provider = "file"
        await send_digest(session, rules, NotificationKind.MORNING_DIGEST, now=NOW)
        notification, result = await send_digest(
            session, rules, NotificationKind.MORNING_DIGEST, now=NOW
        )
        assert notification is None  # nothing worth sending

    async def test_empty_run_is_silent_by_default(self, session, rules):
        notification, result = await send_digest(
            session, rules, NotificationKind.MORNING_DIGEST, now=NOW
        )
        assert notification is None

    async def test_empty_run_can_send_a_note_when_requested(self, session, rules):
        rules.send_when_empty = True
        rules.provider = "file"
        notification, result = await send_digest(
            session, rules, NotificationKind.MORNING_DIGEST, now=NOW
        )
        assert notification is not None and result.ok

    async def test_disabled_notifications_send_nothing(self, session, rules):
        make_job(session, cid="v")
        rules.enabled = False
        notification, _ = await send_digest(
            session, rules, NotificationKind.MORNING_DIGEST, now=NOW
        )
        assert notification is None

    async def test_dry_run_does_not_mark_jobs_notified(self, session, rules):
        make_job(session, cid="w")
        rules.provider = "file"
        notification, _ = await send_digest(
            session, rules, NotificationKind.MORNING_DIGEST, now=NOW, dry_run=True
        )
        assert notification.status == "dry_run"
        assert session.query(Job).first().notified is False


class TestProviders:
    def test_unconfigured_telegram_falls_back_to_file(self, monkeypatch):
        monkeypatch.setattr(TelegramProvider, "is_configured", lambda self: False)
        assert isinstance(get_provider("telegram"), FileProvider)

    def test_unknown_provider_falls_back_to_file(self):
        assert isinstance(get_provider("carrier-pigeon"), FileProvider)

    async def test_telegram_reports_error_without_credentials(self):
        provider = TelegramProvider(token=None, chat_id=None)
        result = await provider.send(NotificationMessage(text="hi"))
        assert result.ok is False
        assert "not configured" in result.error

    async def test_file_provider_writes(self, tmp_path):
        provider = FileProvider(path=tmp_path / "notifications.jsonl")
        result = await provider.send(NotificationMessage(text="hello", subject="s"))
        assert result.ok
        assert "hello" in (tmp_path / "notifications.jsonl").read_text(encoding="utf-8")
