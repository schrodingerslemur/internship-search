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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Job, Notification, NotificationItem, User, UserJobState
from app.models.base import (
    Freshness,
    NotificationKind,
    Priority,
    utcnow,
)
from app.notify.base import NotificationMessage
from app.schemas.preferences import NotificationRules
from app.services import user_jobs

#: Statuses that mean this user has already dealt with a job. Re-exported from
#: the per-user state service so there is exactly one definition.
ACTED_ON = user_jobs.ACTED_ON


@dataclass
class DigestSelection:
    jobs: list[Job] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)
    total_new: int = 0
    total_apply_now: int = 0
    total_strong: int = 0
    total_updated: int = 0
    #: This user's score and reasons per job, so rendering never falls back to
    #: someone else's view of the same posting.
    scores: dict[int, float] = field(default_factory=dict)
    reasons_by_job: dict[int, list] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.jobs


def already_notified_job_ids(session: Session, user: User) -> set[int]:
    """Jobs this user has been told about. Another user's digest is irrelevant."""
    return user_jobs.notified_job_ids(session, user)


def last_notified_at(session: Session, job_id: int, user: User | None = None) -> datetime | None:
    if user is not None:
        state = user_jobs.get_state(session, user, job_id)
        if state is not None:
            return state.notified_at
        return None
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
    user: User | None = None,
    now: datetime | None = None,
    candidate_ids: list[int] | None = None,
) -> DigestSelection:
    """Choose which jobs deserve a place in this user's next notification."""
    from app.services.preferences import get_or_create_user

    now = now or utcnow()
    user = user or get_or_create_user(session)
    selection = DigestSelection()

    # Threshold against this user's own score where one exists, falling back to
    # the shared score for jobs crawled before they signed up.
    user_score = (
        select(UserJobState.relevance_score)
        .where(UserJobState.job_id == Job.id, UserJobState.user_id == user.id)
        .correlate(Job)
        .scalar_subquery()
    )
    effective = func.coalesce(user_score, Job.relevance_score)

    query = select(Job).where(Job.is_active.is_(True), effective >= rules.min_score)
    if candidate_ids:
        query = query.where(Job.id.in_(candidate_ids))
    jobs = session.scalars(query.order_by(effective.desc())).all()

    states = user_jobs.states_for(session, user, [j.id for j in jobs])

    notified = already_notified_job_ids(session, user)
    acted_on = user_jobs.acted_on_job_ids(session, user)

    for job in jobs:
        if job.id in acted_on and not rules.include_dismissed:
            continue

        if job.id not in notified:
            reason = "new"
        else:
            # Already sent once: only a material change may re-surface it, and
            # only after the cooldown, so a busy job cannot spam the user.
            if not rules.notify_on_updates or job.freshness != Freshness.UPDATED.value:
                continue
            previous = last_notified_at(session, job.id, user)
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
    def priority_for(job: Job) -> str:
        return user_jobs.priority_of(states.get(job.id), job)

    selection.total_apply_now = sum(
        1 for j in selection.jobs if priority_for(j) == Priority.APPLY_NOW.value
    )
    selection.total_strong = sum(
        1 for j in selection.jobs if priority_for(j) == Priority.STRONG_MATCH.value
    )

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
            order.get(priority_for(j), 5),
            -user_jobs.score_of(states.get(j.id), j),
        )
    )
    # Carry the per-user numbers so the rendered digest shows this user's view.
    selection.scores = {
        j.id: user_jobs.score_of(states.get(j.id), j) for j in selection.jobs
    }
    selection.reasons_by_job = {
        j.id: (states.get(j.id).match_reasons if states.get(j.id) else None) or j.match_reasons
        for j in selection.jobs
    }
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


# --------------------------------------------------------------------------
# Email rendering
#
# Email clients strip <style> blocks and ignore most modern CSS, so the HTML
# body is table-based with inline styles only -- the one layout that renders
# the same in Gmail, Outlook and Apple Mail.
# --------------------------------------------------------------------------

_PRIORITY_COLORS: dict[str, str] = {
    Priority.APPLY_NOW.value: "#d94f2b",
    Priority.STRONG_MATCH.value: "#c98a12",
    Priority.WORTH_CONSIDERING.value: "#2f7d4f",
    Priority.MAYBE.value: "#6b7280",
}

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _email_job_row(
    index: int,
    job: Job,
    now: datetime,
    *,
    score_override: float | None = None,
    reasons_override: list | None = None,
    priority_override: str | None = None,
    links: dict[str, str] | None = None,
) -> str:
    score = int(round(score_override if score_override is not None else (job.relevance_score or 0)))
    color = _PRIORITY_COLORS.get(priority_override or job.priority, "#6b7280")
    url = escape(job.application_url, quote=True)
    reasons = "; ".join((reasons_override or job.match_reasons or [])[:2])
    location = job.location_raw or "Location not listed"
    meta = escape(location)
    if job.source_count > 1:
        meta += f" &middot; {job.source_count} sources"
    deadline = _deadline_marker(job, now).strip()

    parts = [
        '<tr><td style="padding:14px 0;border-bottom:1px solid #e8e8e8;">',
        f'<div style="font:600 16px/1.35 {_FONT};color:#111;">',
        f'{index}. {escape(job.company_name)} &mdash; '
        f'<a href="{url}" style="color:#1a5fb4;text-decoration:none;">{escape(job.title)}</a>',
        "</div>",
        f'<div style="font:600 13px/1.6 {_FONT};color:{color};">{score}/100'
        + (f' <span style="color:#b3261e;">{escape(deadline)}</span>' if deadline else "")
        + "</div>",
    ]
    if reasons:
        parts.append(
            f'<div style="font:400 13px/1.5 {_FONT};color:#444;">{escape(reasons)}</div>'
        )
    parts.append(f'<div style="font:400 13px/1.5 {_FONT};color:#666;">&#128205; {meta}</div>')
    buttons = [
        f'<a href="{url}" style="font:600 13px/1 {_FONT};color:#fff;background:#1a5fb4;'
        'padding:9px 14px;border-radius:6px;text-decoration:none;display:inline-block;'
        'margin:0 6px 6px 0;">Open</a>'
    ]
    # One-click triage: acting from the phone you read the digest on is the
    # difference between clearing it and letting it pile up.
    for label, key, colour in (
        ("Applied", "applied", "#2f7d4f"),
        ("Save", "saved", "#5b6472"),
        ("Dismiss", "dismissed", "#8a8f98"),
    ):
        link = links.get(key) if links else None
        if not link:
            continue
        buttons.append(
            f'<a href="{escape(link, quote=True)}" style="font:600 13px/1 {_FONT};'
            f'color:{colour};background:#f1f3f6;padding:9px 12px;border-radius:6px;'
            'text-decoration:none;display:inline-block;margin:0 6px 6px 0;">'
            f"{label}</a>"
        )
    parts.append(f'<div style="padding-top:8px;">{"".join(buttons)}</div>')
    parts.append("</td></tr>")
    return "".join(parts)


