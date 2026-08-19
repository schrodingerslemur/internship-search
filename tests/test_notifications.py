"""Notification selection rules, digest rendering, and delivery."""

from __future__ import annotations

import smtplib
from datetime import timedelta

import pytest

from app.models import Job, Notification, NotificationItem
from app.models.base import Freshness, JobStatus, NotificationKind, Priority
from app.notify.base import NotificationMessage
from app.notify.digest import build_digest, build_empty_digest, select_jobs_for_digest
from app.notify.engine import send_digest
from app.notify.providers import EmailProvider, FileProvider, TelegramProvider, get_provider
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


def configured_email_provider(**overrides) -> EmailProvider:
    """An EmailProvider with credentials, independent of the ambient .env."""
    provider = EmailProvider()
    provider.host = "smtp.example.com"
    provider.port = 587
    provider.user = "me@example.com"
    provider.password = "app-password"
    provider.starttls = True
    provider.sender = "me@example.com"
    provider.recipients = ["you@andrew.cmu.edu"]
    for key, value in overrides.items():
        setattr(provider, key, value)
    return provider


class TestEmailProvider:
    def test_unconfigured_without_credentials(self):
        assert configured_email_provider(password=None).is_configured() is False
        assert configured_email_provider(recipients=[]).is_configured() is False

    async def test_reports_error_rather_than_raising(self):
        result = await configured_email_provider(host=None).send(NotificationMessage(text="hi"))
        assert result.ok is False
        assert "not configured" in result.error

    def test_message_is_multipart_with_text_and_html(self):
        mail = configured_email_provider()._build(
            NotificationMessage(text="plain body", subject="Digest", html="<p>rich body</p>")
        )
        assert mail["Subject"] == "Digest"
        assert "me@example.com" in mail["From"]
        assert mail["To"] == "you@andrew.cmu.edu"
        types = {part.get_content_type() for part in mail.walk()}
        assert "text/plain" in types
        assert "text/html" in types
        assert "plain body" in mail.get_body(("plain",)).get_content()
        assert "rich body" in mail.get_body(("html",)).get_content()

    def test_text_only_message_still_sends(self):
        """A provider must never require the HTML variant to exist."""
        mail = configured_email_provider()._build(NotificationMessage(text="just text"))
        assert mail.get_content_type() == "text/plain"
        assert "just text" in mail.get_content()

    def test_multiple_recipients_are_addressed(self):
        provider = configured_email_provider(recipients=["a@x.com", "b@y.com"])
        assert provider._build(NotificationMessage(text="t"))["To"] == "a@x.com, b@y.com"

    async def test_successful_send_uses_starttls_and_login(self, monkeypatch):
        calls: dict[str, object] = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                calls["host"], calls["port"] = host, port

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ehlo(self):
                calls["ehlo"] = calls.get("ehlo", 0) + 1

            def starttls(self, context=None):
                calls["starttls"] = True

            def login(self, user, password):
                calls["login"] = (user, password)

            def send_message(self, mail):
                calls["sent_subject"] = mail["Subject"]

        monkeypatch.setattr("app.notify.providers.smtplib.SMTP", FakeSMTP)
        result = await configured_email_provider().send(
            NotificationMessage(text="body", subject="Internship Search — Aug 18")
        )

        assert result.ok
        assert calls["host"] == "smtp.example.com"
        assert calls["starttls"] is True
        assert calls["login"] == ("me@example.com", "app-password")
        assert calls["sent_subject"] == "Internship Search — Aug 18"

    async def test_port_465_uses_implicit_tls(self, monkeypatch):
        used = {}

        class FakeSMTPSSL:
            def __init__(self, host, port, timeout=None, context=None):
                used["ssl"] = True

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                pass

            def send_message(self, mail):
                pass

        monkeypatch.setattr("app.notify.providers.smtplib.SMTP_SSL", FakeSMTPSSL)
        result = await configured_email_provider(port=465).send(NotificationMessage(text="b"))
        assert result.ok
        assert used["ssl"] is True

    async def test_auth_failure_explains_app_passwords(self, monkeypatch):
        def boom(self, mail):
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

        monkeypatch.setattr(EmailProvider, "_send_sync", boom)
        result = await configured_email_provider().send(NotificationMessage(text="b"))
        assert result.ok is False
        assert "app password" in result.error

    async def test_transport_failure_is_reported_not_raised(self, monkeypatch):
        def boom(self, mail):
            raise OSError("connection refused")

        monkeypatch.setattr(EmailProvider, "_send_sync", boom)
        result = await configured_email_provider().send(NotificationMessage(text="b"))
        assert result.ok is False
        assert "connection refused" in result.error

    def test_configured_email_is_preferred_over_unconfigured_telegram(self, monkeypatch):
        """A deploy with SMTP set must not silently degrade to a file."""
        monkeypatch.setattr(TelegramProvider, "is_configured", lambda self: False)
        monkeypatch.setattr(EmailProvider, "is_configured", lambda self: True)
        assert isinstance(get_provider("telegram"), EmailProvider)


class TestEmailRendering:
    def test_html_digest_is_a_standalone_document(self, session, rules):
        make_job(session, title="FPGA Design Intern")
        selection = select_jobs_for_digest(session, rules, now=NOW)
        message = build_digest(
            selection, NotificationKind.MORNING_DIGEST, base_url="https://example.fly.dev", now=NOW
        )
        assert message.html.startswith("<!doctype html>")
        assert "FPGA Design Intern" in message.html
        assert "https://example.fly.dev/" in message.html

    def test_html_digest_escapes_job_text(self, session, rules):
        make_job(session, title="Intern <script>alert(1)</script>", cid="xss")
        selection = select_jobs_for_digest(session, rules, now=NOW)
        message = build_digest(selection, NotificationKind.MORNING_DIGEST, now=NOW)
        assert "<script>" not in message.html
        assert "&lt;script&gt;" in message.html

    def test_subject_leads_with_the_count(self, session, rules):
        make_job(session, title="FPGA Design Intern")
        selection = select_jobs_for_digest(session, rules, now=NOW)
        message = build_digest(selection, NotificationKind.MORNING_DIGEST, now=NOW)
        assert "1 to apply to" in message.subject

    def test_empty_digest_has_html_too(self):
        message = build_empty_digest(NotificationKind.MORNING_DIGEST, now=NOW)
        assert message.html.startswith("<!doctype html>")
        assert "No strong new matches" in message.html
