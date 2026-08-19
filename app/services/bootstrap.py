"""First-boot bootstrapping.

Seeding the company + ATS registry means fetching several large curated lists,
which takes minutes rather than seconds. On a hosted deployment that is far too
long to do before the web server binds its port -- the platform health check
would fail and the deploy would be rolled back. So seeding runs *after* the app
is serving, in the background, exactly once per database.

"Once per database" is decided by asking the database, not by a marker file on
disk. A file would be wrong in both directions: a container with a fresh disk
pointed at an existing Postgres would re-seed needlessly, and an ephemeral CI
runner would re-seed on every single run.
"""

from __future__ import annotations

import asyncio

from app.logging_setup import get_logger

log = get_logger("bootstrap")

#: A registry smaller than this is treated as unseeded. A handful of boards can
#: be left behind by a failed partial run; a real seed yields hundreds.
MIN_SEEDED_BOARDS = 25


def needs_seed() -> bool:
    """True when the registry is empty enough to be worth seeding.

    Never raises: if the database cannot be read yet, the honest answer is
    "do not start a seed", because something more fundamental is wrong.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import AtsBoard

    try:
        with session_scope() as session:
            boards = session.scalar(select(func.count(AtsBoard.id))) or 0
    except Exception:
        log.exception("bootstrap.seed_check_failed")
        return False
    return boards < MIN_SEEDED_BOARDS


async def seed_registry() -> dict[str, int]:
    """Fetch curated lists and register the companies and ATS boards they reveal."""
    from app.db import session_scope
    from app.pipeline import discovery
    from app.services.preferences import load_preferences
    from app.sources.base import SourceContext
    from app.sources.http import HttpClient
    from app.sources.lists.github_lists import GithubInternshipLists

    source = GithubInternshipLists()
    async with HttpClient() as http:
        outcome = await source.run(SourceContext(http=http, queries=[]))

    if not outcome.jobs:
        raise RuntimeError(outcome.error or "curated lists returned no listings")

    with session_scope() as session:
        prefs = load_preferences(session)
        preferred = {discovery.slugify_company(c) for c in prefs.companies.preferred}
        blacklisted = {discovery.slugify_company(c) for c in prefs.companies.blacklisted}
        companies = discovery.register_companies(
            session, outcome.jobs, preferred=preferred, blacklisted=blacklisted
        )
        harvested = discovery.harvest_boards(outcome.jobs)
        boards = discovery.register_boards(session, harvested)
        summary = discovery.discovery_summary(session)

    return {
        "listings": outcome.job_count,
        "new_companies": companies,
        "boards_found": len(harvested),
        "new_boards": boards,
        "total_companies": summary["companies"],
        "total_boards": summary["boards"],
    }


async def seed_if_needed() -> None:
    """Background first-boot seed. Never raises -- a failed seed is retried next boot."""
    if not needs_seed():
        return
    log.info("bootstrap.seed_started")
    try:
        stats = await seed_registry()
    except Exception:
        log.exception("bootstrap.seed_failed")
        return
    log.info("bootstrap.seed_complete", **stats)


def schedule_seed(loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Task | None:
    """Kick off the seed without blocking startup."""
    if not needs_seed():
        return None
    return asyncio.ensure_future(seed_if_needed(), loop=loop)
