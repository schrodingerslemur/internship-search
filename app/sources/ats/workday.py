"""Workday career sites.

Workday powers the careers pages of a large share of big semiconductor,
hardware, and enterprise employers, so it is the highest-yield ATS for this
search. Each tenant exposes the same public JSON endpoint that its own careers
page calls::

    POST https://{host}/wday/cxs/{tenant}/{site}/jobs
         {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"}

The list response carries title, location, date, and requisition id but *not*
the description. Fetching every description would mean thousands of extra
requests, so descriptions are hydrated only for postings that survive a cheap
title pre-filter -- keeping the crawl fast while still giving the scoring
engine real text for plausible matches.
"""

from __future__ import annotations

import re
from typing import Any

from app.logging_setup import get_logger
from app.models.base import SourceKind, utcnow
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import BoardJobSource, SourceContext
from app.sources.http import FetchError

log = get_logger("workday")

#: Workday rejects limits above 20 with HTTP 400.
PAGE_SIZE = 20
MAX_PAGES = 8
#: Free-text search is relevance-ranked and degrades fast, so read few pages.
SEARCH_FALLBACK_PAGES = 3
#: Descriptions fetched per board per run.
HYDRATE_LIMIT = 20

_REQ_RE = re.compile(r"\b(R-?\d[\w-]*|JR-?\d+)\b", re.I)

#: Facet labels that identify internships. Tenants name these differently, so
#: the label is matched rather than assuming a fixed facet id.
_INTERN_FACET_RE = re.compile(r"intern|co-?op|student|apprentice|university|early career", re.I)
#: Facets worth inspecting. Others (location, department) would over-narrow.
FACET_PARAMS_OF_INTEREST: frozenset[str] = frozenset(
    {"workerSubType", "timeType", "jobFamily", "employmentType", "jobType"}
)


