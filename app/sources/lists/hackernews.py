"""Hacker News "Ask HN: Who is hiring?" threads.

The monthly thread is where small and early-stage companies post directly, so
it surfaces employers that never appear on aggregators -- exactly the "unknown
startup with an excellent FPGA internship" case. Fetched through the free
Algolia HN API.

Comments are free-form, so parsing is deliberately conservative: a comment is
only emitted as a job when a company and a plausible role line can both be
recovered.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.models.base import SourceKind, utcnow
from app.pipeline.extract import parse_date
from app.pipeline.textutil import strip_html
from app.schemas.job import RawJob
from app.sources.base import JobSource, SourceContext

SEARCH_API = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_API = "https://hn.algolia.com/api/v1/items/{item_id}"

#: Separators used in the conventional "Company | Role | Location" header line.
_SEP = re.compile(r"\s*[|•·—–]\s*|\s+-\s+")
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_REMOTE_RE = re.compile(r"\bremote\b", re.I)
_INTERN_RE = re.compile(r"\b(intern|internship|co-?op)\b", re.I)


class HackerNewsWhoIsHiring(JobSource):
    name = "hackernews"
    display_name = "HN Who is Hiring"
    kind = SourceKind.JOB_BOARD
    is_discovery_source = True
    notes = "Free Algolia API. Startup discovery; conservative comment parsing."

    #: How many recent monthly threads to read.
    threads = 2

    async def fetch(self, ctx: SourceContext) -> list[RawJob]:
        payload = await ctx.http.get_json(
            SEARCH_API,
            # search_by_date returns newest first, so the current month's
            # thread is always hit 0 rather than a decade-old one.
            params={
                "tags": "story,author_whoishiring",
                "hitsPerPage": self.threads * 3,
            },
        )
        hits = (payload or {}).get("hits") or []
        # The account also posts "Who wants to be hired?"; keep hiring threads.
        story_ids = [
            h.get("objectID")
            for h in hits
            if h.get("objectID") and "who is hiring" in (h.get("title") or "").lower()
        ][: self.threads]
        if not story_ids:
            return []

        results = await ctx.http.gather(
            [ctx.http.get_json(ITEM_API.format(item_id=sid)) for sid in story_ids]
        )
        out: list[RawJob] = []
        for result in results:
            if isinstance(result, Exception) or not result:
                continue
            story_date = parse_date(result.get("created_at"))
            for child in result.get("children") or []:
                job = self._parse_comment(child, story_date)
                if job is not None:
                    out.append(job)
        return out

    def _parse_comment(self, comment: dict[str, Any], story_date: datetime | None) -> RawJob | None:
        text = strip_html(comment.get("text") or "")
        if not text or len(text) < 40:
            return None
        comment_id = str(comment.get("id") or "")
        if not comment_id:
            return None

        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            return None
        header = lines[0]

        parts = [p.strip() for p in _SEP.split(header) if p.strip()]
        if len(parts) < 2:
            return None
        company = parts[0]
        # Guard against prose being mistaken for a company name.
        if len(company) > 60 or len(company.split()) > 6:
            return None

        # Prefer a segment that reads like a role; otherwise use the next field.
        role = None
        for part in parts[1:]:
            if _INTERN_RE.search(part) or re.search(
                r"\b(engineer|developer|scientist|designer|intern|architect|analyst)\b", part, re.I
            ):
                role = part
                break
        if role is None:
            role = parts[1]
        if len(role) > 120:
            role = role[:120]

        location = None
        for part in parts[1:]:
            if part is role:
                continue
            if _REMOTE_RE.search(part) or re.search(r"[A-Z]{2}\b|,", part):
                location = part
                break
        if location is None and _REMOTE_RE.search(text[:400]):
            location = "Remote"

        urls = _URL_RE.findall(text)
        apply_url = urls[0] if urls else None
        # Prefer a link that points at a known ATS: better dedup and discovery.
        for candidate in urls:
            if any(
                host in candidate
                for host in ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "rippling")
            ):
                apply_url = candidate
                break

        posted = story_date or parse_date(comment.get("created_at"))
        if posted and posted < utcnow() - timedelta(days=90):
            return None

        return self.make_job(
            source_job_id=f"hn:{comment_id}",
            title=role,
            company=company,
            url=apply_url or f"https://news.ycombinator.com/item?id={comment_id}",
            apply_url=apply_url,
            location=location,
            description=text,
            remote_status="remote" if _REMOTE_RE.search(text[:400]) else None,
            date_posted=posted,
            raw={"hn_id": comment_id},
        )
