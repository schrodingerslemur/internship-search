"""Job APIs that require credentials.

Each degrades to ``unconfigured`` when its key is absent -- reported honestly in
the coverage dashboard rather than silently skipped.

These are the *legitimate* route to the large boards. Indeed, LinkedIn, and
Google Jobs content reaches the pipeline through licensed resellers (Adzuna,
JSearch, SerpApi) instead of scraping sites whose terms forbid it.
"""

from __future__ import annotations

from typing import Any

from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import QueryJobSource, SearchQuery, SourceContext

ADZUNA_API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
JSEARCH_API = "https://jsearch.p.rapidapi.com/search"
USAJOBS_API = "https://data.usajobs.gov/api/search"
JOOBLE_API = "https://jooble.org/api/{key}"
SERPAPI = "https://serpapi.com/search"


class AdzunaSource(QueryJobSource):
    name = "adzuna"
    display_name = "Adzuna"
    kind = SourceKind.AGGREGATOR
    required_credentials = ("app_id", "app_key")
    max_queries = 10
    notes = "Licensed aggregator with a free tier. Broadest legal board coverage."

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        params: dict[str, Any] = {
            "app_id": ctx.credentials.get("app_id"),
            "app_key": ctx.credentials.get("app_key"),
            "results_per_page": min(query.max_results, 50),
            "what": query.text,
            "content-type": "application/json",
            "max_days_old": 30,
        }
        if query.location:
            params["where"] = query.location
        payload = await ctx.http.get_json(
            ADZUNA_API.format(country="us", page=1), params=params
        )
        items = (payload or {}).get("results") or []
        out: list[RawJob] = []
        for item in items:
            company = (item.get("company") or {}).get("display_name") or ""
            if not company:
                continue
            out.append(
                self.make_job(
                    source_job_id=str(item.get("id")),
                    title=item.get("title") or "",
                    company=company,
                    url=item.get("redirect_url"),
                    apply_url=item.get("redirect_url"),
                    location=(item.get("location") or {}).get("display_name"),
                    description=item.get("description") or "",
                    salary_min=item.get("salary_min"),
                    salary_max=item.get("salary_max"),
                    salary_currency="USD",
                    employment_type=item.get("contract_time"),
                    date_posted=parse_date(item.get("created")),
                    department=(item.get("category") or {}).get("label"),
                    raw={"id": item.get("id")},
                )
            )
        return out


class JSearchSource(QueryJobSource):
    name = "jsearch"
    display_name = "JSearch (Google Jobs / LinkedIn / Indeed)"
    kind = SourceKind.AGGREGATOR
    required_credentials = ("api_key",)
    max_queries = 8
    notes = (
        "Licensed reseller of Google Jobs results, which include LinkedIn and "
        "Indeed postings. Apply links usually resolve to the employer ATS, "
        "which makes this a strong discovery source."
    )

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        text = query.text
        if query.location:
            text = f"{text} in {query.location}"
        payload = await ctx.http.get_json(
            JSEARCH_API,
            params={"query": text, "page": 1, "num_pages": 2, "date_posted": "month"},
            headers={
                "X-RapidAPI-Key": ctx.credentials.get("api_key") or "",
                "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
            },
        )
        items = (payload or {}).get("data") or []
        out: list[RawJob] = []
        for item in items:
            company = item.get("employer_name") or ""
            if not company:
                continue
            location = ", ".join(
                str(p)
                for p in (item.get("job_city"), item.get("job_state"), item.get("job_country"))
                if p
            )
            out.append(
                self.make_job(
                    source_job_id=str(item.get("job_id")),
                    title=item.get("job_title") or "",
                    company=company,
                    url=item.get("job_apply_link"),
                    apply_url=item.get("job_apply_link"),
                    company_url=item.get("employer_website"),
                    location=location or None,
                    description=item.get("job_description") or "",
                    employment_type=item.get("job_employment_type"),
                    remote_status="remote" if item.get("job_is_remote") else None,
                    salary_min=item.get("job_min_salary"),
                    salary_max=item.get("job_max_salary"),
                    salary_currency=item.get("job_salary_currency"),
                    salary_period=item.get("job_salary_period"),
                    date_posted=parse_date(item.get("job_posted_at_datetime_utc")),
                    deadline=parse_date(item.get("job_offer_expiration_datetime_utc")),
                    raw={"publisher": item.get("job_publisher")},
                )
            )
        return out


