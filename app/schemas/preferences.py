"""User-editable search preferences.

Everything the ranking engine consults lives here: roles, keywords, location
weights, company preferences, internship constraints, scoring weights, priority
thresholds, and notification rules. Nothing in the pipeline hardcodes a company,
role, location, or weight -- defaults below are only a starting point and every
field is editable from the settings page.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TargetRole(BaseModel):
    """One role the user is searching for."""

    name: str
    #: Relative importance among roles. Higher wins ties during ranking.
    weight: float = 1.0
    enabled: bool = True
    #: Display/priority order in the settings UI.
    order: int = 0
    #: Extra query strings for this role, beyond automatic expansion.
    extra_queries: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("role name must not be empty")
        return v


class KeywordPrefs(BaseModel):
    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)
    #: Negative keywords that disqualify outright rather than just deducting.
    hard_exclude: list[str] = Field(default_factory=list)
    positive_points_each: float = 3.0
    negative_points_each: float = -6.0
    #: Cap on total positive keyword contribution, to stop keyword stuffing.
    positive_cap: float = 30.0


class LocationRule(BaseModel):
    """A location preference with its ranking bonus.

    ``pattern`` is matched case-insensitively as a substring against the job's
    location string, so "Bay Area" style aliases work via ``aliases``.
    """

    pattern: str
    bonus: float = 0.0
    aliases: list[str] = Field(default_factory=list)
    #: Excluded locations are filtered out. Nothing is excluded by default.
    excluded: bool = False

    def matches(self, location: str) -> bool:
        hay = (location or "").lower()
        if not hay:
            return False
        needles = [self.pattern] + list(self.aliases)
        return any(n.strip().lower() in hay for n in needles if n.strip())


class LocationPrefs(BaseModel):
    rules: list[LocationRule] = Field(default_factory=list)
    remote_bonus: float = 7.0
    hybrid_bonus: float = 5.0
    onsite_bonus: float = 0.0
    #: Applied when a US location matches no specific rule.
    other_us_bonus: float = 2.0
    international_bonus: float = 0.0
    #: Applied when location cannot be determined. Never treated as exclusion.
    unknown_bonus: float = 0.0
    #: Countries you can actually work in, as two-letter codes. Empty means no
    #: restriction. This is a hard filter -- somewhere you cannot take a job is
    #: not a low-scoring job, it is not a job -- but only ever against a country
    #: the posting actually stated. A listing whose country could not be parsed
    #: is never excluded, in keeping with "unknown is not disqualifying".
    allowed_countries: list[str] = Field(default_factory=list)

    @field_validator("allowed_countries")
    @classmethod
    def _normalise_countries(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v:
            code = str(item).strip().upper()
            if code and code not in out:
                out.append(code)
        return out

    def country_allowed(self, country: str | None) -> bool:
        """Whether a posting in this country is acceptable at all."""
        if not self.allowed_countries:
            return True
        if not country or not str(country).strip():
            return True  # unknown is never disqualifying
        from app.pipeline.extract import country_code

        return country_code(country) in self.allowed_countries

    def bonus_for(self, location: str | None) -> tuple[float, str]:
        """Return (bonus, explanation) for a location string."""
        if not location:
            return self.unknown_bonus, "Location unknown"
        for rule in self.rules:
            if rule.excluded and rule.matches(location):
                return -999.0, f"Excluded location: {rule.pattern}"
        best: tuple[float, str] | None = None
        for rule in self.rules:
            if rule.matches(location):
                if best is None or rule.bonus > best[0]:
                    best = (rule.bonus, f"Preferred location: {rule.pattern}")
        if best:
            return best
        return self.other_us_bonus, "Other location"


class CompanyPrefs(BaseModel):
    """Company preferences.

    These bias ranking only. The search universe is never restricted to
    ``preferred`` -- see ``app.pipeline.discovery``.
    """

    preferred: list[str] = Field(default_factory=list)
    blacklisted: list[str] = Field(default_factory=list)
    #: Companies whose careers pages get extra monitoring (supplementary only).
    monitored: list[str] = Field(default_factory=list)
    preferred_types: list[str] = Field(default_factory=list)
    preferred_bonus: float = 8.0
    preferred_type_bonus: float = 4.0
    #: Bonus for a company the system has never surfaced before, to reward
    #: discovery instead of entrenching known names.
    new_company_bonus: float = 0.0


class InternshipConstraints(BaseModel):
    internship_only: bool = True
    #: Accepted terms, e.g. Summer 2026. Empty means accept any term.
    seasons: list[str] = Field(default_factory=list)
    target_years: list[int] = Field(default_factory=list)
    min_duration_weeks: int | None = None
    max_duration_weeks: int | None = None
    graduation_year: int | None = None
    min_gpa: float | None = None
    work_authorization: str | None = None
    requires_sponsorship: bool = False
    #: When True, a job that explicitly refuses sponsorship is filtered out
    #: entirely. Jobs with UNKNOWN sponsorship are never filtered.
    hard_filter_sponsorship: bool = False
    accept_remote: bool = True
    accept_hybrid: bool = True
    accept_onsite: bool = True
    willing_to_relocate: bool = True
    min_compensation_hourly: float | None = None
    #: Max years of experience a posting may demand before it is penalised.
    max_experience_years: float = 2.0


class ScoringWeights(BaseModel):
    """Component weights, expressed as percentages of the 0-100 score."""

    role_match: float = 25.0
    technical_skills: float = 25.0
    candidate_fit: float = 15.0
    location: float = 10.0
    company_preference: float = 5.0
    freshness: float = 10.0
    internship_constraints: float = 10.0

    @property
    def total(self) -> float:
        return (
            self.role_match
            + self.technical_skills
            + self.candidate_fit
            + self.location
            + self.company_preference
            + self.freshness
            + self.internship_constraints
        )

    def normalized(self) -> dict[str, float]:
        """Weights scaled to sum to 1.0, so custom weights never break scoring."""
        total = self.total or 1.0
        return {
            "role_match": self.role_match / total,
            "technical_skills": self.technical_skills / total,
            "candidate_fit": self.candidate_fit / total,
            "location": self.location / total,
            "company_preference": self.company_preference / total,
            "freshness": self.freshness / total,
            "internship_constraints": self.internship_constraints / total,
        }


class FreshnessWeights(BaseModel):
    """Bonus by posting age, in descending day thresholds."""

    within_24h: float = 10.0
    within_3d: float = 7.0
    within_7d: float = 3.0
    within_14d: float = 1.0
    older: float = 0.0
    unknown: float = 2.0

    def bonus_for_days(self, days: float | None) -> float:
        if days is None:
            return self.unknown
        if days <= 1:
            return self.within_24h
        if days <= 3:
            return self.within_3d
        if days <= 7:
            return self.within_7d
        if days <= 14:
            return self.within_14d
        return self.older


class PriorityThresholds(BaseModel):
    apply_now: float = 90.0
    strong_match: float = 80.0
    worth_considering: float = 70.0
    maybe: float = 60.0

    @model_validator(mode="after")
    def _check_order(self) -> PriorityThresholds:
        vals = [self.apply_now, self.strong_match, self.worth_considering, self.maybe]
        if vals != sorted(vals, reverse=True):
            raise ValueError("priority thresholds must be strictly descending")
        return self


class NotificationRules(BaseModel):
    enabled: bool = True
    provider: str = "telegram"
    min_score: float = 80.0
    max_jobs_per_notification: int = 7
    #: Send a short "nothing today" note, or stay silent.
    send_when_empty: bool = False
    notify_on_updates: bool = True
    #: Materially-changed jobs are re-notified at most this often.
    update_cooldown_hours: int = 72
    notify_deadline_within_days: int = 3
    include_dismissed: bool = False


class ScheduleRules(BaseModel):
    enabled: bool = True
    timezone: str = "America/New_York"
    morning_enabled: bool = True
    morning_time: str = "08:00"
    afternoon_enabled: bool = True
    afternoon_time: str = "16:00"
    cadence: Literal["all", "weekdays"] = "all"

    @field_validator("morning_time", "afternoon_time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("time must be HH:MM")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("time out of range")
        return f"{h:02d}:{m:02d}"


class SearchScope(BaseModel):
    """Controls breadth of the crawl. Defaults favour discovery."""

    #: Max ATS boards crawled per run. Boards rotate by least-recently-crawled
    #: so the whole registry is covered over successive runs.
    max_ats_boards_per_run: int = 400
    max_queries_per_source: int = 12
    #: Enable automatic semantic expansion of role terms into related queries.
    query_expansion: bool = True
    max_expanded_queries: int = 60
    #: Consider jobs older than this many days as stale and stop re-scoring them.
    max_job_age_days: int = 120
    #: Minimum score for a job to be persisted at all, keeping the DB useful.
    min_score_to_store: float = 25.0
    disabled_sources: list[str] = Field(default_factory=list)
    llm_semantic_matching: bool = False
    llm_dedup_adjudication: bool = False


class SearchPreferences(BaseModel):
    """The complete, versioned search-preference document."""

    model_config = ConfigDict(validate_assignment=True)

    roles: list[TargetRole] = Field(default_factory=list)
    keywords: KeywordPrefs = Field(default_factory=KeywordPrefs)
    locations: LocationPrefs = Field(default_factory=LocationPrefs)
    companies: CompanyPrefs = Field(default_factory=CompanyPrefs)
    constraints: InternshipConstraints = Field(default_factory=InternshipConstraints)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    freshness: FreshnessWeights = Field(default_factory=FreshnessWeights)
    thresholds: PriorityThresholds = Field(default_factory=PriorityThresholds)
    notifications: NotificationRules = Field(default_factory=NotificationRules)
    schedule: ScheduleRules = Field(default_factory=ScheduleRules)
    scope: SearchScope = Field(default_factory=SearchScope)

    def enabled_roles(self) -> list[TargetRole]:
        return sorted(
            [r for r in self.roles if r.enabled], key=lambda r: (r.order, -r.weight, r.name)
        )


def default_preferences() -> SearchPreferences:
    """Starting configuration.

    Seeded from the profile this system was built for (hardware/FPGA focus),
    but every value is editable in the settings UI and none of it is referenced
    anywhere else in the codebase.
    """
    roles = [
        "FPGA Engineer Intern",
        "Hardware Engineer Intern",
        "RTL Design Intern",
        "Design Verification Intern",
        "ASIC Design Intern",
        "Digital Design Intern",
        "Computer Architecture Intern",
        "Embedded Systems Intern",
        "Hardware Security Intern",
        "ML Hardware Intern",
        "Computer Engineering Intern",
        "Low-Latency Hardware Intern",
        "Software Engineer Intern",
    ]
    return SearchPreferences(
        roles=[TargetRole(name=n, order=i) for i, n in enumerate(roles)],
        keywords=KeywordPrefs(
            positive=[
                "FPGA", "RTL", "SystemVerilog", "Verilog", "UVM", "ASIC",
                "verification", "digital design", "hardware security",
                "computer architecture", "PCIe", "networking", "low latency",
                "HLS", "C++", "Python", "embedded", "SoC",
            ],
            negative=[
                "senior", "staff", "principal", "manager", "director",
                "5+ years", "10+ years",
            ],
        ),
        locations=LocationPrefs(
            rules=[
                LocationRule(
                    pattern="San Jose",
                    bonus=10.0,
                    aliases=["Bay Area", "Santa Clara", "Sunnyvale", "Palo Alto",
                             "Mountain View", "San Francisco", "Cupertino", "Fremont"],
                ),
                LocationRule(pattern="Boston", bonus=8.0, aliases=["Cambridge, MA", "Massachusetts"]),
                LocationRule(pattern="New York", bonus=8.0, aliases=["NYC", "Manhattan"]),
                LocationRule(pattern="Austin", bonus=8.0, aliases=["Texas"]),
                LocationRule(pattern="Seattle", bonus=8.0, aliases=["Bellevue", "Redmond"]),
                LocationRule(pattern="Pittsburgh", bonus=5.0),
            ]
        ),
        companies=CompanyPrefs(
            preferred=["NVIDIA", "AMD", "Google", "Apple", "Microsoft"],
            preferred_types=[
                "Semiconductor", "AI", "Quant trading", "Robotics",
                "Automotive", "Defense", "Cloud", "Big Tech", "Startups", "Research",
            ],
        ),
    )
