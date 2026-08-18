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
