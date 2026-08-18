"""Ashby public posting API.

``https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true``
"""

from __future__ import annotations

from typing import Any

from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import BoardJobSource, SourceContext

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class AshbySource(BoardJobSource):
    name = "ashby"
    display_name = "Ashby"
    kind = SourceKind.ATS
    provider = "ashby"
    notes = "Public posting API, no credentials required."

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        token = board["board_token"]
        payload = await ctx.http.get_json(
            API.format(token=token), params={"includeCompensation": "true"}
        )
        items = (payload or {}).get("jobs") or []
        company = board.get("company_name") or token

        out: list[RawJob] = []
        for item in items:
            job_id = str(item.get("id") or "")
            if not job_id or not ctx.keep_title(item.get("title")):
                continue
            comp = item.get("compensation") or {}
            summary = comp.get("compensationTierSummary") or comp.get("summaryComponents")
            secondary = [
                loc.get("location")
                for loc in (item.get("secondaryLocations") or [])
                if isinstance(loc, dict) and loc.get("location")
            ]

            out.append(
                self.make_job(
                    source_job_id=f"{token}:{job_id}",
                    title=item.get("title") or "",
                    company=item.get("organizationName") or company,
                    url=item.get("jobUrl") or item.get("applyUrl"),
                    apply_url=item.get("applyUrl") or item.get("jobUrl"),
                    location=item.get("location"),
                    locations=secondary,
                    description=item.get("descriptionHtml") or item.get("descriptionPlain") or "",
                    employment_type=item.get("employmentType"),
                    remote_status="remote" if item.get("isRemote") else None,
                    department=item.get("department") or item.get("team"),
                    salary_raw=summary if isinstance(summary, str) else None,
                    date_posted=parse_date(item.get("publishedAt") or item.get("updatedAt")),
                    date_updated=parse_date(item.get("updatedAt")),
                    raw={"id": job_id, "board": token},
                )
            )
        return out
