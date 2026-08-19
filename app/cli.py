"""Command-line entry points.

    python -m app.cli search          run the full pipeline once
    python -m app.cli search --no-notify --dry-run
    python -m app.cli serve           start the web dashboard + scheduler
    python -m app.cli seed            seed the ATS registry from curated lists
    python -m app.cli notify-test     verify notification delivery
    python -m app.cli stats           print current database stats
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from app.config import ensure_dirs, get_settings
from app.logging_setup import configure_logging, get_logger

log = get_logger("cli")


def _setup() -> None:
    settings = get_settings()
    ensure_dirs()
    configure_logging(settings.log_level, settings.log_format)


def _resolve_kind(value: str):
    """Which digest this run is, for the notification record.

    ``auto`` reads the clock in the configured timezone, so an externally
    scheduled run (a cron job, a CI workflow) is labelled correctly without
    the caller having to know the schedule.
    """
    from zoneinfo import ZoneInfo

    from app.models.base import NotificationKind

    if value == "morning":
        return NotificationKind.MORNING_DIGEST
    if value == "afternoon":
        return NotificationKind.AFTERNOON_DIGEST

    try:
        local_hour = datetime.now(ZoneInfo(get_settings().timezone)).hour
    except Exception:
        local_hour = datetime.now().hour
    return NotificationKind.MORNING_DIGEST if local_hour < 12 else NotificationKind.AFTERNOON_DIGEST


async def _cmd_search(args: argparse.Namespace) -> int:
    from app.pipeline.runner import run_search

    report = await run_search(
        trigger=args.trigger,
        notify=not args.no_notify,
        notification_kind=_resolve_kind(args.kind),
        dry_run=args.dry_run,
    )

    print("\n" + "=" * 62)
    print(f"  SEARCH RUN #{report.run_id}  [{report.status}]  {report.duration_seconds:.1f}s")
    print("=" * 62)
    print(f"  Queries generated      {report.queries_generated}")
    print(
        f"  Sources                {report.sources_successful} ok / "
        f"{report.sources_failed} failed / {report.sources_unconfigured} unconfigured"
    )
    print(f"  Raw listings           {report.raw_jobs_found}")
    print(f"  Normalized             {report.jobs_normalized}  (dropped {report.jobs_dropped})")
    print(f"  Duplicates removed     {report.duplicates_removed}")
    print(f"  Unique canonical jobs  {report.unique_jobs}")
    print(f"  Relevant               {report.relevant_jobs}")
    print(f"  High priority          {report.high_priority_jobs}")
    print(f"  New / updated          {report.new_jobs} / {report.updated_jobs}")
    print(f"  Companies discovered   {report.companies_discovered}")
    print(f"  ATS boards discovered  {report.boards_discovered}")
    print(f"  Notifications sent     {report.notifications_sent}")
    print("-" * 62)
    print("  PER-SOURCE COVERAGE")
    for outcome in sorted(report.outcomes, key=lambda o: -o.job_count):
        mark = {
            "ok": "OK  ",
            "degraded": "WARN",
            "failed": "FAIL",
            "unconfigured": "----",
        }.get(outcome.status, "?   ")
        detail = ""
        if outcome.sub_targets_attempted:
            detail = f" ({outcome.sub_targets_successful}/{outcome.sub_targets_attempted} boards)"
        print(f"   [{mark}] {outcome.source:22s} {outcome.job_count:6d}{detail}")
        if outcome.error:
            print(f"           -> {outcome.error[:90]}")
    print("=" * 62 + "\n")
    return 0 if report.status in ("success", "partial") else 1


async def _cmd_seed(args: argparse.Namespace) -> int:
    """Populate the ATS registry without running the whole pipeline."""
    from app.services.bootstrap import seed_registry

    try:
        stats = await seed_registry()
    except Exception as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {stats['listings']} listings from curated lists")
    print(f"  companies registered : {stats['new_companies']} new")
    print(f"  ATS boards harvested : {stats['boards_found']} found, {stats['new_boards']} newly registered")
    print(f"  registry totals      : {stats['total_companies']} companies, {stats['total_boards']} boards")
    return 0


async def _cmd_notify_test(args: argparse.Namespace) -> int:
    from app.db import session_scope
    from app.notify.engine import send_test_notification

    with session_scope() as session:
        result = await send_test_notification(session, args.provider)
    if result.provider != args.provider:
        # Silently degrading is right for a scheduled digest, but a test that
        # went somewhere else must say so, or the user thinks email works.
        print(
            f"WARNING: '{args.provider}' is not configured; fell back to "
            f"'{result.provider}'. Check its credentials in .env.",
            file=sys.stderr,
        )
    if result.ok:
        print(f"Test notification sent via {result.provider}. {result.detail or ''}".strip())
        return 0 if result.provider == args.provider else 1
    print(f"Failed to send via {result.provider}: {result.error}", file=sys.stderr)
    return 1


def _cmd_stats(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import AtsBoard, Company, Job, JobListing, SearchRun

    with session_scope() as session:
        jobs = session.scalar(select(func.count(Job.id))) or 0
        listings = session.scalar(select(func.count(JobListing.id))) or 0
        companies = session.scalar(select(func.count(Company.id))) or 0
        boards = session.scalar(select(func.count(AtsBoard.id))) or 0
        runs = session.scalar(select(func.count(SearchRun.id))) or 0
        by_priority = dict(
            session.execute(select(Job.priority, func.count(Job.id)).group_by(Job.priority)).all()
        )
        multi = (
            session.scalar(
                select(func.count()).select_from(
                    select(JobListing.job_id)
                    .group_by(JobListing.job_id)
                    .having(func.count(JobListing.id) > 1)
                    .subquery()
                )
            )
            or 0
        )

    print(f"Canonical jobs      {jobs}")
    print(f"Source listings     {listings}")
    print(f"  ...jobs found on >1 source: {multi}")
    print(f"Companies known     {companies}")
    print(f"ATS boards known    {boards}")
    print(f"Search runs         {runs}")
    print("By priority:")
    for name, count in sorted(by_priority.items(), key=lambda kv: -kv[1]):
        print(f"  {name:20s} {count}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=args.host or settings.app_host,
        port=args.port or settings.app_port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="internship-search", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="run the search pipeline once")
    p_search.add_argument("--no-notify", action="store_true", help="skip notifications")
    p_search.add_argument("--dry-run", action="store_true", help="build but do not send digests")
    p_search.add_argument("--trigger", default="manual")
    p_search.add_argument(
        "--kind",
        choices=("auto", "morning", "afternoon"),
        default="auto",
        help="which digest this run is; 'auto' decides from the local clock",
    )

    sub.add_parser("seed", help="seed companies and ATS boards from curated lists")

    p_notify = sub.add_parser("notify-test", help="send a test notification")
    p_notify.add_argument("--provider", default="telegram")

    sub.add_parser("stats", help="print database statistics")

    p_serve = sub.add_parser("serve", help="run the web dashboard")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)
    _setup()

    if args.command == "search":
        return asyncio.run(_cmd_search(args))
    if args.command == "seed":
        return asyncio.run(_cmd_seed(args))
    if args.command == "notify-test":
        return asyncio.run(_cmd_notify_test(args))
    if args.command == "stats":
        return _cmd_stats(args)
    if args.command == "serve":
        return _cmd_serve(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
