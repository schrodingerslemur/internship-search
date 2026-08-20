"""Company profiles and the self-expanding ATS board registry."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Company(Base, TimestampMixin):
    """A company discovered anywhere in the pipeline.

    Companies are created automatically on discovery -- the user never has to
    enter one first. `is_preferred` / `is_blacklisted` are *preferences*, not
    search boundaries.
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Normalised slug used for matching, e.g. "advanced micro devices" -> "amd".
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    website: Mapped[str | None] = mapped_column(String(500))
    careers_url: Mapped[str | None] = mapped_column(String(500))
    industry: Mapped[str | None] = mapped_column(String(120))
    #: Free-form tags: semiconductor, quant_trading, robotics, defense, ...
    tags: Mapped[list] = mapped_column(JSON, default=list)
    size_hint: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)

    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    #: Extra ranking points, positive or negative, applied on top of preference.
    preference_boost: Mapped[float] = mapped_column(Float, default=0.0)
    #: True when the user explicitly asked to monitor this company's careers page.
    is_monitored: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: How the company entered the system: user, aggregator, curated_list, ats_crawl...
    discovered_via: Mapped[str | None] = mapped_column(String(80))
    jobs_seen_count: Mapped[int] = mapped_column(Integer, default=0)

    ats_boards: Mapped[list[AtsBoard]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class AtsBoard(Base, TimestampMixin):
    """A crawlable ATS job board belonging to a company.

    This table is the engine of company discovery: board identities are
    harvested by regex from *any* posting URL the pipeline encounters
    (aggregator apply-links, curated lists, HN posts), then crawled directly
    on subsequent runs -- surfacing jobs the original aggregator never indexed.
    """

    __tablename__ = "ats_boards"
    __table_args__ = (
        Index("ix_ats_boards_provider_token", "provider", "board_token", unique=True),
        Index("ix_ats_boards_crawl", "enabled", "last_crawled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    #: greenhouse | lever | ashby | smartrecruiters | workable | workday | icims ...
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    #: The board identifier within that provider (e.g. greenhouse board token).
    board_token: Mapped[str] = mapped_column(String(400), nullable=False)
    #: Provider-specific extras, e.g. Workday {"host": "nvidia.wd5...", "site": "..."}.
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    discovered_via: Mapped[str | None] = mapped_column(String(80))
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    jobs_last_crawl: Mapped[int] = mapped_column(Integer, default=0)
    #: Rolling count of jobs from this board that scored as relevant.
    relevant_jobs_total: Mapped[int] = mapped_column(Integer, default=0)

    company: Mapped[Company | None] = relationship(back_populates="ats_boards")

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.board_token}"
