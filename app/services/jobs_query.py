"""Query helpers for the dashboard: filtering, faceting, and analytics.

The job list is always built from the canonical ``jobs`` table, never from
``job_listings``, so the user-facing list stays deduplicated no matter which
filters are applied -- including the source filter, which joins for selection
but never multiplies rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, String, case, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    Company,
    Job,
    JobListing,
    Notification,
    SearchRun,
    User,
    UserJobState,
)
from app.models.base import JobStatus, Priority, utcnow

#: The four destinations in the navigation, expressed as the set of per-user
#: statuses each one contains. A job belongs to exactly one of them at a time,
#: which is what makes "where did that job go?" answerable.
VIEWS: dict[str, frozenset[str]] = {
    # The feed. Undecided work, plus jobs kept for later -- never anything the
    # user has already finished with.
    "review": frozenset({JobStatus.NEW.value, JobStatus.SAVED.value}),
    "saved": frozenset({JobStatus.SAVED.value}),
    "applied": frozenset(
        {
            JobStatus.APPLIED.value,
            JobStatus.ASSESSMENT.value,
            JobStatus.INTERVIEW.value,
            JobStatus.OFFER.value,
            JobStatus.REJECTED.value,
        }
    ),
    "dismissed": frozenset({JobStatus.DISMISSED.value}),
}

#: Views whose whole purpose is history, so an expired or delisted posting must
#: still show: "I applied to that" does not stop being true when it closes.
HISTORY_VIEWS: frozenset[str] = frozenset({"applied", "dismissed"})

DEFAULT_VIEW = "review"


@dataclass
class JobFilters:
    """Every filter the dashboard exposes."""

    #: Which of VIEWS to show. Not a filter the user tweaks -- it is the page
    #: they are on, which is why it is kept out of the filter chips.
    view: str = DEFAULT_VIEW
    q: str | None = None
    min_score: float | None = None
    max_score: float | None = None
    company: str | None = None
    location: str | None = None
    role: str | None = None
    priority: str | None = None
    status: str | None = None
    source: str | None = None
    skill: str | None = None
    remote: str | None = None
    posted_within_days: int | None = None
    deadline_within_days: int | None = None
    has_salary: bool = False
    include_inactive: bool = False
    include_dismissed: bool = False
    sort: str = "score"
    page: int = 1
    per_page: int = 25

    @classmethod
    def from_query(cls, params: dict[str, Any]) -> JobFilters:
        def s(key: str) -> str | None:
            value = params.get(key)
            if value is None:
                return None
            value = str(value).strip()
            return value or None

        def f(key: str) -> float | None:
            raw = s(key)
            try:
                return float(raw) if raw is not None else None
            except ValueError:
                return None

        def i(key: str) -> int | None:
            raw = s(key)
            try:
                return int(raw) if raw is not None else None
            except ValueError:
                return None

        view = s("view") or DEFAULT_VIEW
        if view not in VIEWS and view != "all":
            view = DEFAULT_VIEW

        return cls(
            view=view,
            q=s("q"),
            min_score=f("min_score"),
            max_score=f("max_score"),
            company=s("company"),
            location=s("location"),
            role=s("role"),
            priority=s("priority"),
            status=s("status"),
            source=s("source"),
            skill=s("skill"),
            remote=s("remote"),
            posted_within_days=i("posted_within_days"),
            deadline_within_days=i("deadline_within_days"),
            has_salary=str(params.get("has_salary", "")).lower() in ("1", "true", "on"),
            include_inactive=str(params.get("include_inactive", "")).lower() in ("1", "true", "on"),
            include_dismissed=str(params.get("include_dismissed", "")).lower()
            in ("1", "true", "on"),
            # A history page defaults to "most recent first"; the feed defaults
            # to "best match first". Either is overridden by an explicit sort.
            sort=s("sort") or VIEW_DEFAULT_SORT.get(view, "score"),
            page=max(1, i("page") or 1),
            per_page=min(100, max(1, i("per_page") or 25)),
        )

    def to_query_string(self, **overrides: Any) -> str:
        from urllib.parse import urlencode

        data: dict[str, Any] = {}
        if self.view and self.view != DEFAULT_VIEW:
            data["view"] = self.view
        for name in (
            "q", "min_score", "max_score", "company", "location", "role", "priority",
            "status", "source", "skill", "remote", "posted_within_days",
            "deadline_within_days", "sort", "per_page",
        ):
            value = getattr(self, name)
            if value not in (None, ""):
                data[name] = value
        if self.has_salary:
            data["has_salary"] = "1"
        if self.include_inactive:
            data["include_inactive"] = "1"
        if self.include_dismissed:
            data["include_dismissed"] = "1"
        data.update({k: v for k, v in overrides.items() if v is not None})
        return urlencode(data)

    #: Filters worth showing back to the user as removable chips, with the
    #: wording used on screen. Paging and sorting are deliberately absent --
    #: they are not reasons a job is missing from the list.
    CHIP_LABELS = (
        ("q", "Search"),
        ("priority", "Priority"),
        ("company", "Company"),
        ("location", "Location"),
        ("role", "Role"),
        ("skill", "Skill"),
        ("source", "Source"),
        ("remote", "Arrangement"),
        ("status", "Status"),
        ("min_score", "Min match"),
        ("max_score", "Max match"),
        ("posted_within_days", "Posted within"),
        ("deadline_within_days", "Deadline within"),
    )

    def active_chips(self) -> list[dict[str, str]]:
        """Every filter currently narrowing the list, and how to drop it.

        A job vanishing with no visible explanation is the worst failure a
        filtered list can have, so the explanation is always on screen.
        """
        chips = []
        for name, label in self.CHIP_LABELS:
            value = getattr(self, name)
            if value in (None, ""):
                continue
            if name == "posted_within_days":
                shown = f"last {value} days"
            elif name == "deadline_within_days":
                shown = f"next {value} days"
            else:
                shown = str(value).replace("_", " ")
            chips.append({"name": name, "label": label, "value": shown,
                          "without": self.without(name)})
        if self.has_salary:
            chips.append({"name": "has_salary", "label": "Salary", "value": "listed",
                          "without": self.without("has_salary")})
        return chips

    def without(self, name: str) -> str:
        """This same query with one filter removed, as a query string."""
        from dataclasses import replace

        blank = False if name == "has_salary" else None
        return replace(self, **{name: blank, "page": 1}).to_query_string()

    @property
    def is_filtered(self) -> bool:
        return bool(self.active_chips())


def _user_status(user: User | None):
    """Correlated subquery giving this user's status for each job row.

    A left join would multiply rows when combined with the other joins here, so
    the status is fetched as a scalar instead. Jobs the user has never touched
    have no row at all, hence the COALESCE to 'new'.
    """
    if user is None:
        return None
    return (
        select(UserJobState.status)
        .where(UserJobState.job_id == Job.id, UserJobState.user_id == user.id)
        .correlate(Job)
        .scalar_subquery()
    )


def _user_column(user: User | None, column, fallback):
    """This user's value for a scored column, falling back to the shared one.

    The card shows the user's own score, so the score filter and the sort have
    to agree with it. Filtering on ``jobs.relevance_score`` while displaying the
    per-user number is exactly the "why did that job vanish?" bug the feed must
    not have.
    """
    if user is None:
        return fallback
    per_user = (
        select(column)
        .where(UserJobState.job_id == Job.id, UserJobState.user_id == user.id)
        .correlate(Job)
        .scalar_subquery()
    )
    return func.coalesce(per_user, fallback)


def effective_score(user: User | None):
    return _user_column(user, UserJobState.relevance_score, Job.relevance_score)


def effective_priority(user: User | None):
    return _user_column(user, UserJobState.priority, Job.priority)


def apply_filters(
    stmt: Select, filters: JobFilters, *, now: datetime | None = None, user: User | None = None
) -> Select:
    now = now or utcnow()

    # History views keep delisted postings: your record of having applied does
    # not disappear because the company took the ad down.
    if not filters.include_inactive and filters.view not in HISTORY_VIEWS:
        stmt = stmt.where(Job.is_active.is_(True))

    status_expr = _user_status(user)
    status_col = (
        func.coalesce(status_expr, JobStatus.NEW.value)
        if status_expr is not None
        else Job.status
    )
    score_col = effective_score(user)

    # The view decides which statuses belong on this page. `include_dismissed`
    # survives for the JSON API, where there are no pages to navigate between.
    wanted = VIEWS.get(filters.view)
    if wanted is not None and not filters.include_dismissed:
        stmt = stmt.where(status_col.in_(sorted(wanted)))
    elif not filters.include_dismissed and filters.status != JobStatus.DISMISSED.value:
        stmt = stmt.where(status_col != JobStatus.DISMISSED.value)

    if filters.q:
        like = f"%{filters.q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Job.title).like(like),
                func.lower(Job.company_name).like(like),
                func.lower(Job.description).like(like),
            )
        )
    if filters.min_score is not None:
        stmt = stmt.where(score_col >= filters.min_score)
    if filters.max_score is not None:
        stmt = stmt.where(score_col <= filters.max_score)
    if filters.company:
        stmt = stmt.where(func.lower(Job.company_name).like(f"%{filters.company.lower()}%"))
    if filters.location:
        stmt = stmt.where(func.lower(Job.location_raw).like(f"%{filters.location.lower()}%"))
    if filters.role:
        stmt = stmt.where(func.lower(Job.title).like(f"%{filters.role.lower()}%"))
    if filters.priority:
        stmt = stmt.where(effective_priority(user) == filters.priority)
    if filters.status:
        stmt = stmt.where(status_col == filters.status)
    if filters.remote:
        stmt = stmt.where(Job.remote_status == filters.remote)
    if filters.skill:
        # JSON array stored as text; a LIKE keeps this portable across SQLite
        # and PostgreSQL without a JSON-operator dialect split.
        stmt = stmt.where(
            func.lower(func.cast(Job.skills, String)).like(f'%"{filters.skill.lower()}"%')
        )
    if filters.has_salary:
        stmt = stmt.where(Job.salary_min.is_not(None))
    if filters.posted_within_days:
        stmt = stmt.where(Job.date_posted >= now - timedelta(days=filters.posted_within_days))
    if filters.deadline_within_days:
        stmt = stmt.where(
            Job.deadline.is_not(None),
            Job.deadline <= now + timedelta(days=filters.deadline_within_days),
            Job.deadline >= now,
        )
    if filters.source:
        # Selection only -- the outer query still returns one row per job.
        stmt = stmt.where(
            Job.id.in_(select(JobListing.job_id).where(JobListing.source == filters.source))
        )
    return stmt


#: Default ordering per view. A history page is a diary: the thing you did most
#: recently belongs at the top, not the thing that scores best.
VIEW_DEFAULT_SORT: dict[str, str] = {
    "saved": "saved",
    "applied": "applied",
    "dismissed": "dismissed",
}

#: sort key -> the UserJobState column it orders by.
_ACTIVITY_SORTS: dict[str, str] = {
    "saved": "saved_at",
    "applied": "applied_at",
    "dismissed": "dismissed_at",
}


def _order(stmt: Select, sort: str, user: User | None = None) -> Select:
    score = effective_score(user)

    activity = _ACTIVITY_SORTS.get(sort)
    if activity and user is not None:
        when = (
            select(getattr(UserJobState, activity))
            .where(UserJobState.job_id == Job.id, UserJobState.user_id == user.id)
            .correlate(Job)
            .scalar_subquery()
        )
        return stmt.order_by(when.desc().nullslast(), score.desc())

    if sort == "date":
        return stmt.order_by(Job.date_posted.desc().nullslast(), score.desc())
    if sort == "deadline":
        return stmt.order_by(Job.deadline.asc().nullslast(), score.desc())
    if sort == "company":
        return stmt.order_by(Job.company_name.asc(), score.desc())
    if sort == "discovered":
        return stmt.order_by(Job.date_discovered.desc().nullslast())
    return stmt.order_by(score.desc(), Job.date_posted.desc().nullslast())


@dataclass
class JobPage:
    jobs: list[Job] = field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 25

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def search_jobs(session: Session, filters: JobFilters, user: User | None = None) -> JobPage:
    base = apply_filters(select(Job), filters, user=user)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = _order(base, filters.sort, user)
    stmt = stmt.offset((filters.page - 1) * filters.per_page).limit(filters.per_page)
    jobs = list(session.scalars(stmt).unique().all())
    return JobPage(jobs=jobs, total=total, page=filters.page, per_page=filters.per_page)


def count_jobs(session: Session, filters: JobFilters, user: User | None = None) -> int:
    """How many jobs match, without fetching a page of them.

    An action needs this to keep "showing 12 of 34" honest, and nothing else
    from the query -- so it does not pay for the rows.
    """
    base = apply_filters(select(Job), filters, user=user)
    return session.scalar(select(func.count()).select_from(base.subquery())) or 0


def dashboard_counts(session: Session, user: User | None = None) -> dict[str, int]:
    """Headline counters for the dashboard header.

    Every number here answers a question the user actually has -- how much is
    left to review, what is urgent, where did the rest go -- and each one is a
    link to the list it counts. A counter you cannot act on is decoration.
    """
    now = utcnow()
    since_yesterday = now - timedelta(days=1)

    def count(*conditions) -> int:
        return session.scalar(select(func.count(Job.id)).where(*conditions)) or 0

    active = Job.is_active.is_(True)
    status_expr = _user_status(user)
    status_col = (
        func.coalesce(status_expr, JobStatus.NEW.value)
        if status_expr is not None
        else Job.status
    )
    priority_col = effective_priority(user)

    in_review = status_col.in_(sorted(VIEWS["review"]))
    unreviewed = status_col == JobStatus.NEW.value

    return {
        # The feed, and the two slices of it that deserve attention first.
        "to_review": count(active, in_review),
        "new": count(active, unreviewed),
        "apply_now": count(active, in_review, priority_col == Priority.APPLY_NOW.value),
        "strong": count(active, in_review, priority_col == Priority.STRONG_MATCH.value),
        # "Since yesterday" beats "today": at 09:00 a calendar-day counter reads
        # zero however good last night's run was.
        "new_since_yesterday": count(active, unreviewed, Job.date_discovered >= since_yesterday),
        # Where everything else went. Not filtered by is_active -- history does
        # not shrink when a posting closes.
        "saved": count(status_col == JobStatus.SAVED.value),
        "applied": count(status_col.in_(sorted(VIEWS["applied"]))),
        "dismissed": count(status_col == JobStatus.DISMISSED.value),
        "interviews": count(status_col == JobStatus.INTERVIEW.value),
        "offers": count(status_col == JobStatus.OFFER.value),
        "total_active": count(active, status_col != JobStatus.DISMISSED.value),
    }


def facet_values(session: Session, limit: int = 30) -> dict[str, list]:
    """Distinct values for the filter dropdowns."""
    companies = [
        row[0]
        for row in session.execute(
            select(Job.company_name, func.count(Job.id))
            .where(Job.is_active.is_(True))
            .group_by(Job.company_name)
            .order_by(func.count(Job.id).desc())
            .limit(limit)
        ).all()
    ]
    sources = [
        row[0]
        for row in session.execute(
            select(JobListing.source, func.count(JobListing.id))
            .group_by(JobListing.source)
            .order_by(func.count(JobListing.id).desc())
        ).all()
    ]
    locations = [
        row[0]
        for row in session.execute(
            select(Job.location_raw, func.count(Job.id))
            .where(Job.is_active.is_(True), Job.location_raw.is_not(None))
            .group_by(Job.location_raw)
            .order_by(func.count(Job.id).desc())
            .limit(limit)
        ).all()
    ]
    skills: dict[str, int] = {}
    for (blob,) in session.execute(
        select(Job.skills).where(Job.is_active.is_(True)).limit(2000)
    ).all():
        for skill in blob or []:
            skills[skill] = skills.get(skill, 0) + 1
    top_skills = [name for name, _ in sorted(skills.items(), key=lambda kv: -kv[1])[:limit]]

    return {
        "companies": companies,
        "sources": sources,
        "locations": locations,
        "skills": top_skills,
        "priorities": [p.value for p in Priority],
        "statuses": [s.value for s in JobStatus],
    }


def kanban_board(session: Session, user: User | None = None) -> dict[str, list[Job]]:
    """Jobs grouped by tracker column."""
    from app.models.base import KANBAN_ORDER

    board: dict[str, list[Job]] = {}
    for status in list(KANBAN_ORDER) + [JobStatus.REJECTED]:
        jobs = list(
            session.scalars(
                select(Job)
                .where(
                    func.coalesce(_user_status(user), JobStatus.NEW.value) == status.value
                    if user is not None
                    else Job.status == status.value
                )
                .order_by(Job.relevance_score.desc())
                .limit(80)
            ).all()
        )
        board[status.value] = jobs
    return board


def analytics_summary(session: Session) -> dict[str, Any]:
    """Aggregate metrics for the analytics page."""
    total_jobs = session.scalar(select(func.count(Job.id))) or 0
    total_listings = session.scalar(select(func.count(JobListing.id))) or 0
    duplicates = max(0, total_listings - total_jobs)
    avg_score = session.scalar(select(func.avg(Job.relevance_score))) or 0.0

    multi_source = (
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

    applied = session.scalar(
        select(func.count(Application.id)).where(Application.date_applied.is_not(None))
    ) or 0
    interviews = session.scalar(
        select(func.count(Application.id)).where(Application.date_interview.is_not(None))
    ) or 0
    offers = session.scalar(
        select(func.count(Application.id)).where(Application.date_offer.is_not(None))
    ) or 0

    top_companies = session.execute(
        select(Job.company_name, func.count(Job.id).label("n"))
        .where(Job.is_active.is_(True))
        .group_by(Job.company_name)
        .order_by(func.count(Job.id).desc())
        .limit(12)
    ).all()
    top_locations = session.execute(
        select(Job.location_raw, func.count(Job.id).label("n"))
        .where(Job.is_active.is_(True), Job.location_raw.is_not(None))
        .group_by(Job.location_raw)
        .order_by(func.count(Job.id).desc())
        .limit(12)
    ).all()
    by_source = session.execute(
        select(JobListing.source, func.count(JobListing.id).label("n"))
        .group_by(JobListing.source)
        .order_by(func.count(JobListing.id).desc())
    ).all()

    companies_known = session.scalar(select(func.count(Company.id))) or 0

    return {
        "total_jobs": total_jobs,
        "total_listings": total_listings,
        "duplicates_removed": duplicates,
        "multi_source_jobs": multi_source,
        "avg_score": round(float(avg_score), 1),
        "applied": applied,
        "interviews": interviews,
        "offers": offers,
        "companies_known": companies_known,
        "top_companies": [(name, n) for name, n in top_companies],
        "top_locations": [(name, n) for name, n in top_locations],
        "by_source": [(name, n) for name, n in by_source],
    }


def source_yield_stats(session: Session, days: int = 30) -> list[dict[str, Any]]:
    """Which sources actually produce strong matches (adaptive search input).

    Reported for transparency and ordering only. A low-yield source is never
    disabled automatically: tomorrow it may carry the best job of the season.
    """
    cutoff = utcnow() - timedelta(days=days)
    rows = session.execute(
        select(
            JobListing.source,
            func.count(func.distinct(JobListing.job_id)).label("jobs"),
            func.sum(
                case(
                    (
                        Job.priority.in_(
                            [Priority.APPLY_NOW.value, Priority.STRONG_MATCH.value]
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("strong"),
        )
        .join(Job, Job.id == JobListing.job_id)
        .where(JobListing.first_seen_at >= cutoff)
        .group_by(JobListing.source)
    ).all()

    total_strong = sum(int(r.strong or 0) for r in rows) or 1
    out = []
    for row in rows:
        strong = int(row.strong or 0)
        out.append(
            {
                "source": row.source,
                "jobs": int(row.jobs or 0),
                "strong_matches": strong,
                "share_of_strong": round(100.0 * strong / total_strong, 1),
            }
        )
    return sorted(out, key=lambda d: -d["strong_matches"])


def recent_runs(session: Session, limit: int = 20) -> list[SearchRun]:
    return list(
        session.scalars(select(SearchRun).order_by(SearchRun.started_at.desc()).limit(limit)).all()
    )


def recent_notifications(session: Session, limit: int = 20) -> list[Notification]:
    return list(
        session.scalars(
            select(Notification).order_by(Notification.created_at.desc()).limit(limit)
        ).all()
    )
