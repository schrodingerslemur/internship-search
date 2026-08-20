"""Persisting canonical jobs and detecting what changed since last run.

This stage answers the questions the notification engine depends on:

* Is this job genuinely **new**, or just the same job appearing on a new site?
* Did an existing job **materially change** (deadline, salary, requirements)?
* Was a closed job **reposted**?
* Has a job **expired** because it stopped appearing where it used to be?

Crucially, a job that moves from Indeed to LinkedIn is *not* new: matching
happens against the canonical identity, so a new listing attaches to the
existing job instead of creating one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Company, Job, JobEvent, JobListing
from app.models.base import TERMINAL_STATUSES, Freshness, JobStatus, utcnow
from app.pipeline.dedupe import EXCLUSIVE_AXES, merge_cluster_facts
from app.pipeline.match import MatchResult
from app.pipeline.textutil import discriminators, slugify_company
from app.schemas.job import JobCluster, NormalizedJob

log = get_logger("persistence")

#: Fields whose change counts as *material* and may justify a fresh alert.
MATERIAL_FIELDS: tuple[str, ...] = (
    "deadline",
    "salary_min",
    "salary_max",
    "requirements",
    "location_raw",
    "sponsorship",
    "application_url",
)

#: A job unseen for this long in sources that once carried it is expired.
EXPIRY_GRACE_DAYS = 10


@dataclass
class PersistOutcome:
    new_jobs: int = 0
    updated_jobs: int = 0
    reposted_jobs: int = 0
    unchanged_jobs: int = 0
    stored_jobs: list[Job] = field(default_factory=list)
    new_job_ids: list[int] = field(default_factory=list)
    updated_job_ids: list[int] = field(default_factory=list)


def _set_if_present(job: Job, field: str, value: object) -> None:
    """Assign only when the incoming value carries information."""
    if value is None or value == "":
        return
    setattr(job, field, value)


def canonical_id_for(job: NormalizedJob) -> str:
    """Stable public identifier for a canonical job."""
    basis = job.ats_identity or job.canonical_url or f"{job.company_slug}|{job.fingerprint}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------
# Bulk identity lookup
#
# Resolving each cluster against the database individually costs five to ten
# round trips per job. That is invisible against local SQLite and ruinous
# against a hosted Postgres: 4,281 jobs took 34 minutes, against five for the
# entire internet-facing search that produced them. The same information is
# gathered here in a fixed handful of queries and then answered from memory.
# --------------------------------------------------------------------------

#: Well under the 65,535 bind-parameter ceiling psycopg enforces per statement.
LOOKUP_CHUNK = 900

#: Jobs written between flushes. Bounded so a long run cannot balloon the
#: session's identity map, while still removing almost all the round trips.
FLUSH_EVERY = 500


def _chunked(values, size: int = LOOKUP_CHUNK):
    seq = list(values)
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


class LookupIndex:
    """Cross-run identity lookups for a whole run, prefetched in bulk.

    Objects created during the run are registered back into the index, so two
    clusters resolving to the same stored job still converge on it -- a
    property that used to fall out of flushing after every single cluster.
    """

    def __init__(
        self, session: Session, prepared: list[tuple[JobCluster, NormalizedJob]]
    ) -> None:
        self.session = session
        self.listings_by_source_key: dict[tuple[str, str], JobListing] = {}
        self.jobs_by_ats_identity: dict[str, Job] = {}
        self.listings_by_ats_identity: dict[str, JobListing] = {}
        self.listings_by_url_hash: dict[str, JobListing] = {}
        self.jobs_by_fingerprint: dict[str, list[Job]] = {}
        self.companies_by_slug: dict[str, Company] = {}
        self.listings_by_job_id: dict[object, dict[tuple[str, str], JobListing]] = {}
        self._load(prepared)

    def _load(self, prepared: list[tuple[JobCluster, NormalizedJob]]) -> None:
        source_ids: set[str] = set()
        identities: set[str] = set()
        hashes: set[str] = set()
        fingerprints: set[str] = set()
        slugs: set[str] = set()

        for cluster, rep in prepared:
            fingerprints.add(rep.fingerprint)
            slugs.add(rep.company_slug)
            for member in cluster.members:
                if member.source_job_id:
                    source_ids.add(member.source_job_id)
                if member.ats_identity:
                    identities.add(member.ats_identity)
                if member.canonical_url_hash:
                    hashes.add(member.canonical_url_hash)

        # Matched on source_job_id alone, then narrowed by (source, id) in
        # Python: one indexed IN beats a large OR of composite equalities.
        for batch in _chunked(sorted(source_ids)):
            for row in self.session.scalars(
                select(JobListing).where(JobListing.source_job_id.in_(batch))
            ).all():
                self.listings_by_source_key[(row.source, row.source_job_id)] = row

        for batch in _chunked(sorted(identities)):
            for job in self.session.scalars(
                select(Job).where(Job.ats_identity.in_(batch))
            ).all():
                self.jobs_by_ats_identity.setdefault(job.ats_identity, job)
            for row in self.session.scalars(
                select(JobListing).where(JobListing.ats_identity.in_(batch))
            ).all():
                self.listings_by_ats_identity.setdefault(row.ats_identity, row)

        for batch in _chunked(sorted(hashes)):
            for row in self.session.scalars(
                select(JobListing).where(JobListing.canonical_url_hash.in_(batch))
            ).all():
                self.listings_by_url_hash.setdefault(row.canonical_url_hash, row)

        for batch in _chunked(sorted(fingerprints)):
            for job in self.session.scalars(
                select(Job).where(Job.fingerprint.in_(batch), Job.company_name.is_not(None))
            ).all():
                self.jobs_by_fingerprint.setdefault(job.fingerprint, []).append(job)

        for batch in _chunked(sorted(s for s in slugs if s)):
            for company in self.session.scalars(
                select(Company).where(Company.slug.in_(batch))
            ).all():
                self.companies_by_slug[company.slug] = company

        job_ids = {job.id for job in self._candidate_jobs() if job.id is not None}
        for batch in _chunked(sorted(job_ids)):
            for row in self.session.scalars(
                select(JobListing).where(JobListing.job_id.in_(batch))
            ).all():
                self.listings_by_job_id.setdefault(row.job_id, {})[
                    (row.source, row.source_job_id)
                ] = row

    def _candidate_jobs(self):
        seen: set[int] = set()
        for job in self.jobs_by_ats_identity.values():
            if id(job) not in seen:
                seen.add(id(job))
                yield job
        for jobs in self.jobs_by_fingerprint.values():
            for job in jobs:
                if id(job) not in seen:
                    seen.add(id(job))
                    yield job
        for row in self.listings_by_source_key.values():
            job = row.job
            if job is not None and id(job) not in seen:
                seen.add(id(job))
                yield job

    # ---- registration of objects created during this run ----------------

    def register_job(self, job: Job) -> None:
        if job.ats_identity:
            self.jobs_by_ats_identity.setdefault(job.ats_identity, job)
        if job.fingerprint:
            self.jobs_by_fingerprint.setdefault(job.fingerprint, []).append(job)

    def register_listing(self, job: Job, listing: JobListing) -> None:
        self.listings_by_source_key[(listing.source, listing.source_job_id)] = listing
        if listing.ats_identity:
            self.listings_by_ats_identity.setdefault(listing.ats_identity, listing)
        if listing.canonical_url_hash:
            self.listings_by_url_hash.setdefault(listing.canonical_url_hash, listing)
        # Keyed by object identity: a job created this run has no primary key
        # until the next flush, but still needs to accumulate its listings.
        self.listings_by_job_id.setdefault(id(job), {})[
            (listing.source, listing.source_job_id)
        ] = listing

    def listings_for_job(self, job: Job) -> dict[tuple[str, str], JobListing]:
        """Listings already attached to this job, by (source, source_job_id)."""
        by_pk = self.listings_by_job_id.get(job.id, {}) if job.id is not None else {}
        by_obj = self.listings_by_job_id.get(id(job), {})
        if not by_obj:
            return by_pk
        merged = dict(by_pk)
        merged.update(by_obj)
        return merged


def find_existing_job(index: LookupIndex, rep: NormalizedJob, cluster: JobCluster) -> Job | None:
    """Locate the stored job this cluster belongs to.

    Tried in descending order of certainty, mirroring the dedup stages so that
    cross-run identity matches cross-source identity.
    """
    # 1. Any listing already stored from these exact sources.
    for member in cluster.members:
        listing = index.listings_by_source_key.get((member.source, member.source_job_id))
        if listing is not None and listing.job is not None:
            return listing.job

    # 2. ATS identity, from any member.
    for member in cluster.members:
        if not member.ats_identity:
            continue
        job = index.jobs_by_ats_identity.get(member.ats_identity)
        if job is not None:
            return job
        listing = index.listings_by_ats_identity.get(member.ats_identity)
        if listing is not None and listing.job is not None:
            return listing.job

    # 3. Canonical URL hash.
    for member in cluster.members:
        if not member.canonical_url_hash:
            continue
        listing = index.listings_by_url_hash.get(member.canonical_url_hash)
        if listing is not None and listing.job is not None:
            return listing.job

    # 4. Deterministic fingerprint -- inferential, so it gets the same guards
    #    the in-run deduplicator applies. Without this, "FPGA Intern" and
    #    "FPGA Intern - Fall 2026" would reunite across runs even though the
    #    deduplicator deliberately kept them apart.
    for job in index.jobs_by_fingerprint.get(rep.fingerprint, []):
        if not job.company_name:
            continue
        if slugify_company(job.company_name) != rep.company_slug:
            continue
        if _stored_job_conflicts(job, rep):
            continue
        return job
    return None


def _stored_job_conflicts(job: Job, rep: NormalizedJob) -> bool:
    """Whether a stored job is a *different* opening from the incoming one."""
    if job.requisition_id and rep.requisition_id:
        if job.requisition_id.strip().lower() != rep.requisition_id.strip().lower():
            return True
    if job.ats_identity and rep.ats_identity and job.ats_identity != rep.ats_identity:
        return True
    stored_axes = discriminators(job.title, job.requirements or job.description or "")
    for axis in EXCLUSIVE_AXES:
        left, right = stored_axes & axis, rep.discriminators & axis
        if left and right and left != right:
            return True
    return False


def _material_changes(job: Job, rep: NormalizedJob, apply_url: str) -> dict[str, dict]:
    """Diff the stored job against a freshly-seen version."""
    incoming = {
        "deadline": rep.deadline,
        "salary_min": rep.salary_min,
        "salary_max": rep.salary_max,
        "requirements": rep.requirements,
        "location_raw": rep.location_raw,
        "sponsorship": str(rep.sponsorship),
        "application_url": apply_url,
    }
    changes: dict[str, dict] = {}
    for name in MATERIAL_FIELDS:
        old = getattr(job, name, None)
        new = incoming.get(name)
        if new is None:
            continue  # Missing data never overwrites known data.
        if name == "requirements":
            # Compare coarsely; whitespace churn is not a material change.
            old_h = hashlib.md5((old or "").split().__str__().encode()).hexdigest()
            new_h = hashlib.md5((new or "").split().__str__().encode()).hexdigest()
            if old_h != new_h and old:
                changes[name] = {"old": "(changed)", "new": "(changed)"}
            continue
        if isinstance(old, datetime) and isinstance(new, datetime):
            if abs((old - new).total_seconds()) > 3600:
                changes[name] = {"old": old.isoformat(), "new": new.isoformat()}
            continue
        if old != new and old not in (None, ""):
            changes[name] = {"old": str(old)[:120], "new": str(new)[:120]}
    return changes


def persist_clusters(
    session: Session,
    clusters: list[JobCluster],
    scores: dict[str, MatchResult],
    *,
    run_id: int | None = None,
    min_score_to_store: float = 0.0,
    now: datetime | None = None,
) -> PersistOutcome:
    """Write canonical jobs and their per-source listings.

    ``scores`` is keyed by the cluster representative's ``key``.

    Work is done in three passes rather than one: merge and score entirely in
    memory, fetch every identity lookup the run needs in a handful of bulk
    queries, then write. Listings and events are attached through their
    relationships, so nothing needs a primary key before its flush and the
    whole batch can be written a few hundred jobs at a time.
    """
    now = now or utcnow()
    outcome = PersistOutcome()

    # ---- pass 1: merge and filter, no database involved ----
    prepared: list[tuple[JobCluster, NormalizedJob, MatchResult, str]] = []
    for cluster in clusters:
        rep = merge_cluster_facts(cluster, now=now)
        result = scores.get(rep.key)
        if result is None or result.excluded:
            continue
        if result.score < min_score_to_store:
            continue
        apply_url = rep.apply_url or rep.url or ""
        if not apply_url:
            continue
        prepared.append((cluster, rep, result, apply_url))

    if not prepared:
        session.flush()
        return outcome

    # ---- pass 2: every lookup this run will need, in bulk ----
    index = LookupIndex(session, [(cluster, rep) for cluster, rep, _, _ in prepared])

    # ---- pass 3: write ----
    new_jobs: list[Job] = []
    pending = 0

    for cluster, rep, result, apply_url in prepared:
        company = index.companies_by_slug.get(rep.company_slug)

        job = find_existing_job(index, rep, cluster)
        is_new = job is None

        if is_new:
            job = Job(
                canonical_job_id=canonical_id_for(rep),
                fingerprint=rep.fingerprint,
                date_discovered=now,
                first_run_id=run_id,
                status=JobStatus.NEW.value,
                freshness=Freshness.NEW.value,
            )
            session.add(job)
            outcome.new_jobs += 1
        else:
            changes = _material_changes(job, rep, apply_url)
            was_expired = not job.is_active or job.status == JobStatus.EXPIRED.value
            if was_expired:
                job.freshness = Freshness.REPOSTED.value
                job.times_reposted = (job.times_reposted or 0) + 1
                job.expired_at = None
                if job.status == JobStatus.EXPIRED.value:
                    job.status = JobStatus.NEW.value
                outcome.reposted_jobs += 1
                _add_event(job, "reposted", "Job reappeared in search results", run_id, now)
            elif changes:
                job.freshness = Freshness.UPDATED.value
                outcome.updated_jobs += 1
                outcome.updated_job_ids.append(job.id)
                _add_event(job, "updated", "Material change detected", run_id, now, changes)
            else:
                # Age the job out of NEW once it has been seen before.
                if job.freshness in (Freshness.NEW.value, Freshness.UPDATED.value):
                    age_days = (now - (job.date_discovered or now)).days
                    if age_days >= 1:
                        job.freshness = Freshness.OLD.value
                outcome.unchanged_jobs += 1

        # ---- write canonical fields ----
        job.company_name = rep.company
        job.company_id = company.id if company else job.company_id
        job.title = rep.title
        job.title_core = rep.title_core
        job.ats_identity = rep.ats_identity or job.ats_identity
        job.fingerprint = rep.fingerprint
        _set_if_present(job, "location_raw", rep.location_raw)
        job.locations = rep.locations
        job.city, job.state, job.country = rep.city, rep.state, rep.country
        job.remote_status = str(rep.remote_status)
        job.employment_type = str(rep.employment_type)
        # Enrichment fields: a source that omits a fact this run must not erase
        # what another source told us earlier. Only real values overwrite.
        _set_if_present(job, "description", rep.description)
        _set_if_present(job, "requirements", rep.requirements)
        _set_if_present(job, "responsibilities", rep.responsibilities)
        _set_if_present(job, "preferred_qualifications", rep.preferred_qualifications)
        if rep.salary_min is not None:
            job.salary_min, job.salary_max = rep.salary_min, rep.salary_max
            job.salary_currency = rep.salary_currency or job.salary_currency
            job.salary_period = rep.salary_period or job.salary_period
            job.salary_raw = rep.salary_raw or job.salary_raw
        job.application_url = apply_url
        job.posting_url = rep.url
        job.date_posted = rep.date_posted or job.date_posted
        job.date_updated_source = rep.date_updated or job.date_updated_source
        if rep.deadline and rep.deadline_is_explicit:
            job.deadline = rep.deadline
            job.deadline_is_explicit = True
        job.sponsorship = str(rep.sponsorship)
        job.sponsorship_evidence = rep.sponsorship_evidence
        _set_if_present(job, "experience_required_years", rep.experience_required_years)
        job.degree_requirements = rep.degree_requirements or job.degree_requirements
        job.terms = rep.terms
        job.skills = rep.skills
        _set_if_present(job, "requisition_id", rep.requisition_id)
        job.content_hash = rep.content_hash
        job.last_seen_at = now
        job.is_active = True

        # ---- scoring ----
        job.relevance_score = result.score
        job.priority = str(result.priority)
        job.match_reasons = result.match_reasons
        job.concerns = result.concerns
        job.missing_requirements = result.missing_requirements
        job.score_breakdown = result.breakdown()

        if is_new:
            # Registered before the flush so a later cluster in this same run
            # resolves to this job rather than creating a second copy.
            index.register_job(job)
            new_jobs.append(job)
            _add_event(
                job, "discovered", f"Found on {len(cluster.members)} source(s)", run_id, now
            )

        _upsert_listings(index, job, cluster, now)
        outcome.stored_jobs.append(job)

        pending += 1
        if pending >= FLUSH_EVERY:
            session.flush()
            pending = 0

    session.flush()
    outcome.new_job_ids.extend(job.id for job in new_jobs if job.id is not None)
    return outcome


def _upsert_listings(index: LookupIndex, job: Job, cluster: JobCluster, now: datetime) -> None:
    """Attach every source listing to the canonical job."""
    existing = index.listings_for_job(job)
    for member in cluster.members:
        key = (member.source, member.source_job_id)
        row = existing.get(key)
        if row is None:
            # The listing may already be attached to a different job row if a
            # previous run split them; re-point it rather than violating the
            # uniqueness constraint.
            row = index.listings_by_source_key.get(key)
        if row is None:
            row = JobListing(
                source=member.source,
                source_job_id=member.source_job_id,
                first_seen_at=now,
            )
            # Appending through the relationship lets the unit of work fill in
            # job_id, so a brand-new job needs no early flush to get one.
            job.listings.append(row)
        elif row.job is not job:
            row.job = job
        row.source_kind = str(member.source_kind)
        row.url = member.url
        row.canonical_url = member.canonical_url
        row.canonical_url_hash = member.canonical_url_hash
        row.apply_url = member.apply_url
        row.ats_identity = member.ats_identity
        row.title_raw = member.title
        row.company_raw = member.company
        row.location_raw = member.location_raw
        row.date_posted = member.date_posted
        row.last_seen_at = now
        row.is_active = True
        row.merge_method = cluster.merge_methods.get(member.key)
        row.merge_confidence = cluster.merge_confidence.get(member.key)
        index.register_listing(job, row)


def _add_event(
    job: Job,
    event_type: str,
    detail: str,
    run_id: int | None,
    now: datetime,
    changes: dict | None = None,
) -> None:
    job.events.append(
        JobEvent(
            event_type=event_type,
            detail=detail,
            changes=changes,
            run_id=run_id,
            created_at=now,
        )
    )


def expire_stale_jobs(
    session: Session, *, run_id: int | None = None, grace_days: int = EXPIRY_GRACE_DAYS
) -> int:
    """Mark jobs that have stopped appearing as expired.

    Only jobs the user has not engaged with are auto-expired: an applied or
    saved job stays put so the tracker never loses history.
    """
    now = utcnow()
    cutoff = now - timedelta(days=grace_days)
    protected = {JobStatus.APPLIED.value, JobStatus.ASSESSMENT.value,
                 JobStatus.INTERVIEW.value, JobStatus.OFFER.value, JobStatus.SAVED.value}

    rows = session.scalars(
        select(Job).where(
            Job.is_active.is_(True),
            Job.last_seen_at.is_not(None),
            Job.last_seen_at < cutoff,
        )
    ).all()

    expired = 0
    for job in rows:
        if job.status in protected:
            continue
        job.is_active = False
        job.freshness = Freshness.EXPIRED.value
        job.expired_at = now
        if job.status not in TERMINAL_STATUSES:
            job.status = JobStatus.EXPIRED.value
        _add_event(job, "expired", f"Not seen since {job.last_seen_at:%Y-%m-%d}", run_id, now)
        expired += 1
    session.flush()
    return expired
