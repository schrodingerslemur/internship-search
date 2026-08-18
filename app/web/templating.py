"""Jinja2 environment and template filters."""

from __future__ import annotations

from datetime import datetime

from fastapi.templating import Jinja2Templates

from app.config import PROJECT_ROOT
from app.models.base import Priority, utcnow

TEMPLATE_DIR = PROJECT_ROOT / "app" / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


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
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


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
templates.env.globals["deadline_badge"] = deadline_badge
templates.env.globals["priority_meta"] = priority_meta
templates.env.globals["now"] = utcnow