class USAJobsSource(QueryJobSource):
    name = "usajobs"
    display_name = "USAJOBS (federal)"
    kind = SourceKind.JOB_BOARD
    required_credentials = ("api_key", "email")
    max_queries = 6
    notes = "Free government API. Covers federal internships and Pathways roles."

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        params: dict[str, Any] = {
            "Keyword": query.text,
            "ResultsPerPage": min(query.max_results, 100),
            "WhoMayApply": "student",
        }
        if query.location:
            params["LocationName"] = query.location
        payload = await ctx.http.get_json(
            USAJOBS_API,
            params=params,
            headers={
                "Host": "data.usajobs.gov",
                "User-Agent": ctx.credentials.get("email") or "",
                "Authorization-Key": ctx.credentials.get("api_key") or "",
            },
        )
        items = ((payload or {}).get("SearchResult") or {}).get("SearchResultItems") or []
        out: list[RawJob] = []
        for entry in items:
            item = entry.get("MatchedObjectDescriptor") or {}
            company = item.get("OrganizationName") or item.get("DepartmentName") or ""
            if not company:
                continue
            details = (item.get("UserArea") or {}).get("Details") or {}
            remuneration = item.get("PositionRemuneration") or [{}]
            out.append(
                self.make_job(
                    source_job_id=str(entry.get("MatchedObjectId")),
                    title=item.get("PositionTitle") or "",
                    company=company,
                    url=item.get("PositionURI"),
                    apply_url=(item.get("ApplyURI") or [None])[0],
                    location=item.get("PositionLocationDisplay"),
                    description=details.get("JobSummary") or "",
                    requirements=details.get("Requirements") or None,
                    preferred_qualifications=details.get("Evaluations") or None,
                    employment_type=(item.get("PositionSchedule") or [{}])[0].get("Name"),
                    salary_min=_as_float(remuneration[0].get("MinimumRange")),
                    salary_max=_as_float(remuneration[0].get("MaximumRange")),
                    salary_currency="USD",
                    date_posted=parse_date(item.get("PublicationStartDate")),
                    deadline=parse_date(item.get("ApplicationCloseDate")),
                    # Federal roles are citizenship-gated; state it as evidence
                    # rather than letting the extractor guess.
                    sponsorship_hint=details.get("WhoMayApply", {}).get("Name")
                    if isinstance(details.get("WhoMayApply"), dict)
                    else None,
                    raw={"id": entry.get("MatchedObjectId")},
                )
            )
        return out


class JoobleSource(QueryJobSource):
    name = "jooble"
    display_name = "Jooble"
    kind = SourceKind.AGGREGATOR
    required_credentials = ("api_key",)
    max_queries = 6
    notes = "Free API key on request."

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        payload = await ctx.http.post_json(
            JOOBLE_API.format(key=ctx.credentials.get("api_key")),
            {"keywords": query.text, "location": query.location or ""},
        )
        items = (payload or {}).get("jobs") or []
        out: list[RawJob] = []
        for item in items:
            company = item.get("company") or ""
            if not company:
                continue
            out.append(
                self.make_job(
                    source_job_id=str(item.get("id")),
                    title=item.get("title") or "",
                    company=company,
                    url=item.get("link"),
                    location=item.get("location"),
                    description=item.get("snippet") or "",
                    salary_raw=item.get("salary") or None,
                    employment_type=item.get("type"),
                    date_posted=parse_date(item.get("updated")),
                    raw={"source": item.get("source")},
                )
            )
        return out


class SerpApiGoogleJobsSource(QueryJobSource):
    name = "serpapi_google_jobs"
    display_name = "Google Jobs (SerpApi)"
    kind = SourceKind.AGGREGATOR
    required_credentials = ("api_key",)
    max_queries = 5
    notes = "Paid licensed access to Google Jobs results."

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        params = {
            "engine": "google_jobs",
            "q": f"{query.text} {query.location}" if query.location else query.text,
            "api_key": ctx.credentials.get("api_key"),
            "hl": "en",
        }
        payload = await ctx.http.get_json(SERPAPI, params=params)
        items = (payload or {}).get("jobs_results") or []
        out: list[RawJob] = []
        for item in items:
            company = item.get("company_name") or ""
            if not company:
                continue
            options = item.get("apply_options") or []
            apply_url = options[0].get("link") if options else item.get("share_link")
            extensions = item.get("detected_extensions") or {}
            out.append(
                self.make_job(
                    source_job_id=str(item.get("job_id") or item.get("share_link")),
                    title=item.get("title") or "",
                    company=company,
                    url=apply_url or item.get("share_link"),
                    apply_url=apply_url,
                    location=item.get("location"),
                    description=item.get("description") or "",
                    employment_type=extensions.get("schedule_type"),
                    salary_raw=extensions.get("salary"),
                    raw={"via": item.get("via")},
                )
            )
        return out


def _as_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
