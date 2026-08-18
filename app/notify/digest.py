"""Digest construction and the rules governing what gets sent.

"Notify selectively" is enforced here. Before a job may appear in a
notification it must pass every check in :func:`select_jobs_for_digest`:

* score at or above the configured minimum
* not already notified (unless it materially changed and the cooldown elapsed)
* not dismissed, applied to, or otherwise acted on
* still active

The result is capped at the configured maximum, so a good morning yields a
short, high-signal message rather than a wall of listings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, Notification, NotificationItem
from app.models.base import (
    Freshness,
    JobStatus,
    NotificationKind,
    Priority,
    utcnow,
)
from app.notify.base import NotificationMessage
from app.schemas.preferences import NotificationRules

#: Statuses that mean the user has already dealt with a job.
ACTED_ON: frozenset[str] = frozenset(
    {
        JobStatus.DISMISSED.value,
        JobStatus.APPLIED.value,
        JobStatus.ASSESSMENT.value,
        JobStatus.INTERVIEW.value,
        JobStatus.OFFER.value,
        JobStatus.REJECTED.value,
    }
)


@dataclass
class DigestSelection:
    jobs: list[Job] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)
    total_new: int = 0
    total_apply_now: int = 0
    total_strong: int = 0
    total_updated: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.jobs


def already_notified_job_ids(session: Session) -> set[int]:
    rows = session.scalars(select(NotificationItem.job_id)).all()
    return set(rows)


def last_notified_at(session: Session, job_id: int) -> datetime | None:
    row = session.scalar(
        select(Notification.sent_at)
        .join(NotificationItem, NotificationItem.notification_id == Notification.id)
        .where(NotificationItem.job_id == job_id, Notification.status == "sent")
        .order_by(Notification.sent_at.desc())
        .limit(1)
    )
    return row


def select_jobs_for_digest(
    session: Session,
    rules: NotificationRules,
    *,
    now: datetime | None = None,
    candidate_ids: list[int] | None = None,
) -> DigestSelection:
    """Choose which jobs deserve a place in the next notification."""
    now = now or utcnow()
    selection = DigestSelection()

    query = select(Job).where(Job.is_active.is_(True), Job.relevance_score >= rules.min_score)
    if candidate_ids:
        query = query.where(Job.id.in_(candidate_ids))
    jobs = session.scalars(query.order_by(Job.relevance_score.desc())).all()

    notified = already_notified_job_ids(session)

    for job in jobs:
        if job.status in ACTED_ON and not rules.include_dismissed:
            continue

        if job.id not in notified:
            reason = "new"
        else:
            # Already sent once: only a material change may re-surface it, and
            # only after the cooldown, so a busy job cannot spam the user.
            if not rules.notify_on_updates or job.freshness != Freshness.UPDATED.value:
                continue
            previous = last_notified_at(session, job.id)
            if previous and (now - previous) < timedelta(hours=rules.update_cooldown_hours):
                continue
            reason = "update"

        # Closing soon is worth flagging even for a mid-ranked job.
        if job.deadline and job.deadline_is_explicit:
            days_left = (job.deadline - now).days
            if 0 <= days_left <= rules.notify_deadline_within_days:
                reason = "deadline"

        selection.jobs.append(job)
        selection.reasons[job.id] = reason

    selection.total_new = sum(1 for j in selection.jobs if selection.reasons[j.id] == "new")
    selection.total_updated = sum(1 for j in selection.jobs if selection.reasons[j.id] == "update")
    selection.total_apply_now = sum(1 for j in selection.jobs if j.priority == Priority.APPLY_NOW.value)
    selection.total_strong = sum(1 for j in selection.jobs if j.priority == Priority.STRONG_MATCH.value)

    # Rank by priority first, then score, then recency.
    order = {
        Priority.APPLY_NOW.value: 0,
        Priority.STRONG_MATCH.value: 1,
        Priority.WORTH_CONSIDERING.value: 2,
        Priority.MAYBE.value: 3,
        Priority.SKIP.value: 4,
    }
    selection.jobs.sort(
        key=lambda j: (
            0 if selection.reasons[j.id] == "deadline" else 1,
            order.get(j.priority, 5),
            -(j.relevance_score or 0),
        )
    )
    selection.jobs = selection.jobs[: max(1, rules.max_jobs_per_notification)]
    return selection


def _format_date(value: datetime) -> str:
    """Format as "Aug 18" portably.

    ``%-d`` is a glibc extension and raises ValueError on Windows, so the day
    is trimmed manually instead.
    """
    return f"{value.strftime('%b')} {value.day}"


def _deadline_marker(job: Job, now: datetime) -> str:
    if not (job.deadline and job.deadline_is_explicit):
        return ""
    days = (job.deadline - now).days
    if days < 0:
        return ""
    if days <= 2:
        return f" \U0001f534 closes in {days}d"
    if days <= 7:
        return f" \U0001f7e0 closes in {days}d"
    return ""


def build_digest(
    selection: DigestSelection,
    kind: NotificationKind,
    *,
    base_url: str = "http://127.0.0.1:8000",
    now: datetime | None = None,
    stats: dict[str, int] | None = None,
) -> NotificationMessage:
    """Render the digest in plain text and Telegram-flavoured HTML."""
    now = now or utcnow()
    stats = stats or {}
    label = "Internship Search"
    date_str = _format_date(now)

    header_plain = [f"\U0001f680 {label} — {date_str}", ""]
    header_html = [f"<b>\U0001f680 {label} — {escape(date_str)}</b>", ""]

    total_found = stats.get("new_jobs", selection.total_new)
    if total_found:
        header_plain.append(f"{total_found} new internships found.")
        header_html.append(f"{total_found} new internships found.")
    if selection.total_apply_now:
        header_plain.append(f"\U0001f525 {selection.total_apply_now} worth applying to")
        header_html.append(f"\U0001f525 <b>{selection.total_apply_now}</b> worth applying to")
    if selection.total_strong:
        header_plain.append(f"⭐ {selection.total_strong} strong matches")
        header_html.append(f"⭐ <b>{selection.total_strong}</b> strong matches")
    if selection.total_updated:
        header_plain.append(f"\U0001f504 {selection.total_updated} updated")
        header_html.append(f"\U0001f504 {selection.total_updated} updated")

    body_plain = ["", "TOP MATCHES", ""]
    body_html = ["", "<b>TOP MATCHES</b>", ""]

    for index, job in enumerate(selection.jobs, start=1):
        score = int(round(job.relevance_score or 0))
        marker = _deadline_marker(job, now)
        reason_bits = (job.match_reasons or [])[:2]
        reason = "; ".join(reason_bits)
        location = job.location_raw or "Location not listed"
        sources = job.source_count

        body_plain.append(f"{index}. {job.company_name} — {job.title}")
        body_plain.append(f"{score}/100{marker}")
        if reason:
            body_plain.append(reason)
        body_plain.append(f"\U0001f4cd {location}" + (f" · {sources} sources" if sources > 1 else ""))
        body_plain.append(job.application_url)
        body_plain.append("")

        title_link = f'<a href="{escape(job.application_url, quote=True)}">{escape(job.title)}</a>'
        body_html.append(f"{index}. <b>{escape(job.company_name)}</b> — {title_link}")
        line = f"<b>{score}/100</b>{marker}"
        if reason:
            line += f" · {escape(reason)}"
        body_html.append(line)
        body_html.append(
            f"\U0001f4cd {escape(location)}" + (f" · {sources} sources" if sources > 1 else "")
        )
        body_html.append("")

    footer_plain = [f"View all → {base_url}/"]
    footer_html = [f'<a href="{escape(base_url, quote=True)}/">View all →</a>']

    text = "\n".join(header_plain + body_plain + footer_plain).strip()
    rich = "\n".join(header_html + body_html + footer_html).strip()

    return NotificationMessage(
        text=text,
        rich_text=rich,
        subject=f"{label} — {date_str}",
        job_ids=[j.id for j in selection.jobs],
        links=[(j.title, j.application_url) for j in selection.jobs],
    )


def build_empty_digest(kind: NotificationKind, *, now: datetime | None = None) -> NotificationMessage:
    now = now or utcnow()
    date_str = _format_date(now)
    text = f"\U0001f50d Internship Search — {date_str}\n\nNo strong new matches today."
    return NotificationMessage(text=text, rich_text=f"<b>{text}</b>", subject="No new matches")
