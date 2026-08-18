"""Relevance scoring and ranking.

The score is a weighted blend of seven components, each normalised to 0-100 and
each reporting the reasons behind it, so the dashboard can always explain
*why* a job ranked where it did. All weights and thresholds come from the
user's preferences -- nothing here is hardcoded policy.

Two rules shape the design:

* **Unknown is not disqualifying.** A missing sponsorship statement, salary, or
  posting date never filters a job out; it simply cannot contribute evidence.
* **Only explicit user configuration excludes.** Blacklisted companies, excluded
  locations, and hard-exclude keywords are the only hard filters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.models.base import (
    EmploymentType,
    Priority,
    RemoteStatus,
    SponsorshipStatus,
    utcnow,
)
from app.pipeline.textutil import normalize_text, slugify_company, token_set_ratio
from app.schemas.job import NormalizedJob
from app.schemas.preferences import SearchPreferences
from app.schemas.profile import CandidateProfileData


@dataclass
class ComponentScore:
    """One scoring component: a 0-100 value plus its rationale."""

    name: str
    value: float
    weight: float
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)

    @property
    def weighted(self) -> float:
        return self.value * self.weight


@dataclass
class MatchResult:
    score: float
    priority: Priority
    components: dict[str, ComponentScore]
    match_reasons: list[str]
    concerns: list[str]
    missing_requirements: list[str]
    matched_skills: list[str]
    missing_skills: list[str]
    excluded: bool = False
    exclusion_reason: str | None = None

    def breakdown(self) -> dict[str, dict[str, object]]:
        return {
            name: {
                "value": round(component.value, 1),
                "weight": round(component.weight, 3),
                "weighted": round(component.weighted, 2),
                "reasons": component.reasons,
                "concerns": component.concerns,
            }
            for name, component in self.components.items()
        }


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------


def score_role_match(job: NormalizedJob, prefs: SearchPreferences) -> ComponentScore:
    """How closely the title matches any configured target role."""
    component = ComponentScore("role_match", 0.0, 0.0)
    roles = prefs.enabled_roles()
    if not roles:
        component.value = 50.0
        component.reasons.append("No target roles configured")
        return component

    best_ratio = 0.0
    best_role = None
    for role in roles:
        ratio = token_set_ratio(job.title, role.name)
        # Role weight nudges ties without letting a low-weight role win outright.
        adjusted = ratio * (0.85 + 0.15 * min(role.weight, 2.0))
        if adjusted > best_ratio:
            best_ratio, best_role = adjusted, role

    value = _clamp(best_ratio * 100)

    # A title that is clearly an internship in a relevant domain still scores
    # even when no configured role phrase lines up exactly.
    if value < 40:
        title_low = normalize_text(job.title)
        domain_hits = [s for s in job.skills if s.lower() in title_low]
        if domain_hits:
            value = max(value, 45.0)
            component.reasons.append(f"Title mentions {', '.join(domain_hits[:3])}")

    if best_role and best_ratio > 0.3:
        component.reasons.append(f"Matches target role: {best_role.name}")
    elif value < 30:
        component.concerns.append("Title does not match your target roles")

    component.value = value
    return component


def score_skills(
    job: NormalizedJob, prefs: SearchPreferences, profile: CandidateProfileData
) -> ComponentScore:
    """Overlap between the posting's skills and the candidate's skills."""
    component = ComponentScore("technical_skills", 0.0, 0.0)
    candidate_skills = {normalize_text(s) for s in profile.all_skills() if s}
    job_skills = {normalize_text(s) for s in job.skills if s}

    if not candidate_skills:
        component.value = 50.0
        component.reasons.append("No skills in profile to compare")
        return component
    if not job_skills:
        # No skills detected in the posting is missing evidence, not a negative.
        component.value = 45.0
        component.concerns.append("No recognisable skills found in the posting")
        return component

    matched = sorted(candidate_skills & job_skills)
    coverage = len(matched) / max(1, min(len(job_skills), 12))
    value = _clamp(coverage * 100)

    # Positive keywords appearing anywhere in the posting add signal.
    blob = normalize_text(job.text_blob())
    keyword_hits = [k for k in prefs.keywords.positive if normalize_text(k) in blob]
    if keyword_hits:
        bonus = min(
            prefs.keywords.positive_cap,
            len(keyword_hits) * prefs.keywords.positive_points_each,
        )
        value = _clamp(value + bonus)
        component.reasons.append(f"Keywords: {', '.join(keyword_hits[:6])}")

    if matched:
        component.reasons.append(f"Your skills: {', '.join(matched[:6])}")
    else:
        component.concerns.append("No direct skill overlap detected")

    component.value = value
    return component


def score_candidate_fit(
    job: NormalizedJob, prefs: SearchPreferences, profile: CandidateProfileData
) -> ComponentScore:
    """Eligibility: experience, degree, graduation timing, GPA."""
    component = ComponentScore("candidate_fit", 70.0, 0.0)
    constraints = prefs.constraints
    value = 70.0

    years = job.experience_required_years
    if years is not None:
        if years <= constraints.max_experience_years:
            value += 15
            component.reasons.append(f"Experience requirement is {years:g} years")
        else:
            penalty = min(50.0, (years - constraints.max_experience_years) * 12)
            value -= penalty
            component.concerns.append(f"Requires {years:g}+ years of experience")
    else:
        value += 5  # No stated requirement is mildly favourable for an intern.

    degrees = {d.lower() for d in job.degree_requirements}
    if degrees:
        if "phd" in degrees and profile.degree and "phd" not in profile.degree.lower():
            if not ({"bachelors", "masters"} & degrees):
                value -= 30
                component.concerns.append("Requires a PhD")
        elif "masters" in degrees and not ({"bachelors"} & degrees):
            if profile.degree and "bachelor" in profile.degree.lower():
                value -= 12
                component.concerns.append("Prefers a Masters student")
        if "bachelors" in degrees:
            component.reasons.append("Open to Bachelors students")

    if constraints.min_gpa and profile.gpa and profile.gpa < constraints.min_gpa:
        component.concerns.append("GPA below your configured minimum")

    component.value = _clamp(value)
    return component


def score_location(job: NormalizedJob, prefs: SearchPreferences) -> ComponentScore:
    """Location desirability, driven entirely by configured rules."""
    component = ComponentScore("location", 0.0, 0.0)
    location_text = " ".join(filter(None, [job.location_raw, *job.locations]))

    if job.remote_status is RemoteStatus.REMOTE and prefs.constraints.accept_remote:
        bonus = prefs.locations.remote_bonus
        component.reasons.append("Remote")
    elif job.remote_status is RemoteStatus.HYBRID and prefs.constraints.accept_hybrid:
        bonus, reason = prefs.locations.bonus_for(location_text)
        bonus = max(bonus, prefs.locations.hybrid_bonus)
        component.reasons.append(f"Hybrid — {reason}")
    else:
        bonus, reason = prefs.locations.bonus_for(location_text)
        if bonus <= -900:
            component.value = 0.0
            component.concerns.append(reason)
            return component
        component.reasons.append(reason)

    # Map a bonus in roughly 0..10 onto a 0..100 component.
    max_bonus = max(
        [r.bonus for r in prefs.locations.rules if not r.excluded]
        + [prefs.locations.remote_bonus, 10.0]
    )
    component.value = _clamp(50 + (bonus / max(max_bonus, 1.0)) * 50)
    return component


def score_company(job: NormalizedJob, prefs: SearchPreferences) -> ComponentScore:
    """Company preference boost. Never a filter for non-blacklisted firms."""
    component = ComponentScore("company_preference", 50.0, 0.0)
    slug = job.company_slug
    preferred = {slugify_company(c) for c in prefs.companies.preferred}
    types = [normalize_text(t) for t in prefs.companies.preferred_types]

    if slug in preferred:
        component.value = 100.0
        component.reasons.append(f"{job.company} is on your preferred list")
        return component

    blob = normalize_text(f"{job.company} {job.description or ''}"[:2000])
    hits = [t for t in types if t and t in blob]
    if hits:
        component.value = 75.0
        component.reasons.append(f"Preferred industry: {hits[0]}")
    return component


def score_freshness(
    job: NormalizedJob, prefs: SearchPreferences, *, now: datetime | None = None
) -> ComponentScore:
    """Newer postings rank higher. Unknown dates are neutral, not penalised."""
    component = ComponentScore("freshness", 50.0, 0.0)
    now = now or utcnow()
    if not job.date_posted:
        component.value = 50.0
        component.reasons.append("Posting date unknown")
        return component

    days = max(0.0, (now - job.date_posted).total_seconds() / 86400.0)
    bonus = prefs.freshness.bonus_for_days(days)
    max_bonus = max(prefs.freshness.within_24h, 1.0)
    component.value = _clamp((bonus / max_bonus) * 100)
    if days <= 1:
        component.reasons.append("Posted today")
    elif days <= 3:
        component.reasons.append(f"Posted {int(days)} days ago")
    elif days <= 14:
        component.reasons.append(f"Posted {int(days)} days ago")
    else:
        component.concerns.append(f"Posted {int(days)} days ago")
    return component


def score_constraints(
    job: NormalizedJob, prefs: SearchPreferences, profile: CandidateProfileData
) -> ComponentScore:
    """Internship-specific constraints: type, term, sponsorship, compensation."""
    component = ComponentScore("internship_constraints", 50.0, 0.0)
    constraints = prefs.constraints
    value = 50.0

    if job.employment_type in (EmploymentType.INTERNSHIP, EmploymentType.CO_OP):
        value += 35
        component.reasons.append("Internship")
    elif job.employment_type is EmploymentType.UNKNOWN:
        value += 5
        component.concerns.append("Internship eligibility unclear")
    elif constraints.internship_only:
        value -= 35
        component.concerns.append(f"Listed as {job.employment_type.value.replace('_', ' ')}")

    if constraints.seasons and job.terms:
        wanted = {normalize_text(s) for s in constraints.seasons}
        have = {normalize_text(t) for t in job.terms}
        if wanted & have:
            value += 10
            component.reasons.append(f"Term matches: {sorted(wanted & have)[0]}")
        elif have:
            value -= 10
            component.concerns.append(f"Term is {job.terms[0]}")

    # Sponsorship: only an explicit refusal counts against the job, and only
    # when the candidate actually needs sponsorship.
    needs_sponsorship = constraints.requires_sponsorship or bool(profile.requires_sponsorship)
    if needs_sponsorship:
        if job.sponsorship is SponsorshipStatus.NOT_OFFERED:
            value -= 30
            component.concerns.append("Employer states it does not sponsor visas")
        elif job.sponsorship is SponsorshipStatus.CITIZENSHIP_REQUIRED:
            value -= 40
            component.concerns.append("US citizenship required")
        elif job.sponsorship is SponsorshipStatus.SECURITY_CLEARANCE_REQUIRED:
            value -= 25
            component.concerns.append("Security clearance required")
        elif job.sponsorship is SponsorshipStatus.OFFERED:
            value += 10
            component.reasons.append("Sponsorship available")
        else:
            component.concerns.append("Sponsorship information unavailable")

    if constraints.min_compensation_hourly and job.salary_min:
        hourly = job.salary_min
        if job.salary_period == "yearly":
            hourly = job.salary_min / 2080.0
        elif job.salary_period == "monthly":
            hourly = job.salary_min / 173.0
        if hourly >= constraints.min_compensation_hourly:
            value += 8
            component.reasons.append("Compensation meets your minimum")
        else:
            value -= 10
            component.concerns.append("Compensation below your minimum")

    component.value = _clamp(value)
    return component


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------


def hard_exclusions(
    job: NormalizedJob, prefs: SearchPreferences
) -> tuple[bool, str | None]:
    """Only explicit user configuration excludes a job."""
    blacklist = {slugify_company(c) for c in prefs.companies.blacklisted if c.strip()}
    if job.company_slug and job.company_slug in blacklist:
        return True, f"{job.company} is blacklisted"

    location_text = normalize_text(" ".join(filter(None, [job.location_raw, *job.locations])))
    for rule in prefs.locations.rules:
        if rule.excluded and rule.matches(location_text):
            return True, f"Excluded location: {rule.pattern}"

    blob = normalize_text(f"{job.title} {job.description or ''}"[:4000])
    for term in prefs.keywords.hard_exclude:
        if term.strip() and normalize_text(term) in blob:
            return True, f"Contains excluded keyword: {term}"

    if (
        prefs.constraints.hard_filter_sponsorship
        and prefs.constraints.requires_sponsorship
        and job.sponsorship
        in (SponsorshipStatus.NOT_OFFERED, SponsorshipStatus.CITIZENSHIP_REQUIRED)
    ):
        return True, "Employer does not sponsor and you require sponsorship"

    return False, None


def classify_priority(score: float, prefs: SearchPreferences) -> Priority:
    thresholds = prefs.thresholds
    if score >= thresholds.apply_now:
        return Priority.APPLY_NOW
    if score >= thresholds.strong_match:
        return Priority.STRONG_MATCH
    if score >= thresholds.worth_considering:
        return Priority.WORTH_CONSIDERING
    if score >= thresholds.maybe:
        return Priority.MAYBE
    return Priority.SKIP


def score_job(
    job: NormalizedJob,
    prefs: SearchPreferences,
    profile: CandidateProfileData,
    *,
    now: datetime | None = None,
) -> MatchResult:
    """Compute the full transparent score for one job."""
    excluded, reason = hard_exclusions(job, prefs)
    if excluded:
        return MatchResult(
            score=0.0,
            priority=Priority.SKIP,
            components={},
            match_reasons=[],
            concerns=[reason or "Excluded"],
            missing_requirements=[],
            matched_skills=[],
            missing_skills=[],
            excluded=True,
            exclusion_reason=reason,
        )

    weights = prefs.weights.normalized()
    components = {
        "role_match": score_role_match(job, prefs),
        "technical_skills": score_skills(job, prefs, profile),
        "candidate_fit": score_candidate_fit(job, prefs, profile),
        "location": score_location(job, prefs),
        "company_preference": score_company(job, prefs),
        "freshness": score_freshness(job, prefs, now=now),
        "internship_constraints": score_constraints(job, prefs, profile),
    }
    for name, component in components.items():
        component.weight = weights.get(name, 0.0)

    total = sum(component.weighted for component in components.values())

    # Negative keywords deduct after weighting, so a single bad signal cannot
    # be hidden by strong components elsewhere.
    blob = normalize_text(f"{job.title} {job.description or ''}"[:4000])
    negative_hits = [k for k in prefs.keywords.negative if k.strip() and normalize_text(k) in blob]
    if negative_hits:
        # Seniority words in the *title* matter far more than in body text.
        title_low = normalize_text(job.title)
        title_hits = [k for k in negative_hits if normalize_text(k) in title_low]
        penalty = abs(prefs.keywords.negative_points_each) * (
            len(title_hits) * 2 + (len(negative_hits) - len(title_hits)) * 0.5
        )
        total -= min(penalty, 40.0)

    score = _clamp(total)

    match_reasons: list[str] = []
    concerns: list[str] = []
    for component in components.values():
        match_reasons.extend(component.reasons)
        concerns.extend(component.concerns)
    if negative_hits:
        concerns.append(f"Negative keywords: {', '.join(negative_hits[:4])}")

    candidate_skills = {normalize_text(s) for s in profile.all_skills() if s}
    job_skills = [s for s in job.skills]
    matched_skills = [s for s in job_skills if normalize_text(s) in candidate_skills]
    missing_skills = [s for s in job_skills if normalize_text(s) not in candidate_skills][:10]

    missing_requirements: list[str] = []
    if job.experience_required_years and job.experience_required_years > prefs.constraints.max_experience_years:
        missing_requirements.append(f"{job.experience_required_years:g}+ years experience")
    for degree in job.degree_requirements:
        if degree == "PhD" and (not profile.degree or "phd" not in profile.degree.lower()):
            missing_requirements.append("PhD")

    return MatchResult(
        score=round(score, 1),
        priority=classify_priority(score, prefs),
        components=components,
        match_reasons=match_reasons,
        concerns=concerns,
        missing_requirements=missing_requirements,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
    )
