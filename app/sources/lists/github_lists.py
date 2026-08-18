"""Community-maintained internship lists published on GitHub.

These public JSON files are the single richest *seed* for company discovery:
one fetch yields thousands of internship postings whose URLs point straight at
employer ATS boards. Those boards are then registered and crawled directly on
later runs, which surfaces postings the list itself never contained.

The data is public, served from raw.githubusercontent.com, and used here as
published.
"""

from __future__ import annotations

from typing import Any

from app.models.base import SourceKind
from app.pipeline.extract import parse_date
from app.schemas.job import RawJob
from app.sources.base import JobSource, SourceContext

#: Each entry is (label, raw-JSON URL). Add more lists freely.
LIST_SOURCES: tuple[tuple[str, str], ...] = (
    (
        "simplify-summer2026",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    ),
    (
        "vanshb03-summer2026",
        "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json",
    ),
)


class GithubInternshipLists(JobSource):
    name = "github_lists"
    display_name = "Community internship lists"
    kind = SourceKind.CURATED_LIST
    is_discovery_source = True
    notes = (
        "Public GitHub JSON feeds. Primary seed for ATS board discovery; "
        "entries carry direct employer application links."
    )

    async def fetch(self, ctx: SourceContext) -> list[RawJob]:
        results = await ctx.http.gather(
            [ctx.http.get_json(url) for _, url in LIST_SOURCES]
        )
        out: list[RawJob] = []
        seen: set[str] = set()
        for (label, _), payload in zip(LIST_SOURCES, results, strict=False):
            if isinstance(payload, Exception) or not isinstance(payload, list):
                continue
            for item in payload:
                job = self._parse(label, item)
                if job is None:
                    continue
                if job.source_job_id in seen:
                    continue
                seen.add(job.source_job_id)
                out.append(job)
        return out

    def _parse(self, label: str, item: dict[str, Any]) -> RawJob | None:
        if not isinstance(item, dict):
            return None
        # Respect the lists' own activity flags.
        if item.get("active") is False or item.get("is_visible") is False:
            return None
        company = (item.get("company_name") or "").strip()
        title = (item.get("title") or "").strip()
        url = item.get("url")
        if not (company and title and url):
            return None

        locations = [str(loc) for loc in (item.get("locations") or []) if loc]
        sponsorship = item.get("sponsorship")
        # These feeds use short codes; pass them through as *evidence* and let
        # the extractor decide, so "Other" never becomes a false positive.
        hint = None
        if isinstance(sponsorship, str):
            mapping = {
                "Does Not Offer Sponsorship": "We do not offer visa sponsorship.",
                "U.S. Citizenship is Required": "U.S. citizenship is required.",
                "Offers Sponsorship": "We offer visa sponsorship.",
            }
            hint = mapping.get(sponsorship.strip())

        return self.make_job(
            source_job_id=f"{label}:{item.get('id') or url}",
            title=title,
            company=company,
            url=url,
            apply_url=url,
            company_url=item.get("company_url"),
            location=locations[0] if locations else None,
            locations=locations,
            terms=[str(t) for t in (item.get("terms") or []) if t],
            date_posted=parse_date(item.get("date_posted")),
            date_updated=parse_date(item.get("date_updated")),
            employment_type="internship",
            sponsorship_hint=hint,
            department=item.get("category"),
            raw={"list": label, "degrees": item.get("degrees")},
        )
