"""Notification dispatch and history.

Every send is recorded with the exact jobs it contained, which is what makes
"do not notify me about this twice" enforceable across runs and restarts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Job, Notification, NotificationItem
from app.models.base import NotificationKind, utcnow
from app.notify.base import NotificationMessage, SendResult
from app.notify.digest import (
    DigestSelection,
    build_digest,
    build_empty_digest,
    select_jobs_for_digest,
)
from app.notify.providers import get_provider
from app.schemas.preferences import NotificationRules

log = get_logger("notify.engine")


async def send_digest(
    session: Session,
    rules: NotificationRules,
    kind: NotificationKind,
    *,
    run_id: int | None = None,
    base_url: str = "http://127.0.0.1:8000",
    stats: dict[str, int] | None = None,
    candidate_ids: list[int] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> tuple[Notification | None, SendResult | None]:
    """Build and dispatch a digest, honouring every notification rule."""
    now = now or utcnow()
    if not rules.enabled:
        log.info("notify.disabled")
        return None, None

    selection = select_jobs_for_digest(session, rules, now=now, candidate_ids=candidate_ids)

    if selection.is_empty:
        if not rules.send_when_empty:
            log.info("notify.skipped_empty")
            return None, None
        message = build_empty_digest(kind, now=now)
    else:
        message = build_digest(selection, kind, base_url=base_url, now=now, stats=stats)

    return await _dispatch(
        session, rules, kind, message, selection, run_id=run_id, now=now, dry_run=dry_run
    )


async def _dispatch(
    session: Session,
    rules: NotificationRules,
    kind: NotificationKind,
    message: NotificationMessage,
    selection: DigestSelection,
    *,
    run_id: int | None,
    now: datetime,
    dry_run: bool,
) -> tuple[Notification, SendResult]:
    provider = get_provider(rules.provider)

    notification = Notification(
        kind=str(kind),
        provider=provider.name,
        status="pending",
        subject=message.subject,
        body=message.text,
        job_count=len(selection.jobs),
        run_id=run_id,
        created_at=now,
    )
    session.add(notification)
    session.flush()

    if dry_run:
        notification.status = "dry_run"
        session.flush()
        return notification, SendResult(True, provider.name, detail="dry run")

    result = await provider.send(message)

    if result.ok:
        notification.status = "sent"
        notification.sent_at = now
        for job in selection.jobs:
            session.add(
                NotificationItem(
                    notification_id=notification.id,
                    job_id=job.id,
                    reason=selection.reasons.get(job.id, "new"),
                    score=job.relevance_score,
                )
            )
            job.notified = True
            job.notified_at = now
        log.info("notify.sent", provider=provider.name, jobs=len(selection.jobs))
    else:
        notification.status = "failed"
        notification.error = result.error
        log.warning("notify.failed", provider=provider.name, error=result.error)

    session.flush()
    return notification, result


async def send_test_notification(session: Session, provider_name: str) -> SendResult:
    """Send a one-off message so the user can verify their setup."""
    provider = get_provider(provider_name)
    message = NotificationMessage(
        text=(
            "✅ Internship Search is connected.\n\n"
            "This is a test notification. Digests will arrive on your schedule."
        ),
        rich_text=(
            "<b>✅ Internship Search is connected.</b>\n\n"
            "This is a test notification. Digests will arrive on your schedule."
        ),
        subject="Test notification",
    )
    result = await provider.send(message)
    session.add(
        Notification(
            kind=str(NotificationKind.TEST),
            provider=provider.name,
            status="sent" if result.ok else "failed",
            subject=message.subject,
            body=message.text,
            job_count=0,
            error=result.error,
            created_at=utcnow(),
            sent_at=utcnow() if result.ok else None,
        )
    )
    session.flush()
    return result


def notification_history(session: Session, limit: int = 50) -> list[Notification]:
    from sqlalchemy import select

    return list(
        session.scalars(
            select(Notification).order_by(Notification.created_at.desc()).limit(limit)
        ).all()
    )


def jobs_in_notification(session: Session, notification_id: int) -> list[Job]:
    from sqlalchemy import select

    return list(
        session.scalars(
            select(Job)
            .join(NotificationItem, NotificationItem.job_id == Job.id)
            .where(NotificationItem.notification_id == notification_id)
        ).all()
    )
