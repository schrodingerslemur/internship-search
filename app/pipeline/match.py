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

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.models.base import (
    EmploymentType,
    Priority,
    RemoteStatus,
    SponsorshipStatus,
    utcnow,
)
from app.pipeline.textutil import normalize_text, role_affinity, slugify_company
from app.schemas.job import NormalizedJob
from app.schemas.preferences import SearchPreferences
from app.schemas.profile import CandidateProfileData


@dataclass
class ComponentScore:
    """One scoring component: a 0-100 value plus its rationale.

    ``value`` may be ``None``, meaning *this could not be measured* -- the
    posting carried no evidence either way. That is different from zero, and
    keeping the two apart is what stops a missing description from voting.
    A component with no value is dropped from the blend and its weight is
    shared out among the components that did measure something.
    """

    name: str
    value: float | None
    weight: float
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    #: Which configured role this component matched, when it matched one.
    matched_role: str | None = None

    @property
    def measured(self) -> bool:
        return self.value is not None

    @property
    def weighted(self) -> float:
        return (self.value or 0.0) * self.weight


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

    #: What each component actually contributed to the final score, after
    #: weight renormalisation and the context multiplier. Populated by
    #: ``score_job`` so the UI can explain the number it is showing instead of
    #: listing raw component values that do not add up to it.
    contributions: dict[str, float] = field(default_factory=dict)
    #: The configured role this job's title matched, if any.
    matched_role: str | None = None

    def breakdown(self) -> dict[str, dict[str, object]]:
        return {
            name: {
                # None means "could not be measured", and survives to the UI as
                # such. A component that did not measure anything must not be
                # rendered as a zero bar -- that reads as a bad score.
                "value": None if component.value is None else round(component.value, 1),
                "measured": component.measured,
                "weight": round(component.weight, 3),
                "weighted": round(component.weighted, 2),
                "contribution": round(self.contributions.get(name, 0.0), 2),
                "reasons": component.reasons,
                "concerns": component.concerns,
                "matched_role": component.matched_role,
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

    # Ties are broken by the *unweighted* affinity first, so the role named in
    # the explanation is the one the title actually resembles. Ranking by the
    # weighted figure alone let a role win a tie it had not earned and then be
    # reported as the reason, which is how "Hardware Engineering Intern" came
    # to be explained as a match for "ML Hardware Intern".
    best_affinity = 0.0
    best_adjusted = 0.0
    best_role = None
    for role in roles:
        affinity = role_affinity(job.title, role.name)
        if affinity <= 0:
            continue
        # Role weight nudges ties without letting a low-weight role win outright.
        adjusted = affinity * (0.85 + 0.15 * min(role.weight, 2.0))
        if (adjusted, affinity) > (best_adjusted, best_affinity):
            best_affinity, best_adjusted, best_role = affinity, adjusted, role

    value = _clamp(best_adjusted * 100)

    # A title that is clearly an internship in a relevant domain still scores
    # even when no configured role phrase lines up exactly. Restricted to
    # titles that named a skill *and* were not rejected outright, so a job in
    # another discipline cannot collect the floor.
    if 0 < value < 40 or (value == 0 and best_role is not None):
        title_low = normalize_text(job.title)
        domain_hits = [s for s in job.skills if s.lower() in title_low]
        if domain_hits:
            value = max(value, 45.0)
            component.reasons.append(f"Title mentions {', '.join(domain_hits[:3])}")

    if best_role is not None and best_affinity > 0.3:
        component.reasons.append(f"Matches target role: {best_role.name}")
        component.matched_role = best_role.name
    elif value < 30:
        component.concerns.append("Title does not match your target roles")

    component.value = value
    return component


#: Skills so widely required that holding one says almost nothing about fit.
#: They still count -- a posting wanting Python and you knowing Python is real
#: evidence -- but a specialist skill is worth roughly three of them.
UBIQUITOUS_SKILLS: frozenset[str] = frozenset(
    {
        "python", "java", "javascript", "typescript", "git", "linux", "bash",
        "sql", "docker", "aws", "gcp", "azure", "c", "c++", "react", "node.js",
        "ci/cd", "jenkins", "kubernetes", "numpy", "pandas",
    }
)

#: Evidence needed for a full skills score, in weighted skill-hits. Three
#: specialist matches -- or the equivalent in common ones -- is a strong
#: signal; beyond that the component saturates rather than continuing to
#: reward postings for listing more technologies.
SKILL_EVIDENCE_TARGET = 3.0


#: Body text below this length is a stub -- a title and a link, not a posting.
#: The real corpus is starkly bimodal on this: of 5,134 listings, 2,625 carry
#: exactly zero body characters and 2,474 carry over a thousand, with nine in
#: between. So the threshold only has to answer "was a body fetched at all",
#: and is deliberately low: a genuinely terse but real description is evidence,
#: and should be read rather than discarded.
MIN_READABLE_BODY_CHARS = 40


def _has_readable_body(job: NormalizedJob) -> bool:
    """Whether this posting carries enough prose to draw conclusions from."""
    body = " ".join(
        part
        for part in (
            job.description,
            job.requirements,
            job.responsibilities,
            job.preferred_qualifications,
        )
        if part
    )
    return len(body.strip()) >= MIN_READABLE_BODY_CHARS


def _skill_weight(skill: str) -> float:
    return 0.35 if normalize_text(skill) in UBIQUITOUS_SKILLS else 1.0


def score_skills(
    job: NormalizedJob, prefs: SearchPreferences, profile: CandidateProfileData
) -> ComponentScore:
    """Overlap between the posting's skills and the candidate's skills.

    Scored on the *absolute weight of evidence found*, not on the fraction of
    the posting's skill list that was covered. Dividing by the posting's own
    list punished thorough postings: one naming eighteen technologies needed
    twelve matches to score well, while one mentioning "Python" once needed a
    single match and scored 100. That inverted the ranking -- a biotech firm's
    lone Python reference outscored a trading firm's FPGA/RTL/SystemVerilog
    posting for a hardware candidate.
    """
    component = ComponentScore("technical_skills", None, 0.0)
    candidate_skills = _candidate_skill_set(profile)
    job_skills = [s for s in job.skills if s]

    if not candidate_skills:
        component.concerns.append("No skills in your profile to compare against")
        return component

    # Skills are extracted from the title as well as the body, so a posting
    # with no description can still carry a token or two from its own name.
    # That is not enough to conclude anything: "no overlap" with a title-only
    # skill list is missing evidence, not a poor match, and scoring it zero
    # put a hard zero on a quarter of the score for exactly the postings the
    # crawler failed to fetch. Both cases leave the value unset, which drops
    # the component from the blend rather than letting it vote.
    if not _has_readable_body(job):
        component.concerns.append("Posting has no description, so skills could not be compared")
        return component
    if not job_skills:
        component.concerns.append("No recognisable skills in the posting")
        return component

    matched = sorted({normalize_text(s) for s in job_skills} & candidate_skills)
    evidence = sum(_skill_weight(s) for s in matched)
    value = _clamp(100.0 * min(1.0, evidence / SKILL_EVIDENCE_TARGET))

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


def _candidate_skill_set(profile: CandidateProfileData) -> set[str]:
    """The candidate's skills, read through the same vocabulary as a posting's.

    A profile is free text, so a raw string intersection missed real matches:
    "Python programming" never equalled the token ``python``. Running the
    profile through the same extractor the postings go through puts both sides
    in one vocabulary, and the raw entries are kept too so a skill outside the
    vocabulary is not silently lost.
    """
    from app.pipeline.normalize import extract_skills

    raw = [s for s in profile.all_skills() if s]
    if not raw:
        return set()
    skills = {normalize_text(s) for s in raw}
    skills.update(normalize_text(s) for s in extract_skills(" , ".join(raw)))
    return {s for s in skills if s}


def score_candidate_fit(
    job: NormalizedJob, prefs: SearchPreferences, profile: CandidateProfileData
) -> ComponentScore:
    """Eligibility: experience, degree, graduation timing, GPA.

    Returns *unmeasured* when the posting states no requirement at all, which
    is the common case. Previously this returned a flat 70-75 for essentially
    every job -- p10, p50 and p90 were all 75.0 across the corpus -- so it
    carried 15% of the weight while distinguishing nothing, pulling every job
    toward the middle and compressing the range the thresholds work over.
    """
    component = ComponentScore("candidate_fit", None, 0.0)
    constraints = prefs.constraints
    value = 70.0
    stated = False

    years = job.experience_required_years
    if years is not None:
        stated = True
        if years <= constraints.max_experience_years:
            value += 15
            component.reasons.append(f"Experience requirement is {years:g} years")
        else:
            penalty = min(50.0, (years - constraints.max_experience_years) * 12)
            value -= penalty
            component.concerns.append(f"Requires {years:g}+ years of experience")

    degrees = {d.lower() for d in job.degree_requirements}
    if degrees:
        stated = True
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
        stated = True
        component.concerns.append("GPA below your configured minimum")

    if not stated:
        component.concerns.append("Posting states no experience or degree requirement")
        return component

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
    # Word-boundary matching: a two-letter type like "AI" must not match inside
    # "maintain" or "aircraft".
    hits = [t for t in types if t and re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", blob)]
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


#: The two components that decide whether this is the right *kind* of job.
RELEVANCE_COMPONENTS: tuple[str, ...] = ("role_match", "technical_skills")

#: How much of a relevant job's score survives poor context. Everything outside
#: RELEVANCE_COMPONENTS -- location, freshness, constraints, fit, company --
#: describes how good a *relevant* job is, so it scales the result instead of
#: adding to it. Adding it produced a floor of roughly 32 points on every job,
#: including ones with no role or skill match at all, which squeezed the entire
#: corpus into 30-60 and made an absolute score threshold meaningless.
#:
#: Raised from 0.55: at that floor a *perfect* relevance in average context
#: scored 77.5, below the default strong-match threshold of 80, so nothing in a
#: 5,000-job corpus could reach the top two bands. Context should modulate a
#: relevant job, not halve it.
CONTEXT_FLOOR = 0.70


def _blend(components: dict[str, ComponentScore], weights: dict[str, float]) -> float:
    """Combine components so context modulates relevance rather than replacing it.

    A perfect role and skill match in mediocre context still scores well. A job
    that matches neither cannot be rescued by being nearby, recent and an
    internship -- which is exactly what the previous additive blend allowed.

    Only components that actually measured something take part. An unmeasured
    component's weight is shared out among the rest of its group rather than
    voting with an invented midpoint: half of all postings arrive with no
    description, and treating that silence as a mediocre skill match let the
    crawler's luck outweigh the candidate's fit.
    """
    measured = {name: c for name, c in components.items() if c.measured}

    relevance_weight = sum(
        weights.get(name, 0.0) for name in RELEVANCE_COMPONENTS if name in measured
    )
    context_weight = sum(
        weights.get(name, 0.0)
        for name in measured
        if name not in RELEVANCE_COMPONENTS
    )

    if relevance_weight <= 0:
        # Either the user weighted relevance out entirely, or nothing about
        # relevance could be measured. Respect that literally and fall back to
        # a plain weighted average of whatever else is known.
        if context_weight <= 0:
            return 0.0
        return (
            sum(
                c.weighted
                for name, c in measured.items()
                if name not in RELEVANCE_COMPONENTS
            )
            / context_weight
        )

    relevance = (
        sum(measured[name].weighted for name in RELEVANCE_COMPONENTS if name in measured)
        / relevance_weight
    )
    if context_weight <= 0:
        return relevance

    context = (
        sum(c.weighted for name, c in measured.items() if name not in RELEVANCE_COMPONENTS)
        / context_weight
    )
    return relevance * (CONTEXT_FLOOR + (1.0 - CONTEXT_FLOOR) * (context / 100.0))


def _contributions(
    components: dict[str, ComponentScore], weights: dict[str, float], total: float
) -> dict[str, float]:
    """How many of the final points each component is responsible for.

    The blend is multiplicative, so a component's weighted value is not its
    contribution -- there is no arrangement of the raw numbers that adds up to
    the score. Apportioning the total by each component's share of its own
    group gives a figure that does sum correctly and can be shown to the user.
    """
    measured = {name: c for name, c in components.items() if c.measured}
    if not measured or total <= 0:
        return {}

    rel_weight = sum(weights.get(n, 0.0) for n in RELEVANCE_COMPONENTS if n in measured)
    ctx_weight = sum(
        weights.get(n, 0.0) for n in measured if n not in RELEVANCE_COMPONENTS
    )
    rel_total = sum(measured[n].weighted for n in RELEVANCE_COMPONENTS if n in measured)
    ctx_total = sum(
        c.weighted for n, c in measured.items() if n not in RELEVANCE_COMPONENTS
    )

    # Relevance sets the ceiling; context scales it. Split the final score
    # between the two groups in that proportion, then within each group by
    # each component's share of the group's weighted value.
    if rel_weight <= 0:
        rel_share, ctx_share = 0.0, total
    elif ctx_weight <= 0:
        rel_share, ctx_share = total, 0.0
    else:
        context = ctx_total / ctx_weight
        lift = (1.0 - CONTEXT_FLOOR) * (context / 100.0)
        ctx_fraction = lift / (CONTEXT_FLOOR + lift) if (CONTEXT_FLOOR + lift) else 0.0
        ctx_share = total * ctx_fraction
        rel_share = total - ctx_share

    out: dict[str, float] = {}
    for name, component in measured.items():
        in_relevance = name in RELEVANCE_COMPONENTS
        group_total = rel_total if in_relevance else ctx_total
        group_share = rel_share if in_relevance else ctx_share
        if group_total > 0:
            out[name] = group_share * (component.weighted / group_total)
        else:
            out[name] = 0.0
    return out


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

    total = _blend(components, weights)

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

    candidate_skills = _candidate_skill_set(profile)
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
        contributions=_contributions(components, weights, score),
        matched_role=components["role_match"].matched_role,
    )