def _email_subject(label: str, date_str: str, selection: DigestSelection) -> str:
    """A subject line that is useful in a notification preview.

    The counts go first because that is all a phone lock screen shows.
    """
    if selection.total_apply_now:
        return f"🔥 {selection.total_apply_now} to apply to — {label}, {date_str}"
    if selection.total_strong:
        return f"⭐ {selection.total_strong} strong matches — {label}, {date_str}"
    if selection.jobs:
        return f"{len(selection.jobs)} new matches — {label}, {date_str}"
    return f"{label} — {date_str}"


def build_email_html(
    selection: DigestSelection,
    *,
    title: str,
    summary_lines: list[str],
    base_url: str,
    now: datetime,
    action_links: dict[int, dict[str, str]] | None = None,
) -> str:
    """Render the digest as a standalone HTML document for email clients."""
    rows = "".join(
        _email_job_row(
            i,
            job,
            now,
            score_override=selection.scores.get(job.id),
            reasons_override=selection.reasons_by_job.get(job.id),
            links=(action_links or {}).get(job.id),
        )
        for i, job in enumerate(selection.jobs, start=1)
    )
    summary = "".join(
        f'<div style="font:400 14px/1.7 {_FONT};color:#333;">{line}</div>'
        for line in summary_lines
    )
    dash = escape(base_url, quote=True)
    body_block = (
        f'<tr><td style="padding-top:18px;font:700 12px/1 {_FONT};'
        'color:#666;letter-spacing:.08em;">TOP MATCHES</td></tr>' + rows
        if selection.jobs
        else ""
    )
    # The MIME part already declares utf-8, but some clients render the HTML in
    # a webview that only honours the meta tag -- without it the emoji and the
    # em-dashes in job titles come through as mojibake.
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f5f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:12px;padding:28px 24px;">
<tr><td style="font:700 20px/1.3 {_FONT};color:#111;padding-bottom:8px;">&#128640; {escape(title)}</td></tr>
<tr><td>{summary}</td></tr>
{body_block}
<tr><td style="padding-top:22px;">
<a href="{dash}/" style="font:600 14px/1 {_FONT};color:#1a5fb4;text-decoration:none;">View the full dashboard &rarr;</a>
</td></tr>
<tr><td style="padding-top:18px;font:400 12px/1.5 {_FONT};color:#999;">
Internship Search Agent &middot; nothing is ever submitted on your behalf.
</td></tr>
</table></td></tr></table></body></html>"""


def build_digest(
    selection: DigestSelection,
    kind: NotificationKind,
    *,
    base_url: str = "http://127.0.0.1:8000",
    now: datetime | None = None,
    stats: dict[str, int] | None = None,
    action_links: dict[int, dict[str, str]] | None = None,
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
        score = int(round(selection.scores.get(job.id, job.relevance_score or 0)))
        marker = _deadline_marker(job, now)
        reason_bits = (selection.reasons_by_job.get(job.id) or job.match_reasons or [])[:2]
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

    html = build_email_html(
        selection,
        title=f"{label} — {date_str}",
        summary_lines=[escape(line) for line in header_plain[2:]],
        base_url=base_url,
        now=now,
        action_links=action_links,
    )

    return NotificationMessage(
        text=text,
        rich_text=rich,
        html=html,
        subject=_email_subject(label, date_str, selection),
        job_ids=[j.id for j in selection.jobs],
        links=[(j.title, j.application_url) for j in selection.jobs],
    )


def build_empty_digest(kind: NotificationKind, *, now: datetime | None = None) -> NotificationMessage:
    now = now or utcnow()
    date_str = _format_date(now)
    text = f"\U0001f50d Internship Search — {date_str}\n\nNo strong new matches today."
    html = build_email_html(
        DigestSelection(),
        title=f"Internship Search — {date_str}",
        summary_lines=["No strong new matches this run."],
        base_url=get_settings().public_base_url or "http://127.0.0.1:8000",
        now=now,
    )
    return NotificationMessage(
        text=text,
        rich_text=f"<b>{text}</b>",
        html=html,
        subject=f"Internship Search — no new matches ({date_str})",
    )
