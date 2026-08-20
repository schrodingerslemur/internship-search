"""Persistence and lifecycle: NEW, UPDATED, REPOSTED, EXPIRED."""

from __future__ import annotations

from datetime import timedelta

from app.models import Application, Company, Job, JobEvent, JobListing
from app.models.base import Freshness, JobStatus
from app.pipeline import discovery
from app.pipeline.dedupe import deduplicate, merge_cluster_facts
from app.pipeline.match import score_job
from app.pipeline.normalize import normalize_job
from app.services.actions import add_note, get_job, set_status
from app.services.persistence import expire_stale_jobs, persist_clusters
from tests.conftest import NOW, make_raw


def run_pipeline(session, raws, prefs, profile, *, now=NOW, run_id=1):
    """Normalise -> dedupe -> score -> persist, as the runner does."""
    normalized = [normalize_job(r) for r in raws]
    normalized = [j for j in normalized if j is not None]
    result = deduplicate(normalized)
    reps = [merge_cluster_facts(c, now=now) for c in result.clusters]
    scores = {r.key: score_job(r, prefs, profile, now=now) for r in reps}
    return persist_clusters(
        session, result.clusters, scores, run_id=run_id, min_score_to_store=0.0, now=now
    )


class TestPersistence:
    def test_new_job_is_stored_once_with_all_listings(
        self, session, prefs, profile, cross_source_duplicates
    ):
        outcome = run_pipeline(session, cross_source_duplicates, prefs, profile)
        assert outcome.new_jobs == 1
        assert session.query(Job).count() == 1
        assert session.query(JobListing).count() == 5

    def test_stored_job_reports_its_source_count(
        self, session, prefs, profile, cross_source_duplicates
    ):
        run_pipeline(session, cross_source_duplicates, prefs, profile)
        job = session.query(Job).one()
        assert job.source_count == 5
        assert "greenhouse" in job.source_names

    def test_distinct_jobs_are_stored_separately(self, session, prefs, profile, distinct_jobs):
        outcome = run_pipeline(session, distinct_jobs, prefs, profile)
        assert outcome.new_jobs == 5
        assert session.query(Job).count() == 5

    def test_scores_and_reasons_are_persisted(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        assert job.relevance_score > 0
        assert job.match_reasons
        assert job.score_breakdown

    def test_low_scoring_jobs_can_be_filtered_from_storage(self, session, prefs, profile):
        normalized = [normalize_job(make_raw(title="Marketing Intern",
                                             description="Social media work."))]
        result = deduplicate(normalized)
        reps = [merge_cluster_facts(c) for c in result.clusters]
        scores = {r.key: score_job(r, prefs, profile, now=NOW) for r in reps}
        outcome = persist_clusters(
            session, result.clusters, scores, min_score_to_store=95.0, now=NOW
        )
        assert outcome.new_jobs == 0


class TestFreshness:
    def test_second_run_does_not_create_a_duplicate(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        outcome = run_pipeline(session, [make_raw()], prefs, profile, now=NOW + timedelta(days=1))
        assert outcome.new_jobs == 0
        assert session.query(Job).count() == 1

    def test_job_appearing_on_a_new_source_is_not_new(self, session, prefs, profile):
        """Moving from one board to another must not re-trigger 'new'."""
        first = make_raw(source="greenhouse", source_job_id="1",
                         url="https://boards.greenhouse.io/nvidia/jobs/12345")
        run_pipeline(session, [first], prefs, profile)

        also_on_linkedin = make_raw(
            source="linkedin", source_job_id="li-1",
            url="https://www.linkedin.com/jobs/view/55",
            apply_url="https://boards.greenhouse.io/nvidia/jobs/12345",
        )
        outcome = run_pipeline(
            session, [first, also_on_linkedin], prefs, profile, now=NOW + timedelta(days=1)
        )
        assert outcome.new_jobs == 0
        assert session.query(Job).count() == 1
        assert session.query(Job).one().source_count == 2

    def test_material_change_marks_the_job_updated(self, session, prefs, profile):
        run_pipeline(session, [make_raw(salary_raw="$50 per hour")], prefs, profile)
        changed = make_raw(salary_raw="$70 per hour")
        outcome = run_pipeline(session, [changed], prefs, profile, now=NOW + timedelta(days=1))
        assert outcome.updated_jobs == 1
        job = session.query(Job).one()
        assert job.freshness == Freshness.UPDATED.value
        assert job.salary_min == 70

    def test_deadline_appearing_is_recorded(self, session, prefs, profile):
        run_pipeline(session, [make_raw(salary_raw="$50 per hour")], prefs, profile)
        changed = make_raw(
            salary_raw="$50 per hour",
            description="FPGA internship. Applications close by December 15, 2026.",
        )
        run_pipeline(session, [changed], prefs, profile, now=NOW + timedelta(days=1))
        assert session.query(Job).one().deadline is not None

    def test_cosmetic_rerun_is_not_material(self, session, prefs, profile):
        run_pipeline(session, [make_raw(salary_raw="$50 per hour")], prefs, profile)
        outcome = run_pipeline(
            session, [make_raw(salary_raw="$50 per hour")], prefs, profile,
            now=NOW + timedelta(days=1),
        )
        assert outcome.updated_jobs == 0

    def test_update_is_recorded_as_an_event(self, session, prefs, profile):
        run_pipeline(session, [make_raw(salary_raw="$50 per hour")], prefs, profile)
        run_pipeline(session, [make_raw(salary_raw="$99 per hour")], prefs, profile,
                     now=NOW + timedelta(days=1))
        events = session.query(JobEvent).filter_by(event_type="updated").all()
        assert events

    def test_missing_data_never_overwrites_known_data(self, session, prefs, profile):
        run_pipeline(session, [make_raw(salary_raw="$50 per hour")], prefs, profile)
        before = session.query(Job).one().salary_min
        run_pipeline(session, [make_raw(salary_raw=None)], prefs, profile,
                     now=NOW + timedelta(days=1))
        assert session.query(Job).one().salary_min == before

    def test_stale_job_expires(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        job.last_seen_at = NOW - timedelta(days=40)
        session.flush()
        expired = expire_stale_jobs(session, grace_days=10)
        assert expired == 1
        assert session.query(Job).one().status == JobStatus.EXPIRED.value

    def test_applied_jobs_are_never_auto_expired(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        job.status = JobStatus.APPLIED.value
        job.last_seen_at = NOW - timedelta(days=90)
        session.flush()
        assert expire_stale_jobs(session, grace_days=10) == 0

    def test_expired_job_that_returns_is_marked_reposted(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        job.is_active = False
        job.status = JobStatus.EXPIRED.value
        session.flush()

        outcome = run_pipeline(session, [make_raw()], prefs, profile, now=NOW + timedelta(days=20))
        assert outcome.reposted_jobs == 1
        refreshed = session.query(Job).one()
        assert refreshed.freshness == Freshness.REPOSTED.value
        assert refreshed.is_active is True


class TestUserActions:
    def test_save_creates_an_application(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        set_status(session, job, JobStatus.SAVED, now=NOW)
        application = session.query(Application).one()
        assert application.date_saved is not None
        assert job.status == JobStatus.SAVED.value

    def test_applying_snapshots_the_score(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        original = job.relevance_score
        set_status(session, job, JobStatus.APPLIED, now=NOW)
        job.relevance_score = 10.0  # later re-scoring must not rewrite history
        session.flush()
        assert session.query(Application).one().score_at_apply == original

    def test_status_changes_are_logged(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        set_status(session, job, JobStatus.SAVED, now=NOW)
        set_status(session, job, JobStatus.APPLIED, now=NOW)
        assert session.query(JobEvent).filter_by(event_type="status_changed").count() == 2

    def test_notes_attach_to_the_application(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        add_note(session, job, "Need a referral from Priya")
        assert session.query(Application).one().notes[0].body.startswith("Need a referral")

    def test_empty_note_is_ignored(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        assert add_note(session, session.query(Job).one(), "   ") is None

    def test_lookup_by_canonical_id(self, session, prefs, profile):
        run_pipeline(session, [make_raw()], prefs, profile)
        job = session.query(Job).one()
        assert get_job(session, job.canonical_job_id).id == job.id


class TestDiscovery:
    def test_companies_are_registered_from_any_listing(self, session):
        raws = [make_raw(company="Totally Unknown Startup", source_job_id="1")]
        created = discovery.register_companies(session, raws, preferred=set(), blacklisted=set())
        assert created == 1
        assert session.query(Company).one().name == "Totally Unknown Startup"

    def test_boards_are_harvested_from_urls(self, session):
        raws = [
            make_raw(source_job_id="1", url="https://job-boards.greenhouse.io/acme/jobs/1"),
            make_raw(source_job_id="2", url="https://jobs.lever.co/betacorp/xyz"),
            make_raw(source_job_id="3", url="https://jobs.ashbyhq.com/gamma/abc"),
        ]
        harvested = discovery.harvest_boards(raws)
        assert {"greenhouse:acme", "lever:betacorp", "ashby:gamma"} <= set(harvested)

    def test_harvested_boards_are_registered_for_future_crawls(self, session):
        raws = [make_raw(source_job_id="1", url="https://job-boards.greenhouse.io/newco/jobs/1")]
        discovery.register_companies(session, raws, preferred=set(), blacklisted=set())
        created = discovery.register_boards(session, discovery.harvest_boards(raws))
        assert created == 1
        boards = discovery.select_boards_to_crawl(session, limit=10)
        assert any(b["board_token"] == "newco" for b in boards)

    def test_aggregator_apply_link_reveals_the_employer_board(self, session):
        """Discovery works even when the posting URL is an aggregator."""
        raws = [
            make_raw(
                source="adzuna", source_job_id="1",
                url="https://www.adzuna.com/land/ad/123",
                apply_url="https://job-boards.greenhouse.io/hiddengem/jobs/9",
            )
        ]
        assert "greenhouse:hiddengem" in discovery.harvest_boards(raws)

    def test_boards_are_not_duplicated(self, session):
        raws = [make_raw(source_job_id="1", url="https://job-boards.greenhouse.io/acme/jobs/1")]
        discovery.register_boards(session, discovery.harvest_boards(raws))
        second = discovery.register_boards(session, discovery.harvest_boards(raws))
        assert second == 0

    def test_preferred_flags_are_applied_on_registration(self, session):
        raws = [make_raw(company="NVIDIA", source_job_id="1")]
        discovery.register_companies(session, raws, preferred={"nvidia"}, blacklisted=set())
        assert session.query(Company).one().is_preferred is True


class TestOversizedValues:
    """SQLite ignores VARCHAR limits; PostgreSQL enforces them.

    These tests assert the guard directly rather than relying on the database,
    so they catch the overflow on SQLite too -- which is the whole point, since
    the bug they cover reached production precisely because SQLite stayed quiet.
    """

    #: The real value that aborted the first production run.
    WORKDAY_SLUG = (
        "Stagiaire-associ-e---analyste-des-systmes-d-affaires---Automne-2026---"
        "Associate-Business-Systems-Analyst-Intern---Fall-2026_JR0150830"
    )

    def _column_length(self, model, name: str) -> int:
        return model.__table__.columns[name].type.length

    def test_the_workday_slug_that_broke_production_now_fits(self, session):
        from app.models import Job

        assert len(self.WORKDAY_SLUG) > 120, "fixture no longer reproduces the overflow"
        assert self._column_length(Job, "requisition_id") >= len(self.WORKDAY_SLUG)

    def test_identity_values_are_never_truncated(self, session):
        """Truncation here would merge two distinct openings -- the cardinal sin."""
        from app.models import Job

        job = Job(
            canonical_job_id="cid-req",
            fingerprint="fp",
            company_name="McKesson",
            title="Associate Business Systems Analyst Intern",
            application_url="https://example.com/1",
            requisition_id=self.WORKDAY_SLUG,
        )
        session.add(job)
        session.flush()
        assert job.requisition_id == self.WORKDAY_SLUG

    def test_two_slugs_sharing_a_long_prefix_stay_distinct(self, session):
        """The exact over-merge that truncating requisition ids would cause."""
        from app.models import Job

        base = "Associate-Business-Systems-Analyst-Intern-Fall-2026" * 3
        first, second = f"{base}_JR0150830", f"{base}_JR0150831"
        assert first[:120] == second[:120], "fixture must share a >120 char prefix"

        for i, req in enumerate((first, second)):
            session.add(
                Job(
                    canonical_job_id=f"cid-{i}",
                    fingerprint=f"fp-{i}",
                    company_name="McKesson",
                    title="Analyst Intern",
                    application_url=f"https://example.com/{i}",
                    requisition_id=req,
                )
            )
        session.flush()

        stored = {j.requisition_id for j in session.query(Job).all()}
        assert stored == {first, second}

    def test_overlong_free_text_is_clamped_to_fit(self, session):
        """A freak listing must not abort the flush that carries every other job."""
        from app.models import Job

        limit = self._column_length(Job, "location_raw")
        job = Job(
            canonical_job_id="cid-loc",
            fingerprint="fp-loc",
            company_name="Acme",
            title="Intern",
            application_url="https://example.com/x",
            location_raw="Montreal, QC, Canada; " * 100,
        )
        session.add(job)
        session.flush()

        assert len(job.location_raw) == limit
        assert job.location_raw.endswith("…")

    def test_values_within_the_limit_are_left_alone(self, session):
        from app.models import Job

        job = Job(
            canonical_job_id="cid-ok",
            fingerprint="fp-ok",
            company_name="Acme",
            title="FPGA Intern",
            application_url="https://example.com/ok",
            location_raw="Santa Clara, CA",
        )
        session.add(job)
        session.flush()
        assert job.location_raw == "Santa Clara, CA"

    def test_clamping_applies_on_update_too(self, session):
        from app.models import Job

        job = Job(
            canonical_job_id="cid-upd",
            fingerprint="fp-upd",
            company_name="Acme",
            title="Intern",
            application_url="https://example.com/u",
            location_raw="Austin, TX",
        )
        session.add(job)
        session.flush()

        job.location_raw = "x" * 5000
        session.flush()
        assert len(job.location_raw) == self._column_length(Job, "location_raw")


class TestPersistenceQueryCost:
    """Round trips must not scale with the number of jobs.

    Persistence resolved each cluster with its own SELECTs and its own flush.
    Against local SQLite that is free; against a hosted Postgres it cost 34
    minutes for 4,281 jobs -- seven times the entire internet-facing search
    that produced them, and enough to run into the workflow timeout. These
    tests fail if per-job querying comes back, which no assertion about
    correctness would catch.
    """

    def _count_queries(self, session, fn):
        from sqlalchemy import event

        engine = session.get_bind()
        statements: list[str] = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return statements

    def _raws(self, n: int, offset: int = 0):
        return [
            make_raw(
                title=f"FPGA Design Intern {i}",
                company=f"Company {i}",
                url=f"https://boards.greenhouse.io/co{i}/jobs/{i}",
                source_job_id=f"co{i}-{i}",
            )
            for i in range(offset, offset + n)
        ]

    def _selects(self, statements):
        return [s for s in statements if s.lstrip().upper().startswith("SELECT")]

    def test_lookup_queries_do_not_grow_with_job_count(self, session, prefs, profile):
        """The lookups are prefetched, so their count is independent of volume.

        INSERTs necessarily scale with the number of rows -- SQLAlchemy batches
        those into few round trips on Postgres -- but the identity lookups that
        caused the 34-minute run must stay flat.
        """
        # Disjoint company ranges, so both batches are first-time inserts and
        # the only difference between them is volume.
        small = self._selects(self._count_queries(
            session, lambda: run_pipeline(session, self._raws(5), prefs, profile)
        ))
        big = self._selects(self._count_queries(
            session,
            lambda: run_pipeline(session, self._raws(60, offset=500), prefs, profile, run_id=2),
        ))

        assert len(big) == len(small), (
            f"lookup queries scaled with job count: {len(small)} -> {len(big)}"
        )

    def test_a_large_batch_stays_within_a_flat_query_budget(self, session, prefs, profile):
        selects = self._selects(self._count_queries(
            session, lambda: run_pipeline(session, self._raws(200), prefs, profile)
        ))
        # Six: listings by source id, jobs and listings by ATS identity,
        # listings by URL hash, jobs by fingerprint, and companies by slug.
        assert len(selects) <= 10, f"expected a bulk-prefetch shape, got {len(selects)} SELECTs"

    def test_everything_is_still_stored_correctly(self, session, prefs, profile):
        """The optimisation must not lose rows -- the point of batching safely."""
        outcome = run_pipeline(session, self._raws(40), prefs, profile)
        assert outcome.new_jobs == 40
        assert session.query(Job).count() == 40
        assert session.query(JobListing).count() == 40
        assert len(outcome.new_job_ids) == 40
        assert all(i is not None for i in outcome.new_job_ids)

    def test_a_second_run_reidentifies_rather_than_duplicating(self, session, prefs, profile):
        """Cross-run identity still works without the per-cluster flush."""
        raws = self._raws(25)
        run_pipeline(session, raws, prefs, profile)
        outcome = run_pipeline(session, raws, prefs, profile, run_id=2)

        assert outcome.new_jobs == 0
        assert session.query(Job).count() == 25
        assert session.query(JobListing).count() == 25

    def test_events_are_attached_to_the_right_jobs(self, session, prefs, profile):
        run_pipeline(session, self._raws(10), prefs, profile)
        events = session.query(JobEvent).filter(JobEvent.event_type == "discovered").all()
        assert len(events) == 10
        assert all(e.job_id is not None for e in events)
        assert len({e.job_id for e in events}) == 10
