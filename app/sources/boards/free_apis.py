"""Public job APIs that need no credentials.

These broaden the search beyond ATS boards the system already knows about, and
every listing they return is mined for new ATS boards (see
``app.pipeline.discovery``), so they feed company discovery as well as coverage.
"""

from __future__ import annotations

from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import QueryJobSource, SearchQuery, SourceContext

MUSE_API = "https://www.themuse.com/api/public/jobs"
REMOTIVE_API = "https://remotive.com/api/remote-jobs"
ARBEITNOW_API = "https://www.arbeitnow.com/api/job-board-api"


class TheMuseSource(QueryJobSource):
    name = "themuse"
    display_name = "The Muse"
    kind = SourceKind.JOB_BOARD
    max_queries = 4
    notes = "Free public API. Filtered to internship level."

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        out: list[RawJob] = []
        for page in range(1, 4):
            params = {"page": page, "level": "Internship"}
            if query.location:
                params["location"] = query.location
            payload = await ctx.http.get_json(MUSE_API, params=params)
            results = (payload or {}).get("results") or []
            if not results:
                break
            terms = {w for w in query.text.lower().split() if len(w) > 2}
            for item in results:
                title = item.get("name") or ""
                # The Muse has no free-text search, so filter client-side
                # against the generated query terms.
                if terms and not (terms & set(title.lower().split())):
                    continue
                company = (item.get("company") or {}).get("name") or ""
                if not company:
                    continue
                locations = [
                    loc.get("name") for loc in (item.get("locations") or []) if loc.get("name")
                ]
                out.append(
                    self.make_job(
                        source_job_id=str(item.get("id")),
                        title=title,
                        company=company,
                        url=(item.get("refs") or {}).get("landing_page"),
                        location=locations[0] if locations else None,
                        locations=locations,
                        description=item.get("contents") or "",
                        date_posted=parse_date(item.get("publication_date")),
                        department=(item.get("categories") or [{}])[0].get("name")
                        if item.get("categories")
                        else None,
                        raw={"id": item.get("id")},
                    )
                )
            if page >= int((payload or {}).get("page_count") or 1):
                break
        return out


class RemotiveSource(QueryJobSource):
    name = "remotive"
    display_name = "Remotive (remote roles)"
    kind = SourceKind.JOB_BOARD
    max_queries = 5
    notes = "Free public API. Remote-only listings."

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        payload = await ctx.http.get_json(
            REMOTIVE_API, params={"search": query.text, "limit": min(query.max_results, 100)}
        )
        items = (payload or {}).get("jobs") or []
        out: list[RawJob] = []
        for item in items:
            company = item.get("company_name") or ""
            if not company:
                continue
            out.append(
                self.make_job(
                    source_job_id=str(item.get("id")),
                    title=item.get("title") or "",
                    company=company,
                    url=item.get("url"),
                    location=item.get("candidate_required_location") or "Remote",
                    description=item.get("description") or "",
                    employment_type=item.get("job_type"),
                    remote_status="remote",
                    salary_raw=item.get("salary") or None,
                    date_posted=parse_date(item.get("publication_date")),
                    department=item.get("category"),
                    raw={"id": item.get("id")},
                )
            )
        return out


class ArbeitnowSource(QueryJobSource):
    name = "arbeitnow"
    display_name = "Arbeitnow"
    kind = SourceKind.JOB_BOARD
    max_queries = 1
    notes = "Free public feed, Europe-weighted. Filtered client-side."

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        payload = await ctx.http.get_json(ARBEITNOW_API)
        items = (payload or {}).get("data") or []
        terms = {w for w in query.text.lower().split() if len(w) > 2}
        out: list[RawJob] = []
        for item in items:
            title = item.get("title") or ""
            haystack = f"{title} {' '.join(item.get('tags') or [])}".lower()
            if terms and not any(t in haystack for t in terms):
                continue
            company = item.get("company_name") or ""
            if not company:
                continue
            out.append(
                self.make_job(
                    source_job_id=str(item.get("slug")),
                    title=title,
                    company=company,
                    url=item.get("url"),
                    location=item.get("location"),
                    description=item.get("description") or "",
                    remote_status="remote" if item.get("remote") else None,
                    employment_type=(item.get("job_types") or [None])[0],
                    date_posted=parse_date(item.get("created_at")),
                    raw={"slug": item.get("slug")},
                )
            )
        return out
