"""End-to-end search pipeline orchestration.

    collect -> normalize -> deduplicate -> score -> persist -> notify

Every stage records what actually happened, so the coverage dashboard reports
measured numbers rather than claims. A source that fails is logged and skipped;
the run continues.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import AtsBoard, JobSourceRecord, SearchRun, SearchRunSource
from app.models.base import NotificationKind, Priority, RunStatus, SourceHealth, utcnow
from app.pipeline import discovery
from app.pipeline.dedupe import DedupResult, deduplicate
from app.pipeline.llm import LlmClient, profile_summary_text
from app.pipeline.match import MatchResult, score_job
from app.pipeline.normalize import normalize_all
from app.pipeline.prefilter import build_title_gate, prefilter
from app.pipeline.queries import generate_queries
from app.schemas.job import NormalizedJob, RawJob, SourceOutcome
from app.schemas.preferences import SearchPreferences
from app.schemas.profile import CandidateProfileData
from app.services.persistence import expire_stale_jobs, persist_clusters
from app.services.preferences import load_preferences, load_profile
from app.sources.base import SourceContext
from app.sources.http import HttpClient
from app.sources.registry import build_sources

log = get_logger("runner")


@dataclass
class RunReport:
    """Everything measured during one pipeline execution."""

    run_id: int | None = None
    status: str = RunStatus.RUNNING.value
    started_at: datetime | None = None
    duration_seconds: float = 0.0
    queries_generated: int = 0
    sources_attempted: int = 0
    sources_successful: int = 0
    sources_failed: int = 0
    sources_unconfigured: int = 0
    raw_jobs_found: int = 0
    jobs_normalized: int = 0
    jobs_dropped: int = 0
    prefiltered_out: int = 0
    duplicates_removed: int = 0
    unique_jobs: int = 0
    relevant_jobs: int = 0
    high_priority_jobs: int = 0
    new_jobs: int = 0
    updated_jobs: int = 0
    reposted_jobs: int = 0
    expired_jobs: int = 0
    companies_discovered: int = 0
    boards_discovered: int = 0
    boards_crawled: int = 0
    llm_calls: int = 0
    notifications_sent: int = 0
    errors: list[str] = field(default_factory=list)
    outcomes: list[SourceOutcome] = field(default_factory=list)
    new_job_ids: list[int] = field(default_factory=list)
    updated_job_ids: list[int] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.sources_successful}/{self.sources_attempted} sources · "
            f"{self.raw_jobs_found} raw · {self.unique_jobs} unique · "
            f"{self.relevant_jobs} relevant · {self.new_jobs} new"
        )


async def collect(
    prefs: SearchPreferences, boards: list[dict], credentials_map: dict[str, dict]
) -> list[SourceOutcome]:
    """Run every enabled source concurrently."""
    queries = generate_queries(prefs)
    sources = build_sources(disabled=set(prefs.scope.disabled_sources))
    title_gate = build_title_gate(prefs)

    async with HttpClient() as http:
        contexts = []
        for source in sources:
            contexts.append(
                (
                    source,
                    SourceContext(
                        http=http,
                        queries=queries,
                        boards=boards,
                        credentials=credentials_map.get(source.name, {}),
                        max_boards=prefs.scope.max_ats_boards_per_run,
                        title_gate=title_gate,
                    ),
                )
            )
        results = await http.gather([source.run(ctx) for source, ctx in contexts])

    outcomes: list[SourceOutcome] = []
    for (source, _), result in zip(contexts, results, strict=False):
        if isinstance(result, Exception):
            outcomes.append(
                SourceOutcome(
                    source=source.name,
                    kind=source.kind,
                    status="failed",
                    error=f"{type(result).__name__}: {result}"[:400],
                )
            )
        else:
            outcomes.append(result)
    return outcomes


def score_all(
    jobs: list[NormalizedJob],
    prefs: SearchPreferences,
    profile: CandidateProfileData,
    *,
    llm: LlmClient | None = None,
    now: datetime | None = None,
) -> dict[str, MatchResult]:
    """Score every cluster representative, optionally adding an LLM opinion."""
    now = now or utcnow()
    results: dict[str, MatchResult] = {}
    summary = profile_summary_text(profile) if llm and llm.enabled else ""

    # Only the strongest deterministic candidates are worth an LLM call.
    ranked: list[tuple[float, NormalizedJob]] = []
    for job in jobs:
        result = score_job(job, prefs, profile, now=now)
        results[job.key] = result
        ranked.append((result.score, job))

    if llm and llm.enabled and prefs.scope.llm_semantic_matching:
        ranked.sort(key=lambda pair: -pair[0])
        for score, job in ranked:
            if llm.budget_left <= 0:
                break
            if score < prefs.thresholds.maybe:
                break
            assessment = llm.classify_relevance(job, summary)
            if not assessment:
                continue
            result = results[job.key]
            # Blend, rather than replace: the deterministic score stays
            # auditable and the LLM nudges it.
            blended = 0.75 * result.score + 0.25 * (assessment["relevance"] * 100)
            result.score = round(max(0.0, min(100.0, blended)), 1)
            from app.pipeline.match import classify_priority

            result.priority = classify_priority(result.score, prefs)
            if assessment.get("reason"):
                result.match_reasons.append(f"AI: {assessment['reason']}")
    return results


async def run_search(
    *,
    trigger: str = "manual",
    notify: bool = True,
    notification_kind: NotificationKind = NotificationKind.MORNING_DIGEST,
    dry_run: bool = False,
) -> RunReport:
    """Execute the complete pipeline."""
    settings = get_settings()
    report = RunReport(started_at=utcnow())
    started = time.monotonic()

    # ---- setup ----
    with session_scope() as session:
        prefs = load_preferences(session)
        profile = load_profile(session)
        run = SearchRun(trigger=trigger, status=RunStatus.RUNNING.value, started_at=report.started_at)
        session.add(run)
        session.flush()
        report.run_id = run.id

        boards = discovery.select_boards_to_crawl(session, prefs.scope.max_ats_boards_per_run)
        # Seed plausible boards for preferred companies that have none yet.
        boards.extend(discovery.seed_boards_for_companies(session, prefs.companies.preferred))
        report.boards_crawled = len(boards)

    credentials_map = settings.source_credentials()
    queries = generate_queries(prefs)
    report.queries_generated = len(queries)
    log.info("run.start", run_id=report.run_id, queries=len(queries), boards=len(boards))

    # ---- collect ----
    outcomes = await collect(prefs, boards, credentials_map)
    report.outcomes = outcomes
    report.sources_attempted = len(outcomes)
    report.sources_successful = sum(1 for o in outcomes if o.status in ("ok", "degraded"))
    report.sources_failed = sum(1 for o in outcomes if o.status == "failed")
    report.sources_unconfigured = sum(1 for o in outcomes if o.status == "unconfigured")

    raw_jobs: list[RawJob] = []
    for outcome in outcomes:
        raw_jobs.extend(outcome.jobs)
        if outcome.error and outcome.status == "failed":
            report.errors.append(f"{outcome.source}: {outcome.error}")
    report.raw_jobs_found = len(raw_jobs)
    log.info("run.collected", raw=len(raw_jobs))

    # ---- prefilter ----
    # Company/board discovery below deliberately runs on the *unfiltered* set,
    # so an unknown employer still enters the registry even when none of its
    # current openings are relevant.
    candidates, prefilter_stats = prefilter(raw_jobs, prefs)
    report.prefiltered_out = prefilter_stats.dropped
    log.info(
        "run.prefiltered",
        kept=prefilter_stats.kept,
        dropped=prefilter_stats.dropped,
    )

    # ---- normalize ----
    normalized, dropped = normalize_all(candidates)
    report.jobs_normalized = len(normalized)
    report.jobs_dropped = dropped

    # ---- deduplicate ----
    llm = LlmClient()
    adjudicator = None
    if llm.enabled and prefs.scope.llm_dedup_adjudication:
        adjudicator = llm.adjudicate_duplicate
    dedup: DedupResult = deduplicate(
        normalized,
        llm_adjudicator=adjudicator,
        max_llm_calls=llm.budget_left if adjudicator else 0,
    )
    report.duplicates_removed = dedup.duplicates_removed
    report.unique_jobs = len(dedup.clusters)
    log.info("run.deduplicated", unique=report.unique_jobs, removed=report.duplicates_removed)

    # ---- score ----
    from app.pipeline.dedupe import merge_cluster_facts

    representatives = [merge_cluster_facts(cluster) for cluster in dedup.clusters]
    scores = score_all(representatives, prefs, profile, llm=llm)
    report.llm_calls = llm.calls

    report.relevant_jobs = sum(
        1 for r in scores.values() if not r.excluded and r.score >= prefs.thresholds.maybe
    )
    report.high_priority_jobs = sum(
        1
        for r in scores.values()
        if r.priority in (Priority.APPLY_NOW, Priority.STRONG_MATCH)
    )

    # ---- persist ----
    with session_scope() as session:
        preferred = {discovery.slugify_company(c) for c in prefs.companies.preferred}
        blacklisted = {discovery.slugify_company(c) for c in prefs.companies.blacklisted}
        report.companies_discovered = discovery.register_companies(
            session, raw_jobs, preferred=preferred, blacklisted=blacklisted
        )
        harvested = discovery.harvest_boards(raw_jobs)
        report.boards_discovered = discovery.register_boards(session, harvested)

        failed_board_ids = _failed_board_ids(outcomes, boards)
        discovery.record_board_results(session, boards, {}, failed_board_ids)
        discovery.prune_failed_boards(session)

        persisted = persist_clusters(
            session,
            dedup.clusters,
            scores,
            run_id=report.run_id,
            min_score_to_store=prefs.scope.min_score_to_store,
        )
        report.new_jobs = persisted.new_jobs
        report.updated_jobs = persisted.updated_jobs
        report.reposted_jobs = persisted.reposted_jobs
        report.new_job_ids = persisted.new_job_ids
        report.updated_job_ids = persisted.updated_job_ids
        report.expired_jobs = expire_stale_jobs(session, run_id=report.run_id)

        _record_run(session, report, outcomes)
        _update_source_records(session, outcomes)

    log.info("run.persisted", new=report.new_jobs, updated=report.updated_jobs)

    # ---- notify ----
    if notify:
        with session_scope() as session:
            prefs_now = load_preferences(session)
            from app.notify.engine import send_digest

            base_url = f"http://{settings.app_host}:{settings.app_port}"
            notification, result = await send_digest(
                session,
                prefs_now.notifications,
                notification_kind,
                run_id=report.run_id,
                base_url=base_url,
                stats={"new_jobs": report.new_jobs},
                now=utcnow(),
                dry_run=dry_run,
            )
            if notification is not None and result is not None and result.ok:
                report.notifications_sent = 1

    report.duration_seconds = time.monotonic() - started
    report.status = (
        RunStatus.SUCCESS.value
        if report.sources_failed == 0
        else (RunStatus.PARTIAL.value if report.sources_successful else RunStatus.FAILED.value)
    )

    with session_scope() as session:
        run = session.get(SearchRun, report.run_id)
        if run is not None:
            run.status = report.status
            run.finished_at = utcnow()
            run.duration_seconds = report.duration_seconds
            run.notifications_sent = report.notifications_sent

    log.info("run.finished", run_id=report.run_id, summary=report.summary())
    return report


def _failed_board_ids(outcomes: list[SourceOutcome], boards: list[dict]) -> set[int]:
    """Boards belonging to sources that failed outright."""
    failed_sources = {o.source for o in outcomes if o.status == "failed"}
    if not failed_sources:
        return set()
    from app.sources.registry import board_providers

    provider_to_source = board_providers()
    return {
        board["id"]
        for board in boards
        if board.get("id") and provider_to_source.get(board["provider"]) in failed_sources
    }


def _record_run(session: Session, report: RunReport, outcomes: list[SourceOutcome]) -> None:
    run = session.get(SearchRun, report.run_id)
    if run is None:
        return
    run.queries_generated = report.queries_generated
    run.sources_attempted = report.sources_attempted
    run.sources_successful = report.sources_successful
    run.sources_failed = report.sources_failed
    run.sources_unconfigured = report.sources_unconfigured
    run.raw_jobs_found = report.raw_jobs_found
    run.jobs_normalized = report.jobs_normalized
    run.duplicates_removed = report.duplicates_removed
    run.unique_jobs = report.unique_jobs
    run.relevant_jobs = report.relevant_jobs
    run.high_priority_jobs = report.high_priority_jobs
    run.new_jobs = report.new_jobs
    run.updated_jobs = report.updated_jobs
    run.reposted_jobs = report.reposted_jobs
    run.expired_jobs = report.expired_jobs
    run.companies_discovered = report.companies_discovered
    run.ats_boards_discovered = report.boards_discovered
    run.llm_calls = report.llm_calls
    run.errors = report.errors[:50]

    for outcome in outcomes:
        session.add(
            SearchRunSource(
                run_id=run.id,
                source=outcome.source,
                kind=str(outcome.kind),
                status=outcome.status,
                jobs_returned=outcome.job_count,
                queries_run=outcome.queries_run,
                sub_targets_attempted=outcome.sub_targets_attempted,
                sub_targets_successful=outcome.sub_targets_successful,
                duration_seconds=round(outcome.duration_seconds, 2),
                error=outcome.error,
            )
        )
    session.flush()


def _update_source_records(session: Session, outcomes: list[SourceOutcome]) -> None:
    """Maintain the source registry: health plus rolling adaptive statistics."""
    from app.sources.registry import source_catalog

    catalog = {entry["name"]: entry for entry in source_catalog()}
    now = utcnow()

    for outcome in outcomes:
        row = session.scalar(select(JobSourceRecord).where(JobSourceRecord.name == outcome.source))
        meta = catalog.get(outcome.source, {})
        if row is None:
            row = JobSourceRecord(
                name=outcome.source,
                display_name=str(meta.get("display_name", outcome.source)),
                kind=str(outcome.kind),
                requires_credentials=bool(meta.get("requires_credentials", False)),
                notes=str(meta.get("notes", "")),
            )
            session.add(row)

        row.last_run_at = now
        row.total_runs = (row.total_runs or 0) + 1
        row.total_jobs_returned = (row.total_jobs_returned or 0) + outcome.job_count
        previous_avg = row.avg_duration_seconds or 0.0
        row.avg_duration_seconds = round(
            (previous_avg * (row.total_runs - 1) + outcome.duration_seconds) / row.total_runs, 2
        )

        if outcome.status == "ok":
            row.health = SourceHealth.OK.value
            row.last_success_at = now
            row.consecutive_failures = 0
            row.last_error = None
        elif outcome.status == "degraded":
            row.health = SourceHealth.DEGRADED.value
            row.last_success_at = now
            row.consecutive_failures = 0
            row.last_error = outcome.error
        elif outcome.status == "unconfigured":
            row.health = SourceHealth.UNCONFIGURED.value
            row.last_error = outcome.error
        else:
            row.health = SourceHealth.FAILED.value
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_error = outcome.error
    session.flush()


def board_count(session: Session) -> int:
    from sqlalchemy import func

    return session.scalar(select(func.count(AtsBoard.id))) or 0
