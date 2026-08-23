"""Eightfold AI career sites.

Microsoft is the reason this exists. It had no registered board at all, so its
entire presence in the corpus arrived second-hand through curated GitHub lists
-- twelve jobs for an employer that runs hundreds of intern requisitions.

The obvious endpoint was a dead end twice over. `gcsservices.careers.microsoft.com`,
the host every guide names, now serves a certificate for `*.azureedge.net` and
404s regardless; and `jobs.careers.microsoft.com` redirects to a new platform
at `apply.careers.microsoft.com` that speaks a different API entirely. Watching
what that page actually requests is what found this::

    GET https://{host}/api/pcsx/search?query=intern&start=0&domain={domain}&num_items=10
    GET https://{host}/api/pcsx/position_details?position_id={id}&domain={domain}

That is Eightfold, a platform with many large tenants, so this is written
against the platform rather than against Microsoft. Like Workday, the list
response carries no description, so descriptions are hydrated only for postings
that survive the title gate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.logging_setup import get_logger
from app.models.base import SourceKind
from app.schemas.job import RawJob
from app.sources.base import BoardJobSource, SourceContext, first_text
from app.sources.http import FetchError

log = get_logger("eightfold")

#: ``num_items`` above this is ignored by the API, which returns ten regardless.
PAGE_SIZE = 10
MAX_PAGES = 10
#: Descriptions fetched per board per run, mirroring the Workday budget.
HYDRATE_LIMIT = 25

SEARCH_TERMS: tuple[str, ...] = ("intern", "internship", "university graduate")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")

_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&#39;": "'",
    "&quot;": '"',
}


def strip_html(value: str | None) -> str | None:
    """Descriptions arrive as HTML; the scorer reads plain text."""
    if not value:
        return None
    text = _TAG_RE.sub(" ", value)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return _WS_RE.sub("\n", text).strip() or None


def _posted_at(value: Any) -> datetime | None:
    """``postedTs`` is unix seconds. Anything else is not worth guessing at."""
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


class EightfoldSource(BoardJobSource):
    name = "eightfold"
    display_name = "Eightfold career sites"
    kind = SourceKind.ATS
    provider = "eightfold"
    notes = (
        "Public endpoint used by each tenant's own careers page. Descriptions "
        "are hydrated for postings that pass the title gate."
    )

    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        extra = board.get("extra") or {}
        host = extra.get("host")
        domain = extra.get("domain")
        if not (host and domain):
            raise FetchError(f"eightfold board missing host/domain: {board}")
        token = board.get("board_token") or domain.split(".")[0]
        company = board.get("company_name") or token

        collected: dict[str, dict] = {}
        for term in SEARCH_TERMS:
            await self._paginate(host, domain, term, ctx, collected)

        keep = {
            pid: row
            for pid, row in collected.items()
            if ctx.keep_title(row.get("name"))
        }
        details = await self._hydrate(host, domain, list(keep)[:HYDRATE_LIMIT], ctx)

        out: list[RawJob] = []
        for pid, row in keep.items():
            detail = details.get(pid) or {}
            locations = [str(x) for x in (row.get("locations") or []) if x]
            url = row.get("positionUrl") or f"/careers/job/{pid}"
            if url.startswith("/"):
                url = f"https://{host}{url}"

            out.append(
                self.make_job(
                    source_job_id=f"{token}:{pid}",
                    title=row.get("name") or "",
                    company=company,
                    url=url,
                    apply_url=url,
                    location=locations[0] if locations else None,
                    locations=locations[1:],
                    description=strip_html(detail.get("jobDescription")),
                    employment_type=first_text(detail.get("efcustomTextEmploymentType")),
                    remote_status=(
                        "remote"
                        if "remote" in str(row.get("workLocationOption") or "").lower()
                        else None
                    ),
                    date_posted=_posted_at(row.get("postedTs")),
                    # ``displayJobId`` is the number a human sees on the posting;
                    # ``id`` is the platform's internal key and means nothing to
                    # anyone reading the listing.
                    requisition_id=str(row.get("displayJobId") or pid),
                    department=first_text(row.get("department")),
                    raw={"position_id": pid, "board": token, "host": host},
                )
            )
        return out

    async def _paginate(
        self,
        host: str,
        domain: str,
        term: str,
        ctx: SourceContext,
        collected: dict[str, dict],
    ) -> None:
        """Read pages for one term until it stops contributing anything new.

        The terms overlap heavily and the API fuzzy-matches, so a fixed page
        count would spend most of its requests re-reading the same postings.
        """
        for page in range(MAX_PAGES):
            payload = await ctx.http.get_json(
                f"https://{host}/api/pcsx/search",
                params={
                    "query": term,
                    "start": page * PAGE_SIZE,
                    "domain": domain,
                    "num_items": PAGE_SIZE,
                    "sort_by": "relevance",
                },
            )
            rows = ((payload or {}).get("data") or {}).get("positions") or []
            if not rows:
                return

            added = 0
            for row in rows:
                pid = str(row.get("id") or "")
                if pid and pid not in collected:
                    collected[pid] = row
                    added += 1
            if added == 0:
                return

    async def _hydrate(
        self, host: str, domain: str, ids: list[str], ctx: SourceContext
    ) -> dict[str, dict]:
        """Fetch descriptions for the postings worth scoring.

        One request per posting, so this is bounded by ``HYDRATE_LIMIT`` rather
        than by however many the search happened to return.
        """
        if not ids:
            return {}
        results = await ctx.http.gather(
            [
                ctx.http.get_json(
                    f"https://{host}/api/pcsx/position_details",
                    params={"position_id": pid, "domain": domain, "hl": "en"},
                )
                for pid in ids
            ]
        )
        details: dict[str, dict] = {}
        for pid, result in zip(ids, results, strict=False):
            if isinstance(result, Exception) or not result:
                continue
            data = (result or {}).get("data")
            if isinstance(data, dict):
                details[pid] = data
        return details


#: Eightfold tenants worth crawling. As with Phenom, the host is an arbitrary
#: careers domain rather than something derivable from a company name, so the
#: useful ones are recorded rather than discovered.
CURATED_BOARDS: dict[str, dict[str, Any]] = {
    "microsoft": {
        "company_name": "Microsoft",
        "host": "apply.careers.microsoft.com",
        "domain": "microsoft.com",
    },
}
