"""Greenhouse job boards.

Public, documented, no authentication:
``https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``
"""

from __future__ import annotations

import html
from typing import Any

from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import BoardJobSource, SourceContext

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


class GreenhouseSource(BoardJobSource):
    name = "greenhouse"
    display_name = "Greenhouse"
    kind = SourceKind.ATS
    provider = "greenhouse"
    notes = "Public job board API, no credentials required."

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        token = board["board_token"]
        payload = await ctx.http.get_json(API.format(token=token), params={"content": "true"})
        jobs_data = (payload or {}).get("jobs") or []
        company = board.get("company_name") or token

        out: list[RawJob] = []
        for item in jobs_data:
            job_id = str(item.get("id") or "")
            if not job_id or not ctx.keep_title(item.get("title")):
                continue
            offices = [o.get("name") for o in (item.get("offices") or []) if o.get("name")]
            location = (item.get("location") or {}).get("name") or (offices[0] if offices else None)
            meta = {
                str(f.get("name", "")).lower(): f.get("value")
                for f in (item.get("metadata") or [])
                if isinstance(f, dict)
            }
            departments = [d.get("name") for d in (item.get("departments") or []) if d.get("name")]

            out.append(
                self.make_job(
                    source_job_id=f"{token}:{job_id}",
                    title=item.get("title") or "",
                    company=item.get("company_name") or company,
                    url=item.get("absolute_url"),
                    apply_url=item.get("absolute_url"),
                    location=location,
                    locations=offices,
                    description=html.unescape(item.get("content") or ""),
                    date_posted=parse_date(item.get("first_published") or item.get("updated_at")),
                    date_updated=parse_date(item.get("updated_at")),
                    requisition_id=str(item.get("requisition_id") or "") or None,
                    department=departments[0] if departments else None,
                    sponsorship_hint=str(meta.get("sponsorship") or "") or None,
                    raw={"id": job_id, "board": token},
                )
            )
        return out
