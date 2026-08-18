"""Dynamic search-query generation.

Queries are derived from the user's configured roles and skills -- never
hardcoded. Each role is expanded into related phrasings, combined with
preferred locations, and de-duplicated so the crawl stays broad without
issuing the same search twice.

Expansion is kept deliberately tight: over-expanding floods the pipeline with
irrelevant postings, which costs both bandwidth and precision.
"""

from __future__ import annotations

from app.pipeline.textutil import normalize_text
from app.schemas.preferences import SearchPreferences
from app.sources.base import SearchQuery

#: Domain-term expansion. Keys are matched as substrings of a normalised role
#: or keyword; values are additional query phrasings to try.
EXPANSION_MAP: dict[str, tuple[str, ...]] = {
    "fpga": ("FPGA engineer", "FPGA design", "FPGA verification", "programmable logic", "RTL"),
    "rtl": ("RTL design", "RTL engineer", "digital design", "logic design"),
    "asic": ("ASIC design", "ASIC engineer", "silicon design", "SoC design"),
    "verification": (
        "design verification", "RTL verification", "ASIC verification",
        "SoC verification", "UVM", "pre-silicon verification",
    ),
    "hardware verification": (
        "design verification", "RTL verification", "UVM verification", "hardware validation",
    ),
    "digital design": ("digital design engineer", "logic design", "RTL design"),
    "computer architecture": ("computer architecture", "microarchitecture", "CPU architecture", "GPU architecture"),
    "embedded": ("embedded software", "embedded systems", "firmware", "device driver"),
    "hardware security": ("hardware security", "silicon security", "secure hardware", "side-channel"),
    "ml hardware": ("ML accelerator", "AI hardware", "deep learning hardware", "AI accelerator"),
    "low-latency": ("low latency", "high frequency trading hardware", "FPGA trading"),
    "low latency": ("low latency", "FPGA trading", "hardware acceleration"),
    "signal processing": ("DSP", "signal processing", "digital signal processing"),
    "software engineer": ("software engineering", "software developer", "SWE"),
    "systemverilog": ("SystemVerilog", "Verilog", "RTL"),
    "uvm": ("UVM", "verification methodology", "testbench"),
    "physical design": ("physical design", "place and route", "static timing analysis"),
    "analog": ("analog design", "mixed signal", "circuit design"),
    "robotics": ("robotics engineer", "controls engineer", "autonomy"),
    "machine learning": ("machine learning", "deep learning", "ML engineer"),
}

#: Suffixes appended to make a query internship-specific.
INTERN_SUFFIXES: tuple[str, ...] = ("intern", "internship")


def _dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = normalize_text(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def expand_role(role_name: str, *, enabled: bool = True) -> list[str]:
    """Expand one role into related search phrasings."""
    base = role_name.strip()
    if not base:
        return []
    variants = [base]

    # A role already containing "intern" also searches without it, so postings
    # titled "FPGA Engineer, Summer Student" are still reachable.
    lowered = normalize_text(base)
    stem = lowered
    for suffix in ("intern", "internship"):
        stem = stem.replace(suffix, "").strip()
    stem = " ".join(stem.split())

    if enabled and stem:
        for needle, expansions in EXPANSION_MAP.items():
            if needle in lowered:
                variants.extend(expansions)
                break
        else:
            variants.append(stem)

    return _dedupe_preserve(variants)


def generate_queries(prefs: SearchPreferences) -> list[SearchQuery]:
    """Build the run's query set from user preferences.

    Locations are attached to a bounded subset of the highest-priority role
    terms: pairing every term with every city multiplies request volume without
    proportionate gain, since most sources already return national results.
    """
    scope = prefs.scope
    roles = prefs.enabled_roles()
    if not roles:
        return [SearchQuery(text="engineering intern")]

    terms: list[str] = []
    for role in roles:
        terms.extend(expand_role(role.name, enabled=scope.query_expansion))
        terms.extend(role.extra_queries)

    # Skills named as positive keywords are worth searching directly -- that is
    # how an unknown company's "SystemVerilog Intern" gets found.
    if scope.query_expansion:
        for keyword in prefs.keywords.positive[:12]:
            terms.append(f"{keyword} intern")

    terms = _dedupe_preserve(terms)

    queries: list[SearchQuery] = []
    for term in terms:
        lowered = normalize_text(term)
        if prefs.constraints.internship_only and not any(
            suffix in lowered for suffix in INTERN_SUFFIXES
        ):
            term = f"{term} intern"
        queries.append(SearchQuery(text=term))
        if len(queries) >= scope.max_expanded_queries:
            break

    # Location-targeted variants for the top few terms.
    location_patterns = [
        rule.pattern for rule in prefs.locations.rules if not rule.excluded and rule.bonus > 0
    ][:4]
    head = queries[:3]
    for query in head:
        for location in location_patterns:
            if len(queries) >= scope.max_expanded_queries:
                break
            queries.append(SearchQuery(text=query.text, location=location))

    # Final de-duplication on the composite key.
    seen: set[str] = set()
    unique: list[SearchQuery] = []
    for query in queries:
        key = query.key()
        if key not in seen:
            seen.add(key)
            unique.append(query)
    return unique[: scope.max_expanded_queries]