class WorkdaySource(BoardJobSource):
    name = "workday"
    display_name = "Workday career sites"
    kind = SourceKind.ATS
    provider = "workday"
    notes = (
        "Public endpoint used by each tenant's own careers page. Undocumented "
        "but stable; crawled politely with rate limiting."
    )

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        extra = board.get("extra") or {}
        host = extra.get("host")
        site = extra.get("site")
        tenant = board.get("board_token")
        if not (host and site and tenant):
            raise FetchError(f"workday board missing host/site: {board}")

        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        company = board.get("company_name") or tenant

        collected: dict[str, dict] = {}

        # Preferred path: filter by the tenant's own "intern" facet. This is an
        # exact filter, so pagination stays correct all the way through.
        facets = await self._discover_intern_facets(base, ctx)
        if facets:
            for facet_param, value_ids in facets.items():
                await self._paginate(
                    base, ctx, collected, applied_facets={facet_param: value_ids}
                )
        else:
            # Fallback: free-text search. Workday ranks these by relevance and
            # quality degrades quickly, so only the top pages are worth reading
            # and the title gate discards the noise.
            for term in self._search_terms(ctx):
                await self._paginate(
                    base, ctx, collected, search_text=term, max_pages=SEARCH_FALLBACK_PAGES
                )

        jobs: list[RawJob] = []
        hydrate_targets: list[str] = []
        for path, item in collected.items():
            title = item.get("title") or ""
            if self._looks_relevant(title) and len(hydrate_targets) < HYDRATE_LIMIT:
                hydrate_targets.append(path)

        details: dict[str, dict] = {}
        if hydrate_targets:
            results = await ctx.http.gather(
                [ctx.http.get_json(f"{base}{p}") for p in hydrate_targets]
            )
            for path, result in zip(hydrate_targets, results, strict=False):
                if isinstance(result, Exception):
                    continue
                info = (result or {}).get("jobPostingInfo") or {}
                if info:
                    details[path] = info

        for path, item in collected.items():
            title = item.get("title") or ""
            if not title:
                continue
            info = details.get(path, {})
            bullets = item.get("bulletFields") or []
            req_id = None
            for bullet in bullets:
                m = _REQ_RE.search(str(bullet))
                if m:
                    req_id = m.group(1)
                    break
            if not req_id:
                m = _REQ_RE.search(path)
                req_id = m.group(1) if m else None

            posted = info.get("startDate") or self._relative_posted(item.get("postedOn"))
            external = info.get("externalUrl") or f"https://{host}/en-US/{site}{path}"

            jobs.append(
                self.make_job(
                    source_job_id=f"{tenant}:{path}",
                    title=title,
                    company=company,
                    url=external,
                    apply_url=info.get("externalUrl") or external,
                    location=item.get("locationsText") or info.get("location"),
                    locations=info.get("additionalLocations") or [],
                    description=info.get("jobDescription") or "",
                    employment_type=info.get("timeType"),
                    remote_status=info.get("remoteType"),
                    date_posted=parse_date(posted),
                    requisition_id=req_id,
                    raw={"path": path, "tenant": tenant},
                )
            )
        return jobs


    # -- crawling helpers --

    async def _discover_intern_facets(
        self, base: str, ctx: SourceContext
    ) -> dict[str, list[str]]:
        """Find facet values that mean "internship" for this tenant.

        Workday tenants expose their own taxonomies, so rather than assuming a
        fixed facet id, the first unfiltered request is inspected for any facet
        value whose label reads as an internship. Returns ``{}`` when the
        tenant has no such facet, in which case the caller falls back to search.
        """
        if ctx.title_gate is None:
            return {}
        try:
            payload = await ctx.http.post_json(
                f"{base}/jobs", {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": 0}
            )
        except FetchError:
            return {}

        found: dict[str, list[str]] = {}
        for facet in (payload or {}).get("facets") or []:
            param = facet.get("facetParameter")
            if not param or param not in FACET_PARAMS_OF_INTEREST:
                continue
            ids = [
                value.get("id")
                for value in (facet.get("values") or [])
                if value.get("id")
                and _INTERN_FACET_RE.search(str(value.get("descriptor") or ""))
                and int(value.get("count") or 0) > 0
            ]
            if ids:
                found[param] = ids
        return found

    async def _paginate(
        self,
        base: str,
        ctx: SourceContext,
        collected: dict[str, dict],
        *,
        applied_facets: dict[str, list[str]] | None = None,
        search_text: str | None = None,
        max_pages: int = MAX_PAGES,
    ) -> None:
        """Page through results, collecting listings that pass the title gate."""
        offset = 0
        total: int | None = None
        for _ in range(max_pages):
            body: dict[str, Any] = {
                "appliedFacets": applied_facets or {},
                "limit": PAGE_SIZE,
                "offset": offset,
            }
            if search_text:
                body["searchText"] = search_text
            payload = await ctx.http.post_json(f"{base}/jobs", body)
            postings = (payload or {}).get("jobPostings") or []
            if not postings:
                return
            for item in postings:
                path = item.get("externalPath") or ""
                if path and path not in collected and ctx.keep_title(item.get("title")):
                    collected[path] = item
            # `total` is only reported on the first page of a query.
            if total is None:
                total = int((payload or {}).get("total") or 0)
            offset += PAGE_SIZE
            if total and offset >= total:
                return
            if len(postings) < PAGE_SIZE:
                return

    # -- helpers --

    def _search_terms(self, ctx: SourceContext) -> list[str]:
        """Search terms for the tenant search box.

        Workday matches search text almost literally, so a specific phrase like
        "fpga design" returns nothing even at an employer with dozens of FPGA
        internships. Because this endpoint is already scoped to one company,
        the right move is to search broadly and let the title gate and the
        scoring engine do the narrowing.
        """
        if ctx.title_gate is not None:
            # An internship search is configured: ask the tenant for its
            # internships and filter locally.
            return ["intern"]
        terms: list[str] = []
        for query in ctx.queries:
            head = " ".join(query.text.lower().split()[:2])
            if head and head not in terms:
                terms.append(head)
            if len(terms) >= 2:
                break
        return terms or ["intern"]

    def _looks_relevant(self, title: str) -> bool:
        low = title.lower()
        return "intern" in low or "co-op" in low or "coop" in low

    def _relative_posted(self, text: str | None) -> str | None:
        """Convert Workday's "Posted 3 Days Ago" into something parseable."""
        if not text:
            return None
        m = re.search(r"(\d+)\+?\s*(day|week|month)s?\s*ago", text, re.I)
        if not m:
            if re.search(r"posted\s+today", text, re.I):
                return "today"
            if re.search(r"yesterday", text, re.I):
                return "yesterday"
            return None
        amount, unit = int(m.group(1)), m.group(2).lower()
        days = amount * {"day": 1, "week": 7, "month": 30}[unit]
        from datetime import timedelta

        return (utcnow() - timedelta(days=days)).isoformat()
