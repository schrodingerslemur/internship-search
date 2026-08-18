"""Lever postings API.

Public, documented, no authentication:
``https://api.lever.co/v0/postings/{company}?mode=json``
"""

from __future__ import annotations

from typing import Any

from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import BoardJobSource, SourceContext

API = "https://api.lever.co/v0/postings/{token}"


class LeverSource(BoardJobSource):
    name = "lever"
    display_name = "Lever"
    kind = SourceKind.ATS
    provider = "lever"
    notes = "Public postings API, no credentials required."

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        token = board["board_token"]
        payload = await ctx.http.get_json(API.format(token=token), params={"mode": "json"})
        if not isinstance(payload, list):
            return []
        company = board.get("company_name") or token

        out: list[RawJob] = []
        for item in payload:
            job_id = str(item.get("id") or "")
            if not job_id or not ctx.keep_title(item.get("text")):
                continue
            categories = item.get("categories") or {}
            lists = item.get("lists") or []
            # Lever splits requirements into named list blocks.
            requirements = "\n".join(
                f"{block.get('text', '')}\n{block.get('content', '')}"
                for block in lists
                if isinstance(block, dict)
            )
            workplace = item.get("workplaceType")

            out.append(
                self.make_job(
                    source_job_id=f"{token}:{job_id}",
                    title=item.get("text") or "",
                    company=company,
                    url=item.get("hostedUrl") or item.get("applyUrl"),
                    apply_url=item.get("applyUrl") or item.get("hostedUrl"),
                    location=categories.get("location"),
                    locations=[
                        loc
                        for loc in (item.get("additionalPlain") and [] or [])
                        if loc
                    ]
                    or ([categories.get("location")] if categories.get("location") else []),
                    description=item.get("descriptionPlain") or item.get("description") or "",
                    requirements=requirements or None,
                    employment_type=categories.get("commitment"),
                    remote_status=workplace,
                    department=categories.get("team") or categories.get("department"),
                    date_posted=parse_date(item.get("createdAt")),
                    raw={"id": job_id, "board": token},
                )
            )
        return out
