"""Jinja2 environment and template filters."""

from __future__ import annotations

from datetime import datetime

from fastapi.templating import Jinja2Templates

from app.config import PROJECT_ROOT
from app.models.base import Priority, utcnow

TEMPLATE_DIR = PROJECT_ROOT / "app" / "web" / "templates"


def _account_context(request) -> dict:
    """Make the signed-in account available to every template.

    A context processor rather than a per-route context key, so adding a page
    can never accidentally lose the account menu.
    """
    return {"current_user": getattr(request.state, "user", None)}


#: The nav badges read `counts`, which every page behind the nav passes from its
#: own request-scoped session. It is deliberately *not* a context processor:
#: processors receive only the request, so one would have to open a second
#: session of its own -- bypassing the session this request is already using,
#: and any override placed on top of it. A page that omits `counts` renders the
#: tabs without badges rather than failing.
templates = Jinja2Templates(directory=str(TEMPLATE_DIR), context_processors=[_account_context])


def timeago(value: datetime | None) -> str:
    if not value:
        return "unknown"
    delta = utcnow() - value
    seconds = delta.total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return "just now" if minutes < 1 else f"{minutes}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'' if months == 1 else 's'} ago"
    years = days // 365
    return f"{years} year{'' if years == 1 else 's'} ago"


def deadline_badge(job) -> dict[str, str]:
    """Traffic-light deadline indicator. Absent deadlines are never invented."""
    if not (getattr(job, "deadline", None) and getattr(job, "deadline_is_explicit", False)):
        return {"level": "none", "text": "No deadline listed", "emoji": "\U0001f7e2"}
    days = (job.deadline - utcnow()).days
    if days < 0:
        return {"level": "passed", "text": "Deadline passed", "emoji": "⚫"}
    if days <= 2:
        return {"level": "urgent", "text": f"Deadline in {days} day{'s' if days != 1 else ''}",
                "emoji": "\U0001f534"}
    if days <= 7:
        return {"level": "soon", "text": f"Deadline in {days} days", "emoji": "\U0001f7e0"}
    return {"level": "ok", "text": f"Deadline in {days} days", "emoji": "\U0001f7e2"}


def priority_meta(value: str) -> dict[str, str]:
    try:
        priority = Priority(value)
    except ValueError:
        priority = Priority.SKIP
    return {
        "emoji": priority.emoji,
        "label": priority.label,
        "slug": priority.value,
    }


#: How each lifecycle state is named and marked in the UI. The icon is a
#: redundant cue, never the only one: the label always travels with it, so the
#: state is legible without colour vision and to a screen reader.
STATUS_META: dict[str, dict[str, str]] = {
    "new": {"label": "New", "icon": "✨"},
    "saved": {"label": "Saved", "icon": "📌"},
    "applied": {"label": "Applied", "icon": "✓"},
    "assessment": {"label": "Assessment", "icon": "📝"},
    "interview": {"label": "Interview", "icon": "🗣"},
    "offer": {"label": "Offer", "icon": "🎉"},
    "rejected": {"label": "Rejected", "icon": "✕"},
    "dismissed": {"label": "Dismissed", "icon": "🚫"},
    "expired": {"label": "Expired", "icon": "⏳"},
}


def status_meta(value: str | None) -> dict[str, str]:
    return STATUS_META.get(
        str(value or "new"), {"label": humanize(value), "icon": "•"}
    )


REMOTE_META: dict[str, dict[str, str]] = {
    "remote": {"label": "Remote", "icon": "🏠"},
    "hybrid": {"label": "Hybrid", "icon": "🔄"},
    "onsite": {"label": "On-site", "icon": "🏢"},
}


def remote_meta(value: str | None) -> dict[str, str]:
    return REMOTE_META.get(str(value or ""), {"label": humanize(value), "icon": "📍"})


def when_text(value: datetime | None, timezone: str = "America/New_York") -> str:
    """An absolute time, in the user's timezone rather than the server's.

    Stored timestamps are naive UTC. "Tomorrow at 08:00" has to mean 08:00 where
    the user is, or the promise the dashboard makes about the next search is
    simply wrong for most of the day.
    """
    if not value:
        return ""
    from datetime import UTC
    from zoneinfo import ZoneInfo

    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        local = aware.astimezone(ZoneInfo(timezone))
    except Exception:
        local = aware
    today = datetime.now(local.tzinfo).date()
    delta = (local.date() - today).days
    day = {0: "today", 1: "tomorrow"}.get(delta) or local.strftime("%a %d %b")
    return f"{day} at {local.strftime('%H:%M')}"


def salary_text(job) -> str:
    if job.salary_min is None:
        return "Not listed"
    currency = job.salary_currency or "USD"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    period = {"hourly": "/hr", "yearly": "/yr", "monthly": "/mo"}.get(job.salary_period or "", "")

    def fmt(value: float) -> str:
        return f"{value:,.0f}" if value >= 1000 else f"{value:,.2f}".rstrip("0").rstrip(".")

    if job.salary_max and job.salary_max > job.salary_min:
        return f"{symbol}{fmt(job.salary_min)}–{symbol}{fmt(job.salary_max)}{period}"
    return f"{symbol}{fmt(job.salary_min)}{period}"


def humanize(value: str | None) -> str:
    if not value:
        return ""
    return str(value).replace("_", " ").title()


def score_class(score: float | None) -> str:
    value = score or 0
    if value >= 90:
        return "score-90"
    if value >= 80:
        return "score-80"
    if value >= 70:
        return "score-70"
    if value >= 60:
        return "score-60"
    return "score-low"


templates.env.filters["timeago"] = timeago
templates.env.filters["salary"] = salary_text
templates.env.filters["humanize"] = humanize
templates.env.filters["score_class"] = score_class
templates.env.filters["when"] = when_text
templates.env.globals["deadline_badge"] = deadline_badge
templates.env.globals["status_meta"] = status_meta
templates.env.globals["remote_meta"] = remote_meta
templates.env.globals["priority_meta"] = priority_meta
templates.env.globals["now"] = utcnow
