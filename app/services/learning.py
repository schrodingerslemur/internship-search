"""Learning from application outcomes.

Compares interview rates across the kinds of roles actually applied to, then
suggests weighting changes. Deliberately conservative: with a handful of
applications, apparent differences are noise, so nothing is recommended until
there is enough data to mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Job
from app.pipeline.textutil import normalize_text

#: Below this many applications, no recommendations are produced at all.
MIN_APPLICATIONS_FOR_INSIGHTS = 15
#: A single category needs at least this many applications to be compared.
MIN_PER_CATEGORY = 5

#: Role families used to bucket applications. Derived from job titles/skills.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "FPGA": ("fpga", "programmable logic"),
    "Hardware Verification": ("verification", "uvm", "dv "),
    "RTL / Digital Design": ("rtl", "digital design", "logic design"),
    "ASIC / SoC": ("asic", "soc", "silicon"),
    "Computer Architecture": ("architecture", "microarchitecture"),
    "Embedded / Firmware": ("embedded", "firmware", "driver"),
    "Hardware Security": ("hardware security", "side channel", "secure boot"),
    "Machine Learning": ("machine learning", "deep learning", "ml ", "ai "),
    "Software": ("software", "swe", "full stack", "backend", "frontend"),
}


@dataclass
class CategoryOutcome:
    name: str
    applications: int = 0
    interviews: int = 0
    offers: int = 0
    rejections: int = 0

    @property
    def interview_rate(self) -> float:
        return (self.interviews / self.applications * 100) if self.applications else 0.0

    @property
    def offer_rate(self) -> float:
        return (self.offers / self.applications * 100) if self.applications else 0.0


@dataclass
class Insights:
    total_applications: int = 0
    total_interviews: int = 0
    total_offers: int = 0
    categories: list[CategoryOutcome] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    has_enough_data: bool = False
    applications_needed: int = MIN_APPLICATIONS_FOR_INSIGHTS

    @property
    def interview_rate(self) -> float:
        return (
            self.total_interviews / self.total_applications * 100
            if self.total_applications
            else 0.0
        )


def categorize(job: Job) -> str:
    hay = normalize_text(f"{job.title} {' '.join(job.skills or [])}")
    for name, needles in CATEGORIES.items():
        if any(needle in hay for needle in needles):
            return name
    return "Other"


def outcome_insights(session: Session) -> Insights:
    """Analyse what the user's applications actually produced."""
    rows = session.execute(
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(Application.date_applied.is_not(None))
    ).all()

    insights = Insights(total_applications=len(rows))
    buckets: dict[str, CategoryOutcome] = {}

    for application, job in rows:
        name = categorize(job)
        bucket = buckets.setdefault(name, CategoryOutcome(name=name))
        bucket.applications += 1
        if application.date_interview:
            bucket.interviews += 1
            insights.total_interviews += 1
        if application.date_offer:
            bucket.offers += 1
            insights.total_offers += 1
        if application.date_rejected:
            bucket.rejections += 1

    insights.categories = sorted(
        buckets.values(), key=lambda c: (-c.interview_rate, -c.applications)
    )

    if insights.total_applications < MIN_APPLICATIONS_FOR_INSIGHTS:
        insights.has_enough_data = False
        insights.applications_needed = MIN_APPLICATIONS_FOR_INSIGHTS - insights.total_applications
        return insights

    insights.has_enough_data = True
    comparable = [c for c in insights.categories if c.applications >= MIN_PER_CATEGORY]
    if len(comparable) >= 2:
        best, worst = comparable[0], comparable[-1]
        if best.interview_rate > worst.interview_rate * 1.5 and best.interviews > 0:
            insights.recommendations.append(
                f"Your strongest response rate is coming from {best.name} roles "
                f"({best.interview_rate:.0f}% interview rate across {best.applications} "
                f"applications, versus {worst.interview_rate:.0f}% for {worst.name}). "
                f"Consider increasing the weighting for {best.name}."
            )
    if insights.interview_rate < 5 and insights.total_applications >= 25:
        insights.recommendations.append(
            "Your overall interview rate is low. Consider raising your minimum "
            "score threshold so you concentrate effort on stronger matches."
        )
    return insights


def analytics_payload(session: Session) -> dict[str, Any]:
    insights = outcome_insights(session)
    return {
        "total_applications": insights.total_applications,
        "total_interviews": insights.total_interviews,
        "total_offers": insights.total_offers,
        "interview_rate": round(insights.interview_rate, 1),
        "has_enough_data": insights.has_enough_data,
        "applications_needed": insights.applications_needed,
        "categories": [
            {
                "name": c.name,
                "applications": c.applications,
                "interviews": c.interviews,
                "offers": c.offers,
                "interview_rate": round(c.interview_rate, 1),
            }
            for c in insights.categories
        ],
        "recommendations": insights.recommendations,
    }
