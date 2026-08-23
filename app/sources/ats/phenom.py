"""Phenom People career sites.

Phenom hosts the careers page of a number of large employers that are absent
from every other ATS this project crawls -- AMD among them, which is why this
source exists. AMD's board reported a single job for weeks; it advertises 44
internships, and none of them were reachable because AMD is not on Workday and
never was.

Each tenant serves its own careers page from the same public JSON endpoint::

    GET https://{host}/api/jobs?keywords=intern&limit=100&page=1

Unlike Workday, the list response carries the **full description**, so there is
no hydration step and no per-posting budget: one request returns everything the
scorer needs. Filtering is server-side via ``keywords`` -- note the plural, as
the singular ``keyword`` is silently ignored and returns the entire corpus,
which is how this endpoint quietly hands back 1,171 jobs when asked for 44.
"""

from __future__ import annotations

import re
from typing import Any

from app.logging_setup import get_logger
from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import BoardJobSource, SourceContext, first_text
from app.sources.http import FetchError

log = get_logger("phenom")

#: The endpoint honours limits up to at least 100; beyond the result set size
#: it simply returns everything, so one page usually suffices.
PAGE_SIZE = 100
MAX_PAGES = 5

#: Search terms issued per board. Phenom's ``keywords`` filter is a plain
#: text match over the posting, so these are cheap and complementary.
SEARCH_TERMS: tuple[str, ...] = ("intern", "co-op", "university graduate")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


def _strip_html(value: str | None) -> str | None:
    """Phenom returns descriptions as HTML; the scorer reads plain text."""
    if not value:
        return None
    text = _TAG_RE.sub(" ", value)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    text = re.sub(r"[ \t]{2,}", " ", text)
    return _WS_RE.sub("\n", text).strip() or None


class PhenomSource(BoardJobSource):
    name = "phenom"
    display_name = "Phenom People career sites"
    kind = SourceKind.ATS
    provider = "phenom"
    notes = (
        "Public JSON endpoint used by each tenant's own careers page. "
        "Descriptions arrive inline, so no hydration is needed."
    )

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        extra = board.get("extra") or {}
        host = extra.get("host")
        if not host:
            raise FetchError(f"phenom board missing host: {board}")
        token = board.get("board_token") or host.split(".")[0]
        company = board.get("company_name") or token

        collected: dict[str, dict] = {}
        for term in SEARCH_TERMS:
            await self._paginate(host, term, ctx, collected)

        out: list[RawJob] = []
        for req_id, data in collected.items():
            title = data.get("title") or ""
            if not ctx.keep_title(title):
                continue

            location = data.get("full_location") or data.get("location_name")
            # A posting open in several cities lists them here; the normaliser
            # decides which one matters, not this source. The field is a bare
            # ``false`` when there is only one, so its type has to be checked
            # rather than trusted.
            extra_locations = data.get("multipleLocations")
            locations = (
                [str(x) for x in extra_locations if x]
                if isinstance(extra_locations, list)
                else []
            )

            description = _strip_html(data.get("description"))
            out.append(
                self.make_job(
                    source_job_id=f"{token}:{req_id}",
                    title=title,
                    company=data.get("hiring_organization") or company,
                    url=data.get("apply_url")
                    or f"https://{host}/careers-home/jobs/{req_id}",
                    apply_url=data.get("apply_url"),
                    location=location or None,
                    locations=locations,
                    description=description,
                    requirements=_strip_html(data.get("qualifications")),
                    responsibilities=_strip_html(data.get("responsibilities")),
                    employment_type=data.get("employment_type"),
                    remote_status=(
                        "remote"
                        if "remote" in str(data.get("location_type") or "").lower()
                        else None
                    ),
                    date_posted=parse_date(data.get("posted_date") or data.get("create_date")),
                    date_updated=parse_date(data.get("update_date")),
                    requisition_id=str(req_id),
                    department=data.get("department") or first_text(data.get("category")),
                    raw={"req_id": req_id, "board": token, "host": host},
                )
            )
        return out

    async def _paginate(
        self,
        host: str,
        term: str,
        ctx: SourceContext,
        collected: dict[str, dict],
    ) -> None:
        """Read pages for one search term until the tenant stops adding rows.

        Terms overlap heavily -- "intern" and "co-op" return many of the same
        postings -- so pagination stops as soon as a page contributes nothing
        new rather than after a fixed count.
        """
        for page in range(1, MAX_PAGES + 1):
            payload = await ctx.http.get_json(
                f"https://{host}/api/jobs",
                params={"keywords": term, "limit": PAGE_SIZE, "page": page},
            )
            rows = (payload or {}).get("jobs") or []
            if not rows:
                return

            added = 0
            for row in rows:
                data = row.get("data") if isinstance(row, dict) else None
                if not isinstance(data, dict):
                    continue
                req_id = str(data.get("req_id") or data.get("slug") or "")
                if req_id and req_id not in collected:
                    collected[req_id] = data
                    added += 1

            if added == 0 or len(rows) < PAGE_SIZE:
                return


#: Phenom tenants worth crawling, keyed by board token (the tenant's own
#: ``client_code``). Unlike Greenhouse or Lever, a Phenom board cannot be
#: guessed from a company name -- the host is an arbitrary careers domain -- so
#: the useful ones are recorded here rather than discovered.
#:
#: AMD is the reason this table exists. It is a preferred employer with 44 open
#: internships, and the crawler could see exactly one of them.
CURATED_BOARDS: dict[str, dict[str, Any]] = {
    "amd": {"company_name": "AMD", "host": "careers.amd.com"},
}
