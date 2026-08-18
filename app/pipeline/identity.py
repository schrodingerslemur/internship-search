"""URL canonicalisation and ATS identity extraction.

This module is the linchpin of two requirements at once:

* **Cross-board deduplication.** If an aggregator's apply-link resolves to
  ``greenhouse.io/acme/jobs/12345`` and the Greenhouse crawler independently
  fetched that same posting, the two share an ``ats_identity`` and are provably
  the same underlying job -- no fuzzy matching required.
* **Company discovery.** Every URL the pipeline sees is mined for an ATS board
  identity. New boards are registered and crawled directly on later runs,
  surfacing jobs from companies the user never named.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

#: Query parameters that never identify a posting.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_name", "utm_cid", "utm_reader", "utm_referrer",
        "gclid", "fbclid", "msclkid", "dclid", "yclid", "igshid", "mc_cid", "mc_eid",
        "ref", "referrer", "referer", "source", "src", "campaign",
        "trk", "trkinfo", "originalsubdomain", "position", "pagenum",
        "refid", "rgtk", "sessionid", "session_id", "sid", "phpsessid", "jsessionid",
        "affiliate", "affiliateid", "aff", "partner", "partnerid",
        "from", "at", "et", "clickid", "click_id", "impressionid",
        "recommended", "eid", "tk", "vjk", "jsa", "advn", "sjdu",
        "hl", "gh_src", "lever_source", "lever-source", "lever_via",
        "ashby_jid_source", "source_id", "iis", "iisn", "spa",
    }
)

#: Parameters that DO identify a posting on specific hosts and must be kept
#: even though they would otherwise look like tracking noise.
MEANINGFUL_PARAMS: dict[str, frozenset[str]] = {
    "greenhouse.io": frozenset({"gh_jid", "token"}),
    "myworkdayjobs.com": frozenset({"jobid"}),
    "icims.com": frozenset({"jobid", "job_id"}),
    "taleo.net": frozenset({"job", "rid", "jobid"}),
    "smartrecruiters.com": frozenset({"oga"}),
    "linkedin.com": frozenset({"currentjobid"}),
    "indeed.com": frozenset({"jk"}),
    "glassdoor.com": frozenset({"jobListingId", "joblistingid"}),
    "ziprecruiter.com": frozenset({"lvk"}),
    "google.com": frozenset({"htidocid"}),
}

#: Hosts whose links merely wrap a real destination URL.
REDIRECT_HOSTS: frozenset[str] = frozenset(
    {"click.appcast.io", "www.google.com", "l.facebook.com", "lnkd.in", "t.co"}
)
REDIRECT_PARAMS: tuple[str, ...] = ("url", "u", "redirect", "target", "dest", "destination", "q")


@dataclass(frozen=True, slots=True)
class AtsIdentity:
    """A posting's identity within an applicant tracking system."""

    provider: str
    board_token: str
    job_id: str | None = None
    #: Provider-specific extras needed to crawl the board (Workday host/site).
    extra: dict[str, str] | None = None

    @property
    def board_key(self) -> str:
        """Identifies the *board* -- used for discovery/registration."""
        return f"{self.provider}:{self.board_token.lower()}"

    @property
    def job_key(self) -> str | None:
        """Identifies the *posting* -- used for deduplication."""
        if not self.job_id:
            return None
        return f"{self.provider}:{self.board_token.lower()}:{self.job_id}"


# --------------------------------------------------------------------------
# URL canonicalisation
# --------------------------------------------------------------------------


def unwrap_redirect(url: str, _depth: int = 0) -> str:
    """Follow wrapper URLs that embed the destination in a query parameter."""
    if _depth > 3:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    if not parts.query:
        return url
    params = dict(parse_qsl(parts.query, keep_blank_values=False))
    lowered = {k.lower(): v for k, v in params.items()}
    for key in REDIRECT_PARAMS:
        candidate = lowered.get(key)
        if not candidate:
            continue
        candidate = unquote(candidate)
        if candidate.startswith(("http://", "https://")):
            # Only unwrap for known wrappers, or when the destination host differs.
            try:
                dest_host = urlsplit(candidate).netloc.lower()
            except ValueError:
                continue
            if host in REDIRECT_HOSTS or dest_host != host:
                return unwrap_redirect(candidate, _depth + 1)
    return url


