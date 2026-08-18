"""Additional public ATS APIs: SmartRecruiters, Workable, and Recruitee."""

from __future__ import annotations

from typing import Any

from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import BoardJobSource, SourceContext

SMARTRECRUITERS_LIST = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
SMARTRECRUITERS_DETAIL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}"
WORKABLE_LIST = "https://apply.workable.com/api/v1/accounts/{token}"
RECRUITEE_LIST = "https://{token}.recruitee.com/api/offers/"

#: Detail hydration budget per board, mirroring the Workday approach.
HYDRATE_LIMIT = 25


class SmartRecruitersSource(BoardJobSource):
    name = "smartrecruiters"
    display_name = "SmartRecruiters"
    kind = SourceKind.ATS
    provider = "smartrecruiters"
    notes = "Public postings API, no credentials required."

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        token = board["board_token"]
        company = board.get("company_name") or token
        payload = await ctx.http.get_json(
            SMARTRECRUITERS_LIST.format(token=token), params={"limit": 100}
        )
        items = (payload or {}).get("content") or []

        hydrate = [
            str(i.get("id"))
            for i in items
            if i.get("id") and self._looks_relevant(i.get("name") or "")
        ][:HYDRATE_LIMIT]
        details: dict[str, dict] = {}
        if hydrate:
            results = await ctx.http.gather(
                [
                    ctx.http.get_json(SMARTRECRUITERS_DETAIL.format(token=token, job_id=jid))
                    for jid in hydrate
                ]
            )
            for jid, result in zip(hydrate, results, strict=False):
                if not isinstance(result, Exception) and result:
                    details[jid] = result

        out: list[RawJob] = []
        for item in items:
            job_id = str(item.get("id") or "")
            if not job_id or not ctx.keep_title(item.get("name")):
                continue
            loc = item.get("location") or {}
            location = ", ".join(
                str(p) for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
            )
            detail = details.get(job_id, {})
            ad = (detail.get("jobAd") or {}).get("sections") or {}
            description = "\n\n".join(
                (ad.get(sec) or {}).get("text", "")
                for sec in ("companyDescription", "jobDescription")
            ).strip()
            requirements = (ad.get("qualifications") or {}).get("text") or None

            out.append(
                self.make_job(
                    source_job_id=f"{token}:{job_id}",
                    title=item.get("name") or "",
                    company=(item.get("company") or {}).get("name") or company,
                    url=item.get("applyUrl")
                    or f"https://jobs.smartrecruiters.com/{token}/{job_id}",
                    apply_url=item.get("applyUrl"),
                    location=location or None,
                    description=description or None,
                    requirements=requirements,
                    employment_type=(item.get("typeOfEmployment") or {}).get("label"),
                    remote_status="remote" if loc.get("remote") else None,
                    date_posted=parse_date(item.get("releasedDate")),
                    department=(item.get("department") or {}).get("label"),
                    raw={"id": job_id, "board": token},
                )
            )
        return out

    def _looks_relevant(self, title: str) -> bool:
        low = title.lower()
        return "intern" in low or "co-op" in low or "student" in low


class WorkableSource(BoardJobSource):
    name = "workable"
    display_name = "Workable"
    kind = SourceKind.ATS
    provider = "workable"
    notes = "Public account jobs API, no credentials required."

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        token = board["board_token"]
        company = board.get("company_name") or token
        payload = await ctx.http.post_json(
            WORKABLE_LIST.format(token=token) + "/jobs", {"query": "", "location": []}
        )
        items = (payload or {}).get("results") or []

        out: list[RawJob] = []
        for item in items:
            shortcode = str(item.get("shortcode") or "")
            if not shortcode or not ctx.keep_title(item.get("title")):
                continue
            loc_parts = [item.get("city"), item.get("state"), item.get("country")]
            location = ", ".join(str(p) for p in loc_parts if p)
            out.append(
                self.make_job(
                    source_job_id=f"{token}:{shortcode}",
                    title=item.get("title") or "",
                    company=company,
                    url=item.get("url") or f"https://apply.workable.com/{token}/j/{shortcode}/",
                    apply_url=item.get("application_url") or item.get("url"),
                    location=location or None,
                    description=item.get("description") or "",
                    remote_status="remote" if item.get("remote") else None,
                    employment_type=item.get("employment_type"),
                    department=item.get("department"),
                    date_posted=parse_date(item.get("published_on") or item.get("created_at")),
                    raw={"shortcode": shortcode, "board": token},
                )
            )
        return out


class RecruiteeSource(BoardJobSource):
    name = "recruitee"
    display_name = "Recruitee"
    kind = SourceKind.ATS
    provider = "recruitee"
    notes = "Public offers API, no credentials required."

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        token = board["board_token"]
        company = board.get("company_name") or token
        payload = await ctx.http.get_json(RECRUITEE_LIST.format(token=token))
        items = (payload or {}).get("offers") or []

        out: list[RawJob] = []
        for item in items:
            job_id = str(item.get("id") or "")
            if not job_id or not ctx.keep_title(item.get("title")):
                continue
            out.append(
                self.make_job(
                    source_job_id=f"{token}:{job_id}",
                    title=item.get("title") or "",
                    company=company,
                    url=item.get("careers_url") or item.get("careers_apply_url"),
                    apply_url=item.get("careers_apply_url"),
                    location=item.get("location"),
                    description=item.get("description") or "",
                    requirements=item.get("requirements") or None,
                    employment_type=item.get("employment_type_code"),
                    remote_status="remote" if item.get("remote") else None,
                    department=item.get("department"),
                    date_posted=parse_date(item.get("published_at")),
                    raw={"id": job_id, "board": token},
                )
            )
        return out
