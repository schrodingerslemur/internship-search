"""Fact extraction from free-text postings.

Guiding rule, applied throughout: **absence of evidence is never evidence.**
If a posting does not mention sponsorship, the result is ``UNKNOWN`` -- never
``OFFERED`` and never ``NOT_OFFERED``. The same applies to deadlines, salary,
and experience requirements.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from dateutil import parser as dateparser

from app.models.base import EmploymentType, RemoteStatus, SponsorshipStatus, utcnow
from app.pipeline.textutil import normalize_text

# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------

US_STATES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
STATE_ABBREVS: frozenset[str] = frozenset(US_STATES.values())

#: Countries as postings actually spell them, mapped to a two-letter code.
#: Normalisation for the places that turn up in listings, not a geography
#: database -- anything unrecognised is kept verbatim rather than discarded.
COUNTRY_NAMES: dict[str, str] = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "america": "US",
    "united states": "US", "united states of america": "US",
    "ca": "CA", "canada": "CA",
    "uk": "GB", "gb": "GB", "united kingdom": "GB", "england": "GB",
    "great britain": "GB", "scotland": "GB", "wales": "GB",
    "ie": "IE", "ireland": "IE",
    "de": "DE", "germany": "DE", "deutschland": "DE",
    "nl": "NL", "netherlands": "NL", "the netherlands": "NL",
    "fr": "FR", "france": "FR",
    "es": "ES", "spain": "ES",
    "it": "IT", "italy": "IT",
    "se": "SE", "sweden": "SE",
    "ch": "CH", "switzerland": "CH",
    "pl": "PL", "poland": "PL",
    "in": "IN", "india": "IN",
    "sg": "SG", "singapore": "SG",
    "au": "AU", "australia": "AU",
    "nz": "NZ", "new zealand": "NZ",
    "jp": "JP", "japan": "JP",
    "kr": "KR", "south korea": "KR", "korea": "KR",
    "cn": "CN", "china": "CN",
    "tw": "TW", "taiwan": "TW",
    "hk": "HK", "hong kong": "HK",
    "il": "IL", "israel": "IL",
    "mx": "MX", "mexico": "MX",
    "br": "BR", "brazil": "BR",
    "ae": "AE", "uae": "AE", "united arab emirates": "AE",
}


def country_code(value: str | None) -> str:
    """Best-effort two-letter code for a country as a posting spelled it."""
    if not value:
        return ""
    text = " ".join(str(value).strip().lower().split())
    for candidate in (text, text.replace(".", "")):
        if candidate in COUNTRY_NAMES:
            return COUNTRY_NAMES[candidate]
    return str(value).strip().upper()

#: Metro grouping. Two postings in the same metro may merge; different metros
#: are treated as distinct positions (requirement: Austin != Santa Clara).
METRO_ALIASES: dict[str, tuple[str, ...]] = {
    "bay_area": (
        "san jose", "santa clara", "sunnyvale", "palo alto", "mountain view",
        "san francisco", "cupertino", "fremont", "milpitas", "menlo park",
        "redwood city", "bay area", "silicon valley", "south san francisco",
        "san mateo", "foster city", "berkeley", "oakland",
    ),
    "seattle": ("seattle", "bellevue", "redmond", "kirkland", "everett"),
    "nyc": ("new york", "nyc", "manhattan", "brooklyn", "jersey city"),
    "boston": ("boston", "cambridge, ma", "somerville", "waltham", "burlington, ma", "andover"),
    "austin": ("austin", "round rock"),
    "dallas": ("dallas", "plano", "richardson", "fort worth", "irving"),
    "la": ("los angeles", "santa monica", "irvine", "san diego", "pasadena", "el segundo"),
    "pittsburgh": ("pittsburgh",),
    "chicago": ("chicago", "evanston", "naperville"),
    "portland": ("portland", "hillsboro", "beaverton"),
    "phoenix": ("phoenix", "chandler", "tempe", "scottsdale", "mesa"),
    "raleigh": ("raleigh", "durham", "cary", "chapel hill"),
    "denver": ("denver", "boulder", "louisville, co"),
    "atlanta": ("atlanta", "alpharetta"),
    "dc": ("washington, dc", "arlington, va", "mclean", "reston", "bethesda", "herndon"),
}

_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|virtual|telecommute)\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_ONSITE_RE = re.compile(r"\b(on[\s-]?site|in[\s-]?office|in[\s-]?person)\b", re.I)


class ParsedLocation:
    __slots__ = ("raw", "city", "state", "country", "metro", "is_remote", "key")

    def __init__(
        self,
        raw: str | None,
        city: str | None,
        state: str | None,
        country: str | None,
        metro: str | None,
        is_remote: bool,
    ) -> None:
        self.raw = raw
        self.city = city
        self.state = state
        self.country = country
        self.metro = metro
        self.is_remote = is_remote
        self.key = metro or (
            f"{(city or '').lower()}|{(state or '').lower()}"
            if (city or state)
            else ("remote" if is_remote else "")
        )


def parse_location(raw: str | None) -> ParsedLocation:
    """Best-effort structured location. Unknown fields stay ``None``."""
    if not raw or not raw.strip():
        return ParsedLocation(raw, None, None, None, None, False)
    text = raw.strip()
    low = normalize_text(text)
    is_remote = bool(_REMOTE_RE.search(low))

    metro = None
    for name, aliases in METRO_ALIASES.items():
        if any(a in low for a in aliases):
            metro = name
            break

    city = state = country = None
    # "City, ST" / "City, State" / "City, ST, Country"
    parts = [p.strip() for p in re.split(r"[,|/]", text) if p.strip()]
    if parts:
        candidate_city = parts[0]
        if not _REMOTE_RE.fullmatch(candidate_city.strip()):
            city = candidate_city
    for part in parts[1:]:
        p_low = normalize_text(part)
        upper = part.strip().upper()
        if upper in STATE_ABBREVS:
            # US states win ties with country codes -- DE is Delaware before
            # Germany, CA California before Canada, IN Indiana before India.
            # The corpus is overwhelmingly US, and the tie-break errs toward
            # reading a location as domestic, which for a country filter means
            # keeping a job rather than hiding one.
            state = upper
        elif p_low in US_STATES:
            state = US_STATES[p_low]
        elif p_low in COUNTRY_NAMES:
            # Recognised country name or code, at any length. The old rule only
            # accepted names longer than three characters, so "London, UK" and
            # "Berlin, DE" parsed with no country at all -- which meant a
            # country filter could not see them.
            country = COUNTRY_NAMES[p_low]
        elif len(p_low) > 3 and not state:
            country = part.strip()
    if state and not country:
        country = "US"
    if city and normalize_text(city) in US_STATES:
        state, city = US_STATES[normalize_text(city)], None
    return ParsedLocation(raw, city, state, country, metro, is_remote)


def infer_remote_status(*texts: str | None) -> RemoteStatus:
    hay = " ".join(t for t in texts if t)
    if not hay.strip():
        return RemoteStatus.UNKNOWN
    if _HYBRID_RE.search(hay):
        return RemoteStatus.HYBRID
    if _REMOTE_RE.search(hay):
        return RemoteStatus.REMOTE
    if _ONSITE_RE.search(hay):
        return RemoteStatus.ONSITE
    return RemoteStatus.UNKNOWN


def locations_are_compatible(a: ParsedLocation, b: ParsedLocation) -> bool:
    """Whether two locations could describe the same posting.

    Unknown locations are compatible with anything (absence of evidence).
    Two *known* different metros are incompatible -- this is what keeps
    "FPGA Intern - Austin" and "FPGA Intern - Santa Clara" as separate jobs.
    """
    if not a.key or not b.key:
        return True
    if a.key == b.key:
        return True
    if a.metro and b.metro:
        return a.metro == b.metro
    if a.is_remote and b.is_remote:
        return True
    if a.city and b.city and normalize_text(a.city) == normalize_text(b.city):
        return True
    # One side has only a state; allow if the other sits in that state.
    if a.state and b.state and a.state != b.state:
        return False
    if (a.city and not b.city) or (b.city and not a.city):
        return a.state == b.state if (a.state and b.state) else True
    return False


# --------------------------------------------------------------------------
# Employment type
# --------------------------------------------------------------------------

_INTERN_RE = re.compile(r"\b(intern|internship|summer analyst|industrial placement)\b", re.I)
_COOP_RE = re.compile(r"\b(co[\s-]?op)\b", re.I)
_NEWGRAD_RE = re.compile(r"\b(new grad|university grad|early career|campus hire|graduate program)\b", re.I)
_PARTTIME_RE = re.compile(r"\bpart[\s-]?time\b", re.I)
_CONTRACT_RE = re.compile(r"\b(contract|contractor|temporary|temp)\b", re.I)


def infer_employment_type(
    title: str | None, description: str | None = None, declared: str | None = None
) -> EmploymentType:
    """Infer employment type, trusting the title most."""
    if declared:
        d = normalize_text(declared)
        mapping = {
            "intern": EmploymentType.INTERNSHIP,
            "internship": EmploymentType.INTERNSHIP,
            "co-op": EmploymentType.CO_OP,
            "coop": EmploymentType.CO_OP,
            "full-time": EmploymentType.FULL_TIME,
            "fulltime": EmploymentType.FULL_TIME,
            "part-time": EmploymentType.PART_TIME,
            "contract": EmploymentType.CONTRACT,
            "temporary": EmploymentType.CONTRACT,
        }
        for needle, value in mapping.items():
            if needle in d:
                # A declared "full time" on an internship title still means intern.
                if value is EmploymentType.FULL_TIME and title and _INTERN_RE.search(title):
                    return EmploymentType.INTERNSHIP
                return value

    title = title or ""
    if _COOP_RE.search(title):
        return EmploymentType.CO_OP
    if _INTERN_RE.search(title):
        return EmploymentType.INTERNSHIP
    if _NEWGRAD_RE.search(title):
        return EmploymentType.NEW_GRAD

    body = description or ""
    if _COOP_RE.search(body) and _INTERN_RE.search(body):
        return EmploymentType.INTERNSHIP
    if _INTERN_RE.search(body):
        return EmploymentType.INTERNSHIP
    if _NEWGRAD_RE.search(body):
        return EmploymentType.NEW_GRAD
    if _PARTTIME_RE.search(title):
        return EmploymentType.PART_TIME
    if _CONTRACT_RE.search(title):
        return EmploymentType.CONTRACT
    return EmploymentType.UNKNOWN


# --------------------------------------------------------------------------
# Sponsorship  (never inferred as available)
# --------------------------------------------------------------------------

_NO_SPONSOR_PATTERNS = (
    r"(?:will |can )?not (?:be able to )?(?:provide|offer|sponsor)\w*[^.]{0,40}(?:sponsorship|visa)",
    r"no (?:visa )?sponsorship",
    r"unable to (?:provide|offer|sponsor)[^.]{0,40}(?:sponsorship|visa)",
    r"does not sponsor",
    r"sponsorship is not (?:available|offered|provided)",
    r"without (?:the need for )?(?:current or future )?(?:visa )?sponsorship",
    r"not (?:currently )?considering candidates (?:who )?requir\w+ sponsorship",
    r"must be (?:legally )?authorized to work[^.]{0,60}without sponsorship",
)
_YES_SPONSOR_PATTERNS = (
    r"(?:we |company )?(?:will |do |can )?(?:provide|offer|sponsor)\w*[^.]{0,30}(?:visa )?sponsorship",
    r"sponsorship (?:is )?available",
    r"visa sponsorship (?:is )?(?:offered|provided|available)",
    # "we will sponsor visas", "sponsors H-1B / OPT / CPT candidates"
    r"\b(?:we |company )?(?:will |do |can |may )?sponsors?\b[^.]{0,30}\b(?:visas?|h-?1b|opt|cpt|candidates?|students?)\b",
    r"open to sponsoring",
    r"eligible for (?:visa )?sponsorship",
)
_CITIZEN_PATTERNS = (
    r"must be a? ?u\.?s\.? citizen",
    r"u\.?s\.? citizenship (?:is )?required",
    r"citizenship requirement",
    r"itar",
    r"export control",
)
_CLEARANCE_PATTERNS = (
    r"security clearance",
    r"secret clearance",
    r"top secret",
    r"ts/sci",
    r"active clearance",
)


def _first_match(text: str, patterns: tuple[str, ...]) -> str | None:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            snippet = text[max(0, m.start() - 60) : m.end() + 60]
            return " ".join(snippet.split())
    return None


def extract_sponsorship(*texts: str | None) -> tuple[SponsorshipStatus, str | None]:
    """Detect sponsorship posture, with the evidence snippet.

    Returns ``UNKNOWN`` when nothing is stated. Order matters: an explicit
    refusal outranks a generic affirmative phrase.
    """
    hay = " ".join(t for t in texts if t)
    if not hay.strip():
        return SponsorshipStatus.UNKNOWN, None

    ev = _first_match(hay, _CLEARANCE_PATTERNS)
    if ev:
        return SponsorshipStatus.SECURITY_CLEARANCE_REQUIRED, ev
    ev = _first_match(hay, _CITIZEN_PATTERNS)
    if ev:
        return SponsorshipStatus.CITIZENSHIP_REQUIRED, ev
    ev = _first_match(hay, _NO_SPONSOR_PATTERNS)
    if ev:
        return SponsorshipStatus.NOT_OFFERED, ev
    ev = _first_match(hay, _YES_SPONSOR_PATTERNS)
    if ev:
        return SponsorshipStatus.OFFERED, ev
    return SponsorshipStatus.UNKNOWN, None


# --------------------------------------------------------------------------
# Experience, degrees, salary, deadline
# --------------------------------------------------------------------------

_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(\d{1,2})?\s*\+?\s*year[s]?\b[^.]{0,40}"
    r"(?:experience|exp\b)",
    re.I,
)
_DEGREE_RE = re.compile(
    r"\b(bachelor'?s?|b\.?s\.?|b\.?a\.?|master'?s?|m\.?s\.?|ph\.?d\.?|doctoral|mba|associate'?s?)\b",
    re.I,
)


def extract_experience_years(*texts: str | None) -> float | None:
    """Minimum years of experience demanded, or ``None`` if unstated."""
    hay = " ".join(t for t in texts if t)
    if not hay:
        return None
    best: float | None = None
    for m in _YEARS_RE.finditer(hay):
        try:
            low = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if low > 30:
            continue
        best = low if best is None else min(best, low)
    return best


def extract_degrees(*texts: str | None) -> list[str]:
    hay = " ".join(t for t in texts if t)
    if not hay:
        return []
    found: list[str] = []
    canon = {
        "bachelor": "Bachelors", "bachelors": "Bachelors", "bs": "Bachelors",
        "ba": "Bachelors", "master": "Masters", "masters": "Masters",
        "ms": "Masters", "phd": "PhD", "doctoral": "PhD", "mba": "MBA",
        "associate": "Associates", "associates": "Associates",
    }
    for m in _DEGREE_RE.finditer(hay):
        key = re.sub(r"[^a-z]", "", m.group(1).lower())
        value = canon.get(key)
        if value and value not in found:
            found.append(value)
    return found


_SALARY_RE = re.compile(
    r"(?:(?P<cur>[$€£])\s?)?(?P<a>\d{1,3}(?:,\d{3})+|\d{2,7}(?:\.\d+)?)\s*"
    r"(?:(?:-|–|to)\s*(?:[$€£]\s?)?(?P<b>\d{1,3}(?:,\d{3})+|\d{2,7}(?:\.\d+)?))?"
    r"\s*(?P<per>per\s+hour|/\s*hour|/\s*hr|hourly|an hour|per\s+year|/\s*year|annually|per\s+month|/\s*month)?",
    re.I,
)


def extract_salary(text: str | None) -> dict[str, float | str | None]:
    """Parse a salary range. Returns empty values when nothing is stated."""
    empty: dict[str, float | str | None] = {
        "min": None, "max": None, "currency": None, "period": None, "raw": None
    }
    if not text:
        return empty
    for m in _SALARY_RE.finditer(text):
        a_raw, b_raw = m.group("a"), m.group("b")
        try:
            a = float(a_raw.replace(",", ""))
        except (AttributeError, ValueError):
            continue
        b = float(b_raw.replace(",", "")) if b_raw else None
        per = (m.group("per") or "").lower()
        if "hour" in per or "hr" in per:
            period = "hourly"
        elif "month" in per:
            period = "monthly"
        elif "year" in per or "annual" in per:
            period = "yearly"
        else:
            period = "hourly" if a < 500 else "yearly"
        # Reject obvious non-salary numbers (years, counts).
        if period == "yearly" and a < 10000:
            continue
        if period == "hourly" and (a < 7 or a > 400):
            continue
        cur = m.group("cur")
        currency = {"$": "USD", "€": "EUR", "£": "GBP"}.get(cur or "", "USD" if cur else None)
        return {
            "min": a,
            "max": b if b and b >= a else None,
            "currency": currency,
            "period": period,
            "raw": " ".join(m.group(0).split()),
        }
    return empty


_DEADLINE_RE = re.compile(
    r"(?:appl(?:y|ication|ications)|submit|deadline|closes?|closing)"
    r"[^.\n]{0,60}?(?:by|before|on|date[:\s]|is)\s*"
    r"(?P<date>(?:[A-Z][a-z]{2,9}\s+\d{1,2},?\s*\d{4})|(?:\d{1,2}/\d{1,2}/\d{2,4})|"
    r"(?:\d{4}-\d{2}-\d{2})|(?:[A-Z][a-z]{2,9}\s+\d{1,2}(?:st|nd|rd|th)?))",
    re.I,
)


def extract_deadline(text: str | None, reference: datetime | None = None) -> tuple[datetime | None, bool]:
    """Extract an explicitly stated application deadline.

    Returns ``(deadline, is_explicit)``. A deadline is never invented: when no
    closing date is stated the result is ``(None, False)``.
    """
    if not text:
        return None, False
    ref = reference or utcnow()
    m = _DEADLINE_RE.search(text)
    if not m:
        return None, False
    raw = m.group("date")
    try:
        parsed = dateparser.parse(raw, default=ref.replace(hour=23, minute=59, second=0), fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None, False
    if parsed is None:
        return None, False
    parsed = parsed.replace(tzinfo=None)
    # A parsed date far in the past usually means a missing year; roll forward once.
    if parsed < ref - timedelta(days=180):
        try:
            parsed = parsed.replace(year=parsed.year + 1)
        except ValueError:  # pragma: no cover - Feb 29
            return None, False
    if parsed > ref + timedelta(days=730) or parsed < ref - timedelta(days=30):
        return None, False
    return parsed, True


_TERM_RE = re.compile(r"\b(summer|fall|autumn|spring|winter)\s*(20\d{2})?\b", re.I)


def extract_terms(*texts: str | None) -> list[str]:
    """Internship terms mentioned, e.g. ``["Summer 2026"]``."""
    hay = " ".join(t for t in texts if t)
    out: list[str] = []
    for m in _TERM_RE.finditer(hay or ""):
        season = m.group(1).title()
        if season == "Autumn":
            season = "Fall"
        year = m.group(2)
        label = f"{season} {year}" if year else season
        if label not in out:
            out.append(label)
    return out[:6]


def parse_date(value: object) -> datetime | None:
    """Parse a date from a string, epoch number, or datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        if ts <= 0:
            return None
        try:
            return datetime.utcfromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_date(int(text))
        try:
            return dateparser.parse(text).replace(tzinfo=None)
        except (ValueError, OverflowError, TypeError):
            return None
    return None
