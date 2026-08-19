"""Scheduling: digest times, timezones, cadence, and duplicate prevention."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app.scheduler import (
    AFTERNOON_JOB_ID,
    MAINTENANCE_JOB_ID,
    MORNING_JOB_ID,
    _day_of_week,
    _parse_hhmm,
    describe_jobs,
    reschedule,
)
from app.schemas.preferences import ScheduleRules


@pytest.fixture
def scheduler():
    s = BackgroundScheduler()
    yield s
    if s.running:  # pragma: no cover - defensive
        s.shutdown(wait=False)


class TestTimeParsing:
    @pytest.mark.parametrize(
        "value,expected", [("08:00", (8, 0)), ("16:30", (16, 30)), ("00:05", (0, 5))]
    )
    def test_valid_times(self, value, expected):
        assert _parse_hhmm(value) == expected

    def test_invalid_time_falls_back_safely(self):
        assert _parse_hhmm("not-a-time") == (8, 0)

    def test_schedule_rules_validate_and_normalise(self):
        rules = ScheduleRules(morning_time="8:5")
        assert rules.morning_time == "08:05"

    def test_out_of_range_time_is_rejected(self):
        with pytest.raises(ValueError):
            ScheduleRules(morning_time="25:00")

    def test_non_numeric_time_is_rejected(self):
        with pytest.raises(ValueError):
            ScheduleRules(afternoon_time="noon")


class TestCadence:
    def test_every_day(self):
        assert _day_of_week("all") == "*"

    def test_weekdays_only(self):
        assert _day_of_week("weekdays") == "mon-fri"


class TestRegistration:
    def test_both_digests_are_scheduled(self, scheduler):
        reschedule(scheduler, ScheduleRules())
        ids = {j.id for j in scheduler.get_jobs()}
        assert MORNING_JOB_ID in ids
        assert AFTERNOON_JOB_ID in ids
        assert MAINTENANCE_JOB_ID in ids

    def test_disabling_the_afternoon_digest(self, scheduler):
        reschedule(scheduler, ScheduleRules(afternoon_enabled=False))
        ids = {j.id for j in scheduler.get_jobs()}
        assert MORNING_JOB_ID in ids
        assert AFTERNOON_JOB_ID not in ids

    def test_disabling_the_scheduler_removes_everything(self, scheduler):
        reschedule(scheduler, ScheduleRules())
        reschedule(scheduler, ScheduleRules(enabled=False))
        assert scheduler.get_jobs() == []

    def test_rescheduling_does_not_duplicate_jobs(self, scheduler):
        for _ in range(4):
            reschedule(scheduler, ScheduleRules())
        ids = [j.id for j in scheduler.get_jobs()]
        assert len(ids) == len(set(ids))
        assert len(ids) == 3

    def test_configured_time_is_used(self, scheduler):
        reschedule(scheduler, ScheduleRules(morning_time="06:45", timezone="America/New_York"))
        trigger = scheduler.get_job(MORNING_JOB_ID).trigger
        fields = {f.name: str(f) for f in trigger.fields}
        assert fields["hour"] == "6"
        assert fields["minute"] == "45"

    def test_timezone_is_applied(self, scheduler):
        reschedule(scheduler, ScheduleRules(timezone="America/Los_Angeles"))
        trigger = scheduler.get_job(MORNING_JOB_ID).trigger
        assert "Los_Angeles" in str(trigger.timezone)

    def test_weekday_cadence_reaches_the_trigger(self, scheduler):
        reschedule(scheduler, ScheduleRules(cadence="weekdays"))
        trigger = scheduler.get_job(MORNING_JOB_ID).trigger
        fields = {f.name: str(f) for f in trigger.fields}
        assert fields["day_of_week"] == "mon-fri"

    def test_next_run_times_are_reported(self, scheduler):
        reschedule(scheduler, ScheduleRules())
        scheduler.start(paused=True)
        described = describe_jobs(scheduler)
        assert len(described) == 3
        assert all("next_run" in d for d in described)

    def test_describe_handles_no_scheduler(self):
        assert describe_jobs(None) == []


class TestMisfireHandling:
    def test_jobs_coalesce_and_limit_concurrency(self, scheduler):
        """A laptop waking from sleep must not fire three overlapping runs."""
        reschedule(scheduler, ScheduleRules())
        job = scheduler.get_job(MORNING_JOB_ID)
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time and job.misfire_grace_time >= 600


class TestScheduleTimezoneBehaviour:
    def test_next_fire_respects_timezone(self, scheduler):
        """8am Eastern and 8am Pacific are different instants."""
        reschedule(scheduler, ScheduleRules(timezone="America/New_York", morning_time="08:00"))
        eastern = scheduler.get_job(MORNING_JOB_ID).trigger

        scheduler2 = BackgroundScheduler()
        reschedule(scheduler2, ScheduleRules(timezone="America/Los_Angeles", morning_time="08:00"))
        pacific = scheduler2.get_job(MORNING_JOB_ID).trigger

        reference = datetime(2026, 8, 18, 0, 0)
        east_next = eastern.get_next_fire_time(None, reference.astimezone(eastern.timezone))
        west_next = pacific.get_next_fire_time(None, reference.astimezone(pacific.timezone))
        assert east_next.utcoffset() != west_next.utcoffset()


class TestDigestKindResolution:
    """An externally scheduled run must label itself correctly."""

    def test_explicit_kinds_are_honoured(self):
        from app.cli import _resolve_kind
        from app.models.base import NotificationKind

        assert _resolve_kind("morning") is NotificationKind.MORNING_DIGEST
        assert _resolve_kind("afternoon") is NotificationKind.AFTERNOON_DIGEST

    def test_auto_reads_the_configured_timezone(self, monkeypatch):
        """08:00 in New York is 12:00 UTC -- the label must follow the user."""
        import app.cli as cli
        from app.models.base import NotificationKind

        class FakeDateTime:
            @staticmethod
            def now(tz=None):
                from datetime import datetime as real

                utc_noon = real(2026, 8, 19, 12, 30, tzinfo=UTC)
                return utc_noon.astimezone(tz) if tz else utc_noon

        monkeypatch.setattr(cli, "datetime", FakeDateTime)
        assert cli._resolve_kind("auto") is NotificationKind.MORNING_DIGEST

    def test_auto_picks_afternoon_later_in_the_day(self, monkeypatch):
        import app.cli as cli
        from app.models.base import NotificationKind

        class FakeDateTime:
            @staticmethod
            def now(tz=None):
                from datetime import datetime as real

                utc_eight_pm = real(2026, 8, 19, 20, 30, tzinfo=UTC)
                return utc_eight_pm.astimezone(tz) if tz else utc_eight_pm

        monkeypatch.setattr(cli, "datetime", FakeDateTime)
        assert cli._resolve_kind("auto") is NotificationKind.AFTERNOON_DIGEST
