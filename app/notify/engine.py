"""Notification dispatch and history.

Every send is recorded with the exact jobs it contained, which is what makes
"do not notify me about this twice" enforceable across runs and restarts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Job, Notification, NotificationItem, User
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
from app.services import user_jobs

log = get_logger("notify.engine")


def _action_links(
    selection: DigestSelection, user: User, base_url: str, key: bytes | None
) -> dict[int, dict[str, str]]:
    """One signed link per job per action, for triage straight from the inbox.

    Without a signing key -- a CLI run that never started the web app -- the
    buttons are simply omitted rather than rendered broken.
    """
    if not key:
        return {}
    from app.services import action_tokens

    root = base_url.rstrip("/")
    return {
        job.id: {
            action: f"{root}/a/{action_tokens.issue(user.id, job.id, action, key)}"
            for action in ("applied", "saved", "dismissed")
        }
        for job in selection.jobs
    }


async def send_digest(
    session: Session,
    rules: NotificationRules,
    kind: NotificationKind,
    *,
    user: User | None = None,
    signing_key: bytes | None = None,
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

    from app.services.preferences import get_or_create_user

    user = user or get_or_create_user(session)
    selection = select_jobs_for_digest(
        session, rules, user=user, now=now, candidate_ids=candidate_ids
    )

    if selection.is_empty:
        if not rules.send_when_empty:
            log.info("notify.skipped_empty")
            return None, None
        message = build_empty_digest(kind, now=now)
    else:
        message = build_digest(
            selection,
            kind,
            base_url=base_url,
            now=now,
            stats=stats,
            action_links=_action_links(selection, user, base_url, signing_key),
        )

    return await _dispatch(
        session, rules, kind, message, selection, user=user, run_id=run_id, now=now,
        dry_run=dry_run,
    )


async def _dispatch(
    session: Session,
    rules: NotificationRules,
    kind: NotificationKind,
    message: NotificationMessage,
    selection: DigestSelection,
    *,
    user: User,
    run_id: int | None,
    now: datetime,
    dry_run: bool,
) -> tuple[Notification, SendResult]:
    from app.services import notify_config

    provider = get_provider(
        rules.provider,
        recipient=user.notification_email,
        config=notify_config.load(session),
    )

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
            user_jobs.mark_notified(session, user, job, now=now)
        log.info(
            "notify.sent", provider=provider.name, jobs=len(selection.jobs), user_id=user.id
        )
    else:
        notification.status = "failed"
        notification.error = result.error
        log.warning("notify.failed", provider=provider.name, error=result.error)

    session.flush()
    return notification, result


async def send_test_notification(
    session: Session, provider_name: str, *, recipient: str | None = None
) -> SendResult:
    """Send a one-off message so the user can verify their setup."""
    from app.services import notify_config

    provider = get_provider(
        provider_name, recipient=recipient, config=notify_config.load(session)
    )
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