def canonicalize_url(url: str | None) -> str | None:
    """Strip tracking noise so the same posting yields one stable URL.

    Preserves parameters that genuinely identify a posting (``gh_jid``,
    Workday ``jobId``, and friends) via :data:`MEANINGFUL_PARAMS`.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    url = unwrap_redirect(url)

    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.netloc:
        return None

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    # Drop default ports.
    if host.endswith(":80") or host.endswith(":443"):
        host = host.rsplit(":", 1)[0]

    keep_keys: set[str] = set()
    for domain, keys in MEANINGFUL_PARAMS.items():
        if domain in host:
            keep_keys |= {k.lower() for k in keys}

    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        low = key.lower()
        if low in keep_keys:
            kept.append((key, value))
        elif low in TRACKING_PARAMS:
            continue
        elif low.startswith("utm_"):
            continue
        else:
            kept.append((key, value))
    kept.sort()

    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(kept), ""))


def url_hash(url: str | None) -> str | None:
    canonical = canonicalize_url(url)
    if not canonical:
        return None
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# ATS identity extraction
# --------------------------------------------------------------------------

_GREENHOUSE_BOARD = re.compile(
    r"(?:job-boards|boards|boards-api)\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)",
    re.I,
)
_GREENHOUSE_JOB = re.compile(r"/jobs/(\d+)")
_GH_JID = re.compile(r"[?&]gh_jid=(\d+)", re.I)
_GH_EMBED_FOR = re.compile(r"[?&]for=([A-Za-z0-9_-]+)", re.I)

_LEVER = re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9-]+))?", re.I)
_ASHBY = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9-]+))?", re.I)
_SMARTRECRUITERS = re.compile(
    r"(?:jobs\.smartrecruiters\.com|careers\.smartrecruiters\.com)/([A-Za-z0-9_-]+)"
    r"(?:/(\d+)[^/]*)?",
    re.I,
)
_SMARTRECRUITERS_API = re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)", re.I)
_WORKABLE = re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)(?:/j/([A-Za-z0-9]+))?", re.I)
_WORKDAY = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)"
    r"(?:/job/[^/?#]+/([^/?#]+))?",
    re.I,
)
_WORKDAY_REQ = re.compile(r"_(R-?\d[\w-]*)\b", re.I)
_ICIMS = re.compile(r"https?://(?:careers-)?([a-z0-9-]+)\.icims\.com.*?/jobs/(\d+)", re.I)
_ICIMS_BOARD = re.compile(r"https?://(?:careers-)?([a-z0-9-]+)\.icims\.com", re.I)
_JOBVITE = re.compile(r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)(?:/job/([A-Za-z0-9]+))?", re.I)
_RECRUITEE = re.compile(r"([A-Za-z0-9-]+)\.recruitee\.com/o/([A-Za-z0-9-]+)", re.I)
_TALEO = re.compile(r"([A-Za-z0-9-]+)\.taleo\.net", re.I)
_RIPPLING = re.compile(r"ats\.rippling\.com/(?:[a-z-]+/)?([A-Za-z0-9_-]+)/jobs/([A-Za-z0-9-]+)", re.I)
_BAMBOO = re.compile(r"([A-Za-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)/(\d+)?", re.I)
_PARADOX = re.compile(r"([A-Za-z0-9-]+)\.eightfold\.ai/careers", re.I)


def extract_ats_identity(*urls: str | None) -> AtsIdentity | None:
    """Return the first ATS identity found across the given URLs.

    Accepts several URLs (posting URL, apply URL, company URL) because
    aggregators often keep the real ATS link only in the apply field.
    """
    for raw in urls:
        if not raw:
            continue
        identity = _extract_one(raw)
        if identity is not None:
            return identity
    return None


def _extract_one(url: str) -> AtsIdentity | None:  # noqa: C901 - flat dispatch table
    url = unwrap_redirect(url.strip())

    # --- Greenhouse ---
    m = _GREENHOUSE_BOARD.search(url)
    if m:
        token = m.group(1)
        if token.lower() == "embed":
            emb = _GH_EMBED_FOR.search(url)
            token = emb.group(1) if emb else token
        job = _GREENHOUSE_JOB.search(url)
        jid = job.group(1) if job else None
        if not jid:
            g = _GH_JID.search(url)
            jid = g.group(1) if g else None
        return AtsIdentity("greenhouse", token, jid)
    # Company-hosted Greenhouse embeds: acme.com/careers?gh_jid=123
    g = _GH_JID.search(url)
    if g:
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        token = host.split(".")[0]
        return AtsIdentity("greenhouse", token, g.group(1))

    # --- Lever ---
    m = _LEVER.search(url)
    if m:
        return AtsIdentity("lever", m.group(1), m.group(2))

    # --- Ashby ---
    m = _ASHBY.search(url)
    if m:
        job_id = m.group(2)
        # Ashby posting ids are UUIDs; anything shorter is a board sub-path.
        if job_id and len(job_id) < 20:
            job_id = None
        return AtsIdentity("ashby", m.group(1), job_id)

    # --- SmartRecruiters ---
    m = _SMARTRECRUITERS_API.search(url)
    if m:
        return AtsIdentity("smartrecruiters", m.group(1), None)
    m = _SMARTRECRUITERS.search(url)
    if m:
        return AtsIdentity("smartrecruiters", m.group(1), m.group(2))

    # --- Workable ---
    m = _WORKABLE.search(url)
    if m:
        return AtsIdentity("workable", m.group(1), m.group(2))

    # --- Workday ---
    m = _WORKDAY.search(url)
    if m:
        tenant, wd, site, job_path = m.group(1), m.group(2), m.group(3), m.group(4)
        job_id: str | None = None
        if job_path:
            req = _WORKDAY_REQ.search(job_path)
            job_id = req.group(1) if req else job_path
        return AtsIdentity(
            "workday",
            tenant.lower(),
            job_id,
            extra={"host": f"{tenant.lower()}.{wd.lower()}.myworkdayjobs.com", "site": site},
        )

    # --- iCIMS ---
    m = _ICIMS.search(url)
    if m:
        return AtsIdentity("icims", m.group(1), m.group(2))
    m = _ICIMS_BOARD.search(url)
    if m:
        return AtsIdentity("icims", m.group(1), None)

    # --- Others ---
    m = _JOBVITE.search(url)
    if m:
        return AtsIdentity("jobvite", m.group(1), m.group(2))
    m = _RECRUITEE.search(url)
    if m:
        return AtsIdentity("recruitee", m.group(1), m.group(2))
    m = _RIPPLING.search(url)
    if m:
        return AtsIdentity("rippling", m.group(1), m.group(2))
    m = _BAMBOO.search(url)
    if m:
        return AtsIdentity("bamboohr", m.group(1), m.group(2))
    m = _PARADOX.search(url)
    if m:
        return AtsIdentity("eightfold", m.group(1), None)
    m = _TALEO.search(url)
    if m:
        return AtsIdentity("taleo", m.group(1), None)
    return None


#: Hosts recognised as aggregators/boards rather than an employer's own site.
KNOWN_BOARD_HOSTS: frozenset[str] = frozenset(
    {
        "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
        "monster.com", "dice.com", "simplyhired.com", "careerbuilder.com",
        "adzuna.com", "themuse.com", "remotive.com", "arbeitnow.com",
        "wellfound.com", "angel.co", "builtin.com", "wayup.com", "handshake.com",
        "jooble.org", "google.com", "talent.com", "lensa.com", "jobs2careers.com",
        "simplify.jobs", "untapped.io", "ripplematch.com",
    }
)

ATS_HOSTS: frozenset[str] = frozenset(
    {
        "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
        "workable.com", "myworkdayjobs.com", "icims.com", "jobvite.com",
        "recruitee.com", "taleo.net", "rippling.com", "bamboohr.com",
        "eightfold.ai", "successfactors.com", "brassring.com", "avature.net",
    }
)


def classify_url_host(url: str | None) -> str:
    """Classify a URL as company_careers, ats, job_board, or unknown.

    Used to elect the most authoritative application URL among duplicates.
    """
    if not url:
        return "unknown"
    try:
        host = urlsplit(url if "://" in url else "https://" + url).netloc.lower()
    except ValueError:
        return "unknown"
    host = host.removeprefix("www.")
    if any(host == b or host.endswith("." + b) for b in ATS_HOSTS):
        return "ats"
    if any(host == b or host.endswith("." + b) for b in KNOWN_BOARD_HOSTS):
        return "job_board"
    if not host:
        return "unknown"
    return "company_careers"
