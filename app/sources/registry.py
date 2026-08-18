"""Registry of all available job sources.

Adding a source is a two-line change: implement :class:`~app.sources.base.JobSource`
and append it to :data:`ALL_SOURCE_CLASSES`.
"""

from __future__ import annotations

from app.sources.ats.ashby import AshbySource
from app.sources.ats.greenhouse import GreenhouseSource
from app.sources.ats.lever import LeverSource
from app.sources.ats.misc_ats import (
    RecruiteeSource,
    SmartRecruitersSource,
    WorkableSource,
)
from app.sources.ats.workday import WorkdaySource
from app.sources.base import BoardJobSource, JobSource
from app.sources.boards.credentialed import (
    AdzunaSource,
    JoobleSource,
    JSearchSource,
    SerpApiGoogleJobsSource,
    USAJobsSource,
)
from app.sources.boards.free_apis import ArbeitnowSource, RemotiveSource, TheMuseSource
from app.sources.lists.github_lists import GithubInternshipLists
from app.sources.lists.hackernews import HackerNewsWhoIsHiring

#: Every source the system knows how to run.
ALL_SOURCE_CLASSES: tuple[type[JobSource], ...] = (
    # Curated lists first: they seed the ATS registry for the board crawlers.
    GithubInternshipLists,
    # ATS platforms -- the backbone of coverage and discovery.
    GreenhouseSource,
    LeverSource,
    AshbySource,
    SmartRecruitersSource,
    WorkableSource,
    RecruiteeSource,
    WorkdaySource,
    # Free public boards.
    TheMuseSource,
    RemotiveSource,
    ArbeitnowSource,
    HackerNewsWhoIsHiring,
    # Credentialed aggregators.
    AdzunaSource,
    JSearchSource,
    USAJobsSource,
    JoobleSource,
    SerpApiGoogleJobsSource,
)


def build_sources(disabled: set[str] | None = None) -> list[JobSource]:
    """Instantiate every enabled source."""
    disabled = disabled or set()
    return [cls() for cls in ALL_SOURCE_CLASSES if cls.name not in disabled]


def board_providers() -> dict[str, str]:
    """Map ATS provider -> source name, for routing discovered boards."""
    mapping: dict[str, str] = {}
    for cls in ALL_SOURCE_CLASSES:
        if issubclass(cls, BoardJobSource) and cls.provider:
            mapping[cls.provider] = cls.name
    return mapping


def source_catalog() -> list[dict[str, object]]:
    """Metadata for the settings and source-health pages."""
    return [
        {
            "name": cls.name,
            "display_name": cls.display_name,
            "kind": str(cls.kind),
            "requires_credentials": bool(cls.required_credentials),
            "required_credentials": list(cls.required_credentials),
            "notes": cls.notes,
            "is_discovery_source": cls.is_discovery_source,
            "is_board_source": issubclass(cls, BoardJobSource),
        }
        for cls in ALL_SOURCE_CLASSES
    ]
