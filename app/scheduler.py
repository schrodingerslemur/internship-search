"""Search scheduling.

Digests fire in the user's configured timezone, morning and afternoon, on
either every day or weekdays only. Rescheduling happens live when settings are
saved -- no restart required.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import session_scope
from app.logging_setup import get_logger
from app.models.base import NotificationKind
from app.schemas.preferences import ScheduleRules

log = get_logger("scheduler")

MORNING_JOB_ID = "morning_digest"
AFTERNOON_JOB_ID = "afternoon_digest"
MAINTENANCE_JOB_ID = "nightly_maintenance"


async def _run_digest(kind: NotificationKind) -> None:
    """Scheduled entry point: run the pipeline and send the digest."""
    from app.pipeline.runner import run_search

    try:
        report = await run_search(trigger=str(kind), notify=True, notification_kind=kind)
        log.info("scheduler.run_complete", kind=str(kind), summary=report.summary())
    except Exception:
        # A scheduled failure must never kill the scheduler thread.
        log.exception("scheduler.run_failed", kind=str(kind))


async def _run_maintenance() -> None:
    """Nightly housekeeping: expire stale jobs and back off dead boards."""
    from app.pipeline.discovery import prune_failed_boards
    from app.services.persistence import expire_stale_jobs

    try:
        with session_scope() as session:
            expired = expire_stale_jobs(session)
            pruned = prune_failed_boards(session)
        log.info("scheduler.maintenance", expired=expired, pruned_boards=pruned)
    except Exception:
        log.exception("scheduler.maintenance_failed")


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour, minute = value.split(":")
        return int(hour), int(minute)
    except (ValueError, AttributeError):
        return 8, 0


def _day_of_week(cadence: str) -> str:
    return "mon-fri" if cadence == "weekdays" else "*"


def load_schedule_rules() -> ScheduleRules:
    """Read the schedule from preferences, falling back to env defaults."""
    from app.config import get_settings

    try:
        with session_scope() as session:
            from app.services.preferences import load_preferences

            return load_preferences(session).schedule
    except Exception:
        log.warning("scheduler.preferences_unavailable")
        settings = get_settings()
        return ScheduleRules(
            enabled=settings.scheduler_enabled,
            timezone=settings.timezone,
            morning_enabled=settings.morning_digest_enabled,
            morning_time=settings.morning_digest_time,
            afternoon_enabled=settings.afternoon_digest_enabled,
            afternoon_time=settings.afternoon_digest_time,
            cadence=settings.digest_schedule,
        )


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    reschedule(scheduler, load_schedule_rules())
    return scheduler


def reschedule(scheduler: AsyncIOScheduler, rules: ScheduleRules) -> None:
    """Apply schedule settings, replacing any existing jobs."""
    for job_id in (MORNING_JOB_ID, AFTERNOON_JOB_ID, MAINTENANCE_JOB_ID):
        existing = scheduler.get_job(job_id)
        if existing is not None:
            existing.remove()

    if not rules.enabled:
        log.info("scheduler.disabled_by_settings")
        return

    timezone = rules.timezone or "UTC"
    day_of_week = _day_of_week(rules.cadence)

    if rules.morning_enabled:
        hour, minute = _parse_hhmm(rules.morning_time)
        scheduler.add_job(
            _run_digest,
            CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week, timezone=timezone),
            id=MORNING_JOB_ID,
            args=[NotificationKind.MORNING_DIGEST],
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

    if rules.afternoon_enabled:
        hour, minute = _parse_hhmm(rules.afternoon_time)
        scheduler.add_job(
            _run_digest,
            CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week, timezone=timezone),
            id=AFTERNOON_JOB_ID,
            args=[NotificationKind.AFTERNOON_DIGEST],
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

    scheduler.add_job(
        _run_maintenance,
        CronTrigger(hour=3, minute=30, timezone=timezone),
        id=MAINTENANCE_JOB_ID,
        replace_existing=True,
        misfire_grace_time=7200,
        coalesce=True,
        max_instances=1,
    )

    log.info(
        "scheduler.configured",
        timezone=timezone,
        cadence=rules.cadence,
        jobs=[j.id for j in scheduler.get_jobs()],
    )


def next_digest_at(scheduler: AsyncIOScheduler | None):
    """When the next search-and-digest actually runs, in the user's timezone.

    The dashboard promises "we will look again at ..."; that promise has to come
    from the live scheduler rather than from the saved settings, because a
    schedule that failed to apply would otherwise still be advertised.
    """
    if scheduler is None:
        return None
    times = [
        job.next_run_time
        for job_id in (MORNING_JOB_ID, AFTERNOON_JOB_ID)
        if (job := scheduler.get_job(job_id)) is not None and job.next_run_time
    ]
    return min(times) if times else None


def describe_jobs(scheduler: AsyncIOScheduler | None) -> list[dict[str, str]]:
    if scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else "not scheduled",
        }
        for job in scheduler.get_jobs()
    ]
