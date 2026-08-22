"""Company and ATS-board discovery.

The preferred-company list is a *preference*, never a search boundary. This
module is what makes that true: every URL the pipeline encounters -- from
aggregators, curated lists, or HN comments -- is mined for an ATS board
identity. New boards are registered and crawled directly on later runs, so the
searchable universe grows on its own and unknown employers surface naturally.

Feedback loop::

    aggregators / lists / HN
        -> harvest ATS identities from URLs
        -> register boards + companies
        -> crawl those boards directly next run
        -> their apply URLs reveal yet more boards
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import AtsBoard, Company
from app.models.base import utcnow
from app.pipeline.identity import AtsIdentity, extract_ats_identity
from app.pipeline.textutil import slugify_company
from app.schemas.job import RawJob
from app.sources.registry import board_providers

log = get_logger("discovery")


def harvest_boards(raws: list[RawJob]) -> dict[str, dict]:
    """Extract candidate ATS boards from a batch of raw listings.

    Returns a mapping of ``provider:token`` to board descriptor.
    """
    found: dict[str, dict] = {}
    for raw in raws:
        identity: AtsIdentity | None = extract_ats_identity(
            raw.apply_url, raw.url, raw.company_url
        )
        if identity is None or not identity.board_token:
            continue
        key = identity.board_key
        entry = found.get(key)
        if entry is None:
            found[key] = {
                "provider": identity.provider,
                "board_token": identity.board_token.lower(),
                "extra": dict(identity.extra or {}),
                "company_name": raw.company,
                "discovered_via": raw.source,
                "count": 1,
            }
        else:
            entry["count"] += 1
            if not entry.get("extra") and identity.extra:
                entry["extra"] = dict(identity.extra)
    return found


def register_companies(
    session: Session, raws: list[RawJob], *, preferred: set[str], blacklisted: set[str]
) -> int:
    """Create ``Company`` rows for every employer seen. Returns new count."""
    now = utcnow()
    slugs: dict[str, RawJob] = {}
    for raw in raws:
        slug = slugify_company(raw.company)
        if slug and slug not in slugs:
            slugs[slug] = raw
    if not slugs:
        return 0

    existing = {
        row.slug: row
        for row in session.scalars(select(Company).where(Company.slug.in_(list(slugs)))).all()
    }
    created = 0
    for slug, raw in slugs.items():
        company = existing.get(slug)
        if company is None:
            company = Company(
                slug=slug,
                name=raw.company.strip(),
                website=raw.company_url,
                first_seen_at=now,
                last_seen_at=now,
                discovered_via=raw.source,
                is_preferred=slug in preferred,
                is_blacklisted=slug in blacklisted,
                jobs_seen_count=0,
            )
            session.add(company)
            created += 1
        else:
            company.last_seen_at = now
            if not company.website and raw.company_url:
                company.website = raw.company_url
            # Keep preference flags in sync with current settings.
            company.is_preferred = slug in preferred
            company.is_blacklisted = slug in blacklisted
        company.jobs_seen_count = (company.jobs_seen_count or 0) + 1
    session.flush()
    return created


def register_boards(session: Session, boards: dict[str, dict]) -> int:
    """Persist newly discovered ATS boards. Returns new count."""
    if not boards:
        return 0
    providers = set(board_providers())

    tokens = [(b["provider"], b["board_token"]) for b in boards.values()]
    existing_rows = session.scalars(
        select(AtsBoard).where(
            AtsBoard.board_token.in_([t for _, t in tokens]),
        )
    ).all()
    existing = {(row.provider, row.board_token): row for row in existing_rows}

    created = 0
    for entry in boards.values():
        provider = entry["provider"]
        # Only register boards we can actually crawl; others are still useful
        # for deduplication but would be dead weight in the crawl registry.
        if provider not in providers:
            continue
        key = (provider, entry["board_token"])
        row = existing.get(key)
        if row is None:
            company = session.scalar(
                select(Company).where(Company.slug == slugify_company(entry.get("company_name")))
            )
            row = AtsBoard(
                provider=provider,
                board_token=entry["board_token"],
                extra=entry.get("extra") or {},
                company_id=company.id if company else None,
                discovered_via=entry.get("discovered_via"),
                enabled=True,
            )
            session.add(row)
            created += 1
        elif entry.get("extra") and not row.extra:
            row.extra = entry["extra"]
    session.flush()
    if created:
        log.info("discovery.boards_registered", count=created)
    return created


def select_boards_to_crawl(session: Session, limit: int) -> list[dict]:
    """Choose which boards to crawl this run.

    Ordered by least-recently-crawled so the whole registry is covered over
    successive runs rather than always hitting the same head of the list.
    Boards that keep failing are backed off but never permanently dropped --
    a source that is broken today may be fine tomorrow.
    """
    cutoff = utcnow() - timedelta(days=3)
    rows = session.scalars(
        select(AtsBoard)
        .where(AtsBoard.enabled.is_(True))
        .where(AtsBoard.consecutive_failures < 8)
        .order_by(
            AtsBoard.consecutive_failures.asc(),
            AtsBoard.last_crawled_at.is_(None).desc(),
            AtsBoard.last_crawled_at.asc(),
        )
        .limit(limit)
    ).all()

    out: list[dict] = []
    for row in rows:
        # Skip boards crawled very recently unless the budget is unfilled.
        if row.last_crawled_at and row.last_crawled_at > cutoff and len(out) >= limit // 2:
            continue
        out.append(
            {
                "id": row.id,
                "provider": row.provider,
                "board_token": row.board_token,
                "extra": row.extra or {},
                "company_name": row.company.name if row.company else None,
            }
        )
    return out


def record_board_results(
    session: Session, boards: list[dict], failed: set[int],
    yields: dict[int, int] | None = None,
) -> None:
    """Update crawl bookkeeping for the boards attempted this run.

    ``yields`` maps board id to the number of listings it returned. Recording
    it is what makes a board that has quietly stopped producing visible: a
    board whose token has gone stale still answers, still counts as a success,
    and returns nothing -- which reads identically to a healthy board on every
    other field.
    """
    now = utcnow()
    counts = yields or {}
    ids = [b["id"] for b in boards if b.get("id")]
    if not ids:
        return
    rows = session.scalars(select(AtsBoard).where(AtsBoard.id.in_(ids))).all()
    for row in rows:
        row.last_crawled_at = now
        if row.id in failed:
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
        else:
            row.consecutive_failures = 0
            row.last_success_at = now
        if row.id in counts:
            found = int(counts[row.id])
            row.jobs_last_crawl = found
            row.relevant_jobs_total = (row.relevant_jobs_total or 0) + found
    session.flush()


def seed_boards_for_companies(
    session: Session, company_names: list[str]
) -> list[dict]:
    """Best-effort board candidates for companies the user named.

    Company names are turned into plausible board tokens for each provider.
    Wrong guesses simply return no jobs, so this is safe to run without a
    lookup table -- but they are marked ``speculative`` so that a guess which
    does not exist is never reported as a source failure. Most large employers
    do not use Greenhouse at all, so most of these are expected to 404.
    """
    candidates: list[dict] = []
    for name in company_names:
        slug = slugify_company(name)
        if not slug:
            continue
        token = slug.replace(" ", "")
        hyphen = slug.replace(" ", "-")
        for provider in ("greenhouse", "lever", "ashby"):
            for candidate in {token, hyphen}:
                existing = session.scalar(
                    select(AtsBoard).where(
                        AtsBoard.provider == provider, AtsBoard.board_token == candidate
                    )
                )
                if existing is None:
                    candidates.append(
                        {
                            "provider": provider,
                            "board_token": candidate,
                            "company_name": name,
                            "speculative": True,
                        }
                    )
    return candidates


def prune_failed_boards(session: Session, *, max_failures: int = 12) -> int:
    """Disable boards that have failed persistently.

    Disabled, not deleted: they remain visible in the UI and can be re-enabled.
    """
    rows = session.scalars(
        select(AtsBoard).where(
            AtsBoard.enabled.is_(True), AtsBoard.consecutive_failures >= max_failures
        )
    ).all()
    for row in rows:
        row.enabled = False
        row.last_error = f"disabled after {row.consecutive_failures} consecutive failures"
    session.flush()
    return len(rows)


def stale_monitored_companies(session: Session, hours: int = 12) -> list[Company]:
    """Monitored companies whose boards have not been checked recently."""
    cutoff = utcnow() - timedelta(hours=hours)
    return list(
        session.scalars(
            select(Company).where(
                Company.is_monitored.is_(True),
                (Company.last_seen_at.is_(None)) | (Company.last_seen_at < cutoff),
            )
        ).all()
    )


def discovery_summary(session: Session) -> dict[str, int | datetime | None]:
    """Headline discovery numbers for the coverage dashboard."""
    from sqlalchemy import func

    total_companies = session.scalar(select(func.count(Company.id))) or 0
    total_boards = session.scalar(select(func.count(AtsBoard.id))) or 0
    active_boards = (
        session.scalar(select(func.count(AtsBoard.id)).where(AtsBoard.enabled.is_(True))) or 0
    )
    crawled = (
        session.scalar(
            select(func.count(AtsBoard.id)).where(AtsBoard.last_success_at.is_not(None))
        )
        or 0
    )
    return {
        "companies": total_companies,
        "boards": total_boards,
        "active_boards": active_boards,
        "boards_crawled": crawled,
    }
