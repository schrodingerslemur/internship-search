"""The pluggable job-source interface.

Adding a source means subclassing :class:`JobSource`, implementing
:meth:`JobSource.fetch`, and registering it. Nothing else in the pipeline needs
to change: normalisation, deduplication, scoring, and reporting all operate on
the common schema.

Two flavours exist:

* :class:`QueryJobSource` -- searched with generated query strings
  (aggregators, search APIs).
* :class:`BoardJobSource` -- crawled per company board, and therefore also a
  *discovery* surface (ATS platforms).
"""

from __future__ import annotations

import abc
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.logging_setup import get_logger
from app.models.base import SourceKind
from app.schemas.job import RawJob, SourceOutcome
from app.sources.http import FetchError, HttpClient

log = get_logger("source")


@dataclass
class SearchQuery:
    """One generated search request."""

    text: str
    location: str | None = None
    remote: bool | None = None
    max_results: int = 100
    #: Extra hints a source may honour (e.g. employment type filters).
    hints: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.text.lower().strip()}|{(self.location or '').lower().strip()}"


@dataclass
class SourceContext:
    """Everything a source needs for one run."""

    http: HttpClient
    queries: list[SearchQuery]
    #: Board targets, for ATS sources: dicts of provider/board_token/extra.
    boards: list[dict[str, Any]] = field(default_factory=list)
    credentials: dict[str, str | None] = field(default_factory=dict)
    max_results_per_query: int = 100
    #: Cap on boards crawled this run, so a huge registry degrades gracefully.
    max_boards: int = 400
    #: Optional cheap title gate, supplied by the runner from user settings.
    #: Board sources consult it before materialising a listing, because a
    #: single employer board can return hundreds of irrelevant roles whose
    #: full descriptions would otherwise be held in memory for nothing.
    #: Policy lives in configuration; sources only ask.
    title_gate: Callable[[str], bool] | None = None

    def keep_title(self, title: str | None) -> bool:
        """Whether a listing with this title is worth materialising."""
        if self.title_gate is None:
            return True
        try:
            return self.title_gate(title or "")
        except Exception:
            return True


class JobSource(abc.ABC):
    """Base class for all job sources."""

    #: Stable machine name, used in the DB and coverage reports.
    name: str = "unnamed"
    #: Human-facing label for the dashboard.
    display_name: str = "Unnamed source"
    kind: SourceKind = SourceKind.UNKNOWN
    #: Credential keys that must be present for this source to run.
    required_credentials: tuple[str, ...] = ()
    #: Documented limitation shown in the source-health panel.
    notes: str = ""
    #: Whether this source can reveal new companies/boards for later crawling.
    is_discovery_source: bool = False
    enabled_by_default: bool = True

    def is_configured(self, credentials: dict[str, str | None]) -> bool:
        return all(credentials.get(key) for key in self.required_credentials)

    @abc.abstractmethod
    async def fetch(self, ctx: SourceContext) -> list[RawJob]:
        """Retrieve listings. May raise; :meth:`run` handles failures."""

    async def run(self, ctx: SourceContext) -> SourceOutcome:
        """Execute the source, capturing timing, errors, and stats.

        Never raises: a failing source degrades the run, it does not end it.
        """
        started = time.monotonic()
        outcome = SourceOutcome(source=self.name, kind=self.kind)

        if not self.is_configured(ctx.credentials):
            outcome.status = "unconfigured"
            outcome.error = (
                f"missing credentials: {', '.join(self.required_credentials)}"
            )
            outcome.duration_seconds = time.monotonic() - started
            log.info("source.unconfigured", source=self.name)
            return outcome

        try:
            jobs = await self.fetch(ctx)
        except FetchError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)[:500]
            log.warning("source.failed", source=self.name, error=str(exc)[:200])
        except Exception as exc:  # pragma: no cover - defensive
            outcome.status = "failed"
            outcome.error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("source.crashed", source=self.name)
        else:
            outcome.jobs = jobs
            outcome.status = "ok" if jobs or not self._expects_results(ctx) else "ok"
            log.info("source.ok", source=self.name, jobs=len(jobs))

        outcome.duration_seconds = time.monotonic() - started
        return outcome

    def _expects_results(self, ctx: SourceContext) -> bool:
        return bool(ctx.queries or ctx.boards)

    # -- helpers for subclasses --

    def make_job(self, **kwargs: Any) -> RawJob:
        kwargs.setdefault("source", self.name)
        kwargs.setdefault("source_kind", self.kind)
        return RawJob(**kwargs)


class QueryJobSource(JobSource):
    """A source searched with generated query strings."""

    #: Upper bound on queries issued per run, to bound cost and rate limits.
    max_queries: int = 12

    @abc.abstractmethod
    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[RawJob]:
        """Run one query."""

    async def fetch(self, ctx: SourceContext) -> list[RawJob]:
        queries = ctx.queries[: self.max_queries]
        if not queries:
            return []
        results = await ctx.http.gather([self.search(q, ctx) for q in queries])
        jobs: list[RawJob] = []
        failures = 0
        for item in results:
            if isinstance(item, Exception):
                failures += 1
                log.debug("source.query_failed", source=self.name, error=str(item)[:150])
                continue
            jobs.extend(item)
        if failures and failures == len(queries):
            raise FetchError(f"all {failures} queries failed")
        return jobs


class BoardJobSource(JobSource):
    """A source crawled per company board (an ATS platform).

    These are the discovery engine: boards are harvested from URLs seen
    anywhere in the pipeline, then crawled directly here.
    """

    is_discovery_source = True
    #: Matches ``AtsIdentity.provider`` for boards routed to this source.
    provider: str = ""

    @abc.abstractmethod
    async def fetch_board(self, board: dict[str, Any], ctx: SourceContext) -> list[RawJob]:
        """Crawl a single company board."""

    def select_boards(self, ctx: SourceContext) -> list[dict[str, Any]]:
        return [b for b in ctx.boards if b.get("provider") == self.provider][: ctx.max_boards]

    async def fetch(self, ctx: SourceContext) -> list[RawJob]:
        boards = self.select_boards(ctx)
        if not boards:
            return []
        results = await ctx.http.gather([self.fetch_board(b, ctx) for b in boards])
        jobs: list[RawJob] = []
        succeeded = 0
        for board, item in zip(boards, results, strict=False):
            if isinstance(item, Exception):
                log.debug(
                    "board.failed",
                    source=self.name,
                    board=board.get("board_token"),
                    error=str(item)[:150],
                )
                continue
            succeeded += 1
            jobs.extend(item)
        self._last_board_stats = (len(boards), succeeded)
        if succeeded == 0 and boards:
            raise FetchError(f"all {len(boards)} boards failed")
        return jobs

    async def run(self, ctx: SourceContext) -> SourceOutcome:
        self._last_board_stats = (0, 0)
        outcome = await super().run(ctx)
        attempted, successful = getattr(self, "_last_board_stats", (0, 0))
        outcome.sub_targets_attempted = attempted
        outcome.sub_targets_successful = successful
        if attempted and successful < attempted and outcome.status == "ok":
            outcome.status = "degraded"
            outcome.error = f"{successful}/{attempted} boards succeeded"
        return outcome
