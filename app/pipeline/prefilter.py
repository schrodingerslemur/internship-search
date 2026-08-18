"""Cheap relevance gate applied *before* full normalisation.

Crawling broadly is the point of this system, but broad crawling returns
everything an employer has open -- a single Greenhouse board can yield 500+
listings that are overwhelmingly sales, legal, and marketing roles. Fully
normalising all of them (HTML stripping, section splitting, shingling for
similarity) costs time and a great deal of memory for postings that could never
match an internship search.

This stage is deliberately **permissive and configuration-driven**: it only
narrows the field when the user has actually asked for internships, it keeps
anything ambiguous, and it never consults the company list. Discovery is
unaffected -- board harvesting runs on the *unfiltered* listings, so unknown
companies still enter the registry even when their current openings are not
relevant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.job import RawJob
from app.schemas.preferences import SearchPreferences

_INTERN_TITLE = re.compile(
    r"\b(intern|internship|co[\s-]?op|coop|summer analyst|placement student|"
    r"industrial placement|apprentice|student|trainee|university grad|new grad|"
    r"early career|campus)\b",
    re.I,
)
_INTERN_BODY = re.compile(r"\b(internship|intern\b|co[\s-]?op)\b", re.I)

#: Titles that are internships but obviously outside a technical search. Only
#: used to skip full normalisation, never to hard-exclude from the database.
_OBVIOUS_NON_TECH = re.compile(
    r"\b(sales|account executive|recruit(er|ing)|talent acquisition|"
    r"marketing|social media|public relations|human resources|payroll|"
    r"customer (success|support|service)|barista|driver|warehouse|nurse|"
    r"teacher|paralegal|attorney|legal counsel|janitor|security guard|"
    r"retail associate|cashier)\b",
    re.I,
)


@dataclass
class PrefilterStats:
    total: int = 0
    kept: int = 0
    dropped_not_internship: int = 0
    dropped_off_domain: int = 0

    @property
    def dropped(self) -> int:
        return self.dropped_not_internship + self.dropped_off_domain


def _has_domain_signal(text: str, terms: set[str]) -> bool:
    """Whether any configured role or keyword term appears in the text."""
    if not terms:
        return True
    low = text.lower()
    return any(term in low for term in terms)


def build_domain_terms(prefs: SearchPreferences) -> set[str]:
    """Search terms drawn from the user's roles and positive keywords.

    These come entirely from configuration, so changing the profile changes
    what survives the gate.
    """
    terms: set[str] = set()
    for role in prefs.enabled_roles():
        for word in re.split(r"[^a-zA-Z0-9+#]+", role.name.lower()):
            if len(word) > 2 and word not in ("intern", "internship", "engineer", "the", "and"):
                terms.add(word)
    for keyword in prefs.keywords.positive:
        cleaned = keyword.strip().lower()
        if len(cleaned) > 1:
            terms.add(cleaned)
    return terms


def prefilter(
    raws: list[RawJob], prefs: SearchPreferences
) -> tuple[list[RawJob], PrefilterStats]:
    """Drop listings that cannot plausibly match, before heavy processing."""
    stats = PrefilterStats(total=len(raws))
    if not prefs.constraints.internship_only:
        stats.kept = len(raws)
        return raws, stats

    terms = build_domain_terms(prefs)
    kept: list[RawJob] = []

    for raw in raws:
        title = raw.title or ""
        declared = (raw.employment_type or "").lower()

        is_internship = bool(
            _INTERN_TITLE.search(title)
            or "intern" in declared
            or raw.terms
            or (raw.description and len(raw.description) < 6000 and _INTERN_BODY.search(raw.description))
        )
        if not is_internship:
            stats.dropped_not_internship += 1
            continue

        # Among internships, keep anything that shows a domain signal in the
        # title, or whose title is generic enough that the description might
        # still qualify it. Only clearly off-domain roles are dropped.
        if _OBVIOUS_NON_TECH.search(title) and not _has_domain_signal(title, terms):
            stats.dropped_off_domain += 1
            continue

        kept.append(raw)

    stats.kept = len(kept)
    return kept, stats


def build_title_gate(prefs: SearchPreferences):
    """Return a cheap title predicate for source-level filtering.

    Applied while listings are still being read from the wire, so irrelevant
    postings never have their full descriptions materialised. Intentionally
    looser than :func:`prefilter`: it only rejects titles that are clearly not
    internships, leaving the real decision to the full pipeline.
    """
    if not prefs.constraints.internship_only:
        return None
    terms = build_domain_terms(prefs)

    def gate(title: str) -> bool:
        if not title:
            return True
        if not _INTERN_TITLE.search(title):
            return False
        if _OBVIOUS_NON_TECH.search(title) and not _has_domain_signal(title, terms):
            return False
        return True

    return gate
