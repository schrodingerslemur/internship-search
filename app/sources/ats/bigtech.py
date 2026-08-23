"""Apple and Google: two employers with one careers platform each.

Neither is on an ATS this project already crawls, and neither had a registered
board, so every posting from them was arriving second-hand through curated
GitHub lists -- thirteen from Apple, five from Google. They are grouped here
because they share a shape rather than a vendor: a single tenant, no discovery
to do, and a hand-written adapter that exists because nothing generic fits.

What each can give differs sharply, and it is worth being plain about it.

**Apple** serves a real search API with summaries inline. It is as good as the
Phenom or Greenhouse paths.

**Google** serves nothing machine-readable. Its careers site renders entirely
in the browser, the detail pages carry no JobPosting structured data, and the
job list is assembled by an internal framework rather than fetched from an
endpoint. What it does publish is a sitemap of every open req, and those URLs
carry the job id and a title slug. That is one request for the whole employer
and it yields a title and a link -- no description, no location, no dates. Such
a posting can be matched on its role and nothing else, which is thin but is
strictly more than the nothing available before, and it costs a single HTTP
call to find out.
"""

from __future__ import annotations

import re
from typing import Any

from app.logging_setup import get_logger
from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.ats.eightfold import strip_html
from app.sources.base import BoardJobSource, SourceContext
from app.sources.http import FetchError

log = get_logger("bigtech")

# --------------------------------------------------------------------------
# Apple
# --------------------------------------------------------------------------

APPLE_SEARCH = "https://jobs.apple.com/api/v1/search"
APPLE_PAGE_SIZE = 20
APPLE_MAX_PAGES = 8

#: Apple's search substring-matches, so "intern" also returns every posting
#: containing "international" and "internal" -- about 1,900 of them against 116
#: for "internship". The narrower terms are deliberate; the title gate would
#: catch the noise but only after paying to download it.
APPLE_TERMS: tuple[str, ...] = ("internship", "intern -", "co-op")


class AppleJobsSource(BoardJobSource):
    name = "apple_jobs"
    display_name = "Apple Jobs"
    kind = SourceKind.ATS
    provider = "apple"
    notes = "Public search API used by jobs.apple.com. Summaries arrive inline."

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        company = board.get("company_name") or "Apple"
        collected: dict[str, dict] = {}
        for term in APPLE_TERMS:
            await self._paginate(term, ctx, collected)

        out: list[RawJob] = []
        for pid, row in collected.items():
            title = row.get("postingTitle") or row.get("transformedPostingTitle") or ""
            if not ctx.keep_title(title):
                continue

            places = [_apple_place(loc) for loc in (row.get("locations") or [])]
            places = [p for p in places if p]
            slug = row.get("transformedPostingTitle") or ""
            team = (row.get("team") or {}).get("teamName")

            out.append(
                self.make_job(
                    source_job_id=f"apple:{pid}",
                    title=title,
                    company=company,
                    url=f"https://jobs.apple.com/en-us/details/{pid}/{slug}".rstrip("/"),
                    location=places[0] if places else None,
                    locations=places[1:],
                    description=strip_html(row.get("jobSummary")),
                    remote_status="remote" if row.get("homeOffice") else None,
                    date_posted=parse_date(row.get("postDateInGMT")),
                    requisition_id=str(row.get("reqId") or pid),
                    department=team,
                    raw={"position_id": pid, "team": team},
                )
            )
        return out

    async def _paginate(
        self, term: str, ctx: SourceContext, collected: dict[str, dict]
    ) -> None:
        for page in range(1, APPLE_MAX_PAGES + 1):
            payload = await ctx.http.post_json(
                APPLE_SEARCH,
                {
                    "query": term,
                    # Both filter keys are required. Sending `{}` is accepted
                    # and silently returns zero results, which is a much more
                    # expensive way to learn the same thing.
                    "filters": {"postingpostLocation": [], "teams": []},
                    "page": page,
                    "locale": "en-us",
                    "sort": "newest",
                    "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
                },
            )
            rows = ((payload or {}).get("res") or {}).get("searchResults") or []
            if not rows:
                return
            added = 0
            for row in rows:
                pid = str(row.get("positionId") or row.get("id") or "")
                if pid and pid not in collected:
                    collected[pid] = row
                    added += 1
            if added == 0 or len(rows) < APPLE_PAGE_SIZE:
                return


def _apple_place(loc: dict) -> str | None:
    parts = [loc.get("city"), loc.get("stateProvince"), loc.get("countryName")]
    return ", ".join(str(p) for p in parts if p) or None


# --------------------------------------------------------------------------
# Google
# --------------------------------------------------------------------------

GOOGLE_SITEMAP = "https://careers.google.com/jobs/sitemap"
#: ``/jobs/results/{id}-{title-slug}/``
_GOOGLE_JOB_RE = re.compile(r"/jobs/results/(\d+)-([a-z0-9-]+)/?\s*$", re.I)
_LOC_RE = re.compile(r"<loc>(.*?)</loc>", re.S)


class GoogleCareersSource(BoardJobSource):
    name = "google_careers"
    display_name = "Google Careers"
    kind = SourceKind.ATS
    provider = "google_careers"
    notes = (
        "Published sitemap only: Google's careers site renders in the browser "
        "and exposes no API or structured data, so these carry a title and a "
        "link but no description."
    )

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        company = board.get("company_name") or "Google"
        try:
            xml = await ctx.http.get_text(GOOGLE_SITEMAP)
        except Exception as exc:
            raise FetchError(f"google sitemap unavailable: {exc}") from exc

        out: list[RawJob] = []
        seen: set[str] = set()
        for url in _LOC_RE.findall(xml or ""):
            match = _GOOGLE_JOB_RE.search(url.strip())
            if not match:
                continue
            job_id, slug = match.group(1), match.group(2)
            if job_id in seen:
                continue
            title = slug.replace("-", " ").strip().title()
            if not ctx.keep_title(title):
                continue
            seen.add(job_id)
            out.append(
                self.make_job(
                    source_job_id=f"google:{job_id}",
                    title=title,
                    company=company,
                    url=url.strip(),
                    # Everything else is genuinely unknown. Inventing a
                    # location or a date to fill the row would be worse than
                    # leaving the scorer to mark them unmeasured.
                    raw={"job_id": job_id, "slug": slug},
                )
            )
        log.info("google.sitemap_read", matched=len(out))
        return out


#: Both are single-tenant, so their "boards" exist only to give the crawler
#: something to schedule and the coverage page something to report health
#: against.
CURATED_BOARDS: dict[str, dict[str, Any]] = {
    "apple": {"provider": "apple", "company_name": "Apple"},
    "google": {"provider": "google_careers", "company_name": "Google"},
}
