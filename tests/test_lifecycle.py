"""The job lifecycle as the user experiences it.

These tests are written against the pages rather than the services, because the
promises they check are promises the interface makes: a job you dealt with leaves
the feed, turns up where you would look for it, and can be taken back.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Job
from app.models.base import JobStatus
from app.services import auth, user_jobs
from app.services.jobs_query import JobFilters


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def account(session):
    user = auth.create_account(session, email="me@example.com", password="a-good-password")
    session.flush()
    return user


@pytest.fixture
def signed_in(client, account):
    client.post("/login", data={"email": "me@example.com", "password": "a-good-password"})
    return client


def make_job(session, *, n: int = 1, title: str = "FPGA Design Intern") -> Job:
    job = Job(
        canonical_job_id=f"job-{n}",
        fingerprint=f"fp-{n}",
        company_name="NVIDIA",
        title=title,
        location_raw="Santa Clara, CA",
        application_url=f"https://example.com/apply/{n}",
        relevance_score=90.0,
        priority="strong_match",
    )
    session.add(job)
    session.flush()
    return job


def act(client, job: Job, status: str, **extra):
    return client.post(f"/job/{job.id}/status", data={"status": status, **extra})


class TestTheFeedShowsOnlyUndecidedWork:
    def test_a_new_job_is_in_the_feed(self, signed_in, session):
        job = make_job(session)
        assert job.title in signed_in.get("/").text

    def test_applying_removes_it_from_the_feed(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, "applied")
        assert job.title not in signed_in.get("/").text

    def test_applying_puts_it_under_applied(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, "applied")
        assert job.title in signed_in.get("/applied").text

    def test_dismissing_removes_it_from_the_feed(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, "dismissed")
        assert job.title not in signed_in.get("/").text

    def test_dismissing_puts_it_under_dismissed(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, "dismissed")
        assert job.title in signed_in.get("/dismissed").text

    def test_saving_keeps_it_in_the_feed(self, signed_in, session):
        """Saving is a bookmark, not a disposal: it is still awaiting a decision."""
        job = make_job(session)
        act(signed_in, job, "saved")
        assert job.title in signed_in.get("/").text

    def test_saving_also_puts_it_under_saved(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, "saved")
        assert job.title in signed_in.get("/saved").text

    def test_an_untouched_job_stays_put_across_visits(self, signed_in, session):
        job = make_job(session)
        signed_in.get("/")
        assert job.title in signed_in.get("/").text


class TestRestoring:
    def test_a_dismissed_job_can_be_restored_to_the_feed(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, "dismissed")
        act(signed_in, job, "new")
        assert job.title in signed_in.get("/").text

    def test_restoring_clears_the_dismissal_timestamp(self, signed_in, session, account):
        job = make_job(session)
        act(signed_in, job, "dismissed")
        assert user_jobs.get_state(session, account, job).dismissed_at is not None
        act(signed_in, job, "new")
        assert user_jobs.get_state(session, account, job).dismissed_at is None

    def test_a_restored_job_leaves_the_dismissed_list(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, "dismissed")
        act(signed_in, job, "new")
        assert job.title not in signed_in.get("/dismissed").text


class TestOpeningAnApplicationIsNotApplying:
    def test_opening_does_not_change_the_status(self, signed_in, session, account):
        job = make_job(session)
        signed_in.post(f"/job/{job.id}/opened", data={"view": "review"})
        state = user_jobs.get_state(session, account, job)
        assert state.status == JobStatus.NEW.value

    def test_opening_records_when_it_happened(self, signed_in, session, account):
        job = make_job(session)
        signed_in.post(f"/job/{job.id}/opened", data={"view": "review"})
        assert user_jobs.get_state(session, account, job).opened_at is not None

    def test_opening_asks_whether_the_application_went_through(self, signed_in, session):
        job = make_job(session)
        response = signed_in.post(f"/job/{job.id}/opened", data={"view": "review"})
        assert "Did you apply?" in response.text

    def test_the_question_survives_a_refresh(self, signed_in, session):
        """Refreshing mid-decision must not silently drop the question."""
        job = make_job(session)
        signed_in.post(f"/job/{job.id}/opened", data={"view": "review"})
        assert "Did you apply?" in signed_in.get("/").text

    def test_not_yet_clears_the_question(self, signed_in, session, account):
        job = make_job(session)
        signed_in.post(f"/job/{job.id}/opened", data={"view": "review"})
        signed_in.post(f"/job/{job.id}/not-applied", data={"view": "review"})
        assert user_jobs.get_state(session, account, job).opened_at is None
        assert "Did you apply?" not in signed_in.get("/").text

    def test_not_yet_leaves_the_job_in_the_feed(self, signed_in, session):
        job = make_job(session)
        signed_in.post(f"/job/{job.id}/opened", data={"view": "review"})
        signed_in.post(f"/job/{job.id}/not-applied", data={"view": "review"})
        assert job.title in signed_in.get("/").text


class TestFeedbackAndUndo:
    def _hx(self, client, job, status, view="review"):
        return client.post(
            f"/job/{job.id}/status",
            data={"status": status, "view": view, "redirect_to": "/"},
            headers={"HX-Request": "true"},
        )

    def test_an_action_says_what_it_did(self, signed_in, session):
        job = make_job(session)
        assert "Dismissed" in self._hx(signed_in, job, "dismissed").text

    def test_an_action_offers_the_way_back(self, signed_in, session):
        job = make_job(session)
        assert "Undo" in self._hx(signed_in, job, "dismissed").text

    def test_a_job_leaving_the_view_is_removed_from_it(self, signed_in, session):
        job = make_job(session)
        response = self._hx(signed_in, job, "dismissed", view="review")
        assert f'id="job-{job.id}"' not in response.text

    def test_a_job_staying_in_the_view_is_re_rendered(self, signed_in, session):
        job = make_job(session)
        response = self._hx(signed_in, job, "saved", view="review")
        assert f'id="job-{job.id}"' in response.text

    def test_undo_returns_the_job_to_where_it_was(self, signed_in, session, account):
        """Undo restores the previous status, not a guess at the starting one."""
        job = make_job(session)
        act(signed_in, job, "saved")
        response = self._hx(signed_in, job, "dismissed", view="review")
        assert f'value="{JobStatus.SAVED.value}"' in response.text

    def test_a_non_htmx_action_confirms_after_the_redirect(self, signed_in, session):
        job = make_job(session)
        response = act(signed_in, job, "saved", redirect_to="/")
        assert "done=Saved" in response.headers["location"]


class TestTimestamps:
    def test_applying_twice_keeps_the_first_date(self, signed_in, session, account):
        """A double-click must not rewrite when you applied."""
        job = make_job(session)
        act(signed_in, job, "applied")
        first = user_jobs.get_state(session, account, job).applied_at
        act(signed_in, job, "applied")
        assert user_jobs.get_state(session, account, job).applied_at == first

    def test_dismissal_is_dated(self, signed_in, session, account):
        job = make_job(session)
        act(signed_in, job, "dismissed")
        assert user_jobs.get_state(session, account, job).dismissed_at is not None


class TestEmailsRespectDecisions:
    """The digest must not re-offer work the user has already finished with."""

    def _selection(self, session, user, prefs):
        from app.notify.digest import select_jobs_for_digest

        return select_jobs_for_digest(session, prefs.notifications, user=user)

    def test_an_applied_job_is_not_sent(self, session, account, prefs):
        job = make_job(session)
        user_jobs.set_status(session, account, job, JobStatus.APPLIED.value)
        assert job.id not in {j.id for j in self._selection(session, account, prefs).jobs}

    def test_a_dismissed_job_is_not_sent(self, session, account, prefs):
        job = make_job(session)
        user_jobs.set_status(session, account, job, JobStatus.DISMISSED.value)
        assert job.id not in {j.id for j in self._selection(session, account, prefs).jobs}

    def test_a_saved_job_is_still_worth_sending(self, session, account, prefs):
        job = make_job(session)
        user_jobs.set_status(session, account, job, JobStatus.SAVED.value)
        assert job.id in {j.id for j in self._selection(session, account, prefs).jobs}

    def test_a_restored_job_becomes_eligible_again(self, session, account, prefs):
        job = make_job(session)
        user_jobs.set_status(session, account, job, JobStatus.DISMISSED.value)
        user_jobs.set_status(session, account, job, JobStatus.NEW.value)
        assert job.id in {j.id for j in self._selection(session, account, prefs).jobs}


class TestOneUsersDecisionsAreTheirOwn:
    def test_the_other_account_still_sees_the_job(self, client, session):
        mine = auth.create_account(session, email="a@example.com", password="a-good-password")
        theirs = auth.create_account(session, email="b@example.com", password="a-good-password")
        session.flush()
        job = make_job(session)
        user_jobs.set_status(session, mine, job, JobStatus.DISMISSED.value)
        session.commit()

        client.post("/login", data={"email": "b@example.com", "password": "a-good-password"})
        assert job.title in client.get("/").text
        assert theirs.id != mine.id


class TestFilters:
    def test_an_active_filter_is_shown_back_to_the_user(self, signed_in, session):
        make_job(session)
        page = signed_in.get("/?company=NVIDIA").text
        assert "Filtered by" in page and "Clear all filters" in page

    def test_removing_one_filter_keeps_the_others(self):
        filters = JobFilters.from_query({"company": "NVIDIA", "q": "fpga"})
        remaining = filters.without("company")
        assert "company" not in remaining and "q=fpga" in remaining

    def test_the_view_survives_filter_removal(self):
        filters = JobFilters.from_query({"view": "saved", "company": "NVIDIA"})
        assert "view=saved" in filters.without("company")

    def test_an_unfiltered_list_shows_no_chips(self, signed_in, session):
        make_job(session)
        assert "Filtered by" not in signed_in.get("/").text

    def test_the_score_filter_uses_this_users_score(self, session, account):
        """The card shows your score, so the filter has to mean your score too."""
        from app.services.jobs_query import search_jobs

        job = make_job(session)
        state = user_jobs.get_or_create_state(session, account, job)
        state.relevance_score = 20.0
        session.flush()

        found = search_jobs(session, JobFilters.from_query({"min_score": "80"}), account)
        assert job.id not in {j.id for j in found.jobs}


class TestEmptyStates:
    def test_saved_explains_itself(self, signed_in):
        assert "No saved jobs yet" in signed_in.get("/saved").text

    def test_applied_explains_itself(self, signed_in):
        assert "No applications tracked yet" in signed_in.get("/applied").text

    def test_dismissed_explains_itself(self, signed_in):
        assert "Nothing dismissed" in signed_in.get("/dismissed").text

    def test_a_filtered_empty_list_offers_a_way_out(self, signed_in, session):
        make_job(session)
        page = signed_in.get("/?q=nothing-matches-this").text
        assert "No jobs match these filters" in page

    def test_an_all_caught_up_feed_says_so(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, "applied")
        assert "all caught up" in signed_in.get("/").text.lower()


class TestRegressions:
    def test_saving_tracking_details_works(self, signed_in, session):
        """This form raised a TypeError before the user argument was passed."""
        job = make_job(session)
        response = signed_in.post(
            f"/job/{job.id}/application", data={"contact_name": "Ada"}
        )
        assert response.status_code == 303

    def test_a_missing_job_gets_a_real_page(self, signed_in):
        response = signed_in.get("/job/99999")
        assert response.status_code == 404
        assert "can't find that job" in response.text

    def test_bulk_actions_report_what_changed(self, signed_in, session):
        job = make_job(session)
        response = signed_in.post(
            "/jobs/bulk", data={"status": "dismissed", "job_ids": [job.id], "redirect_to": "/"}
        )
        assert "Dismissed%3A%201%20job" in response.headers["location"]

    def test_bulk_with_nothing_selected_says_so(self, signed_in):
        response = signed_in.post("/jobs/bulk", data={"status": "dismissed", "redirect_to": "/"})
        assert "Nothing%20was%20selected" in response.headers["location"]

    def test_the_api_still_lists_every_state(self, signed_in, session):
        """A data endpoint returns the data; only the feed hides handled jobs."""
        job = make_job(session)
        act(signed_in, job, "applied")
        listed = signed_in.get("/api/jobs").json()
        assert job.id in {row["id"] for row in listed["jobs"]}


class TestTomorrowsSearch:
    """The scenario the whole state model exists for.

    Fifteen jobs arrive. You dismiss some, save some, apply to some, and leave
    the rest. Tomorrow the crawler finds all fifteen again -- because they are
    still advertised -- and none of your decisions may be undone by that.
    """

    @pytest.fixture
    def yesterday(self, session, prefs, profile, account, cross_source_duplicates, distinct_jobs):
        from tests.test_persistence import run_pipeline

        run_pipeline(session, cross_source_duplicates + distinct_jobs, prefs, profile)
        jobs = session.query(Job).order_by(Job.id).all()
        user_jobs.set_status(session, account, jobs[0], JobStatus.APPLIED.value)
        user_jobs.set_status(session, account, jobs[1], JobStatus.DISMISSED.value)
        user_jobs.set_status(session, account, jobs[2], JobStatus.SAVED.value)
        session.commit()
        return jobs

    def _rerun(self, session, prefs, profile, raws):
        from tests.test_persistence import run_pipeline

        return run_pipeline(session, raws, prefs, profile, run_id=2)

    def test_the_same_listings_create_no_new_jobs(
        self, session, prefs, profile, yesterday, cross_source_duplicates, distinct_jobs
    ):
        outcome = self._rerun(session, prefs, profile, cross_source_duplicates + distinct_jobs)
        assert outcome.new_jobs == 0

    def test_the_applied_job_stays_applied(
        self, session, prefs, profile, account, yesterday, cross_source_duplicates, distinct_jobs
    ):
        self._rerun(session, prefs, profile, cross_source_duplicates + distinct_jobs)
        state = user_jobs.get_state(session, account, yesterday[0])
        assert state.status == JobStatus.APPLIED.value

    def test_the_dismissed_job_stays_dismissed(
        self, session, prefs, profile, account, yesterday, cross_source_duplicates, distinct_jobs
    ):
        self._rerun(session, prefs, profile, cross_source_duplicates + distinct_jobs)
        state = user_jobs.get_state(session, account, yesterday[1])
        assert state.status == JobStatus.DISMISSED.value

    def test_handled_jobs_stay_out_of_the_feed(
        self, signed_in, session, prefs, profile, yesterday, cross_source_duplicates, distinct_jobs
    ):
        self._rerun(session, prefs, profile, cross_source_duplicates + distinct_jobs)
        feed = signed_in.get("/").text
        assert f'id="job-{yesterday[0].id}"' not in feed
        assert f'id="job-{yesterday[1].id}"' not in feed

    def test_untouched_jobs_are_still_waiting(
        self, signed_in, session, prefs, profile, yesterday, cross_source_duplicates, distinct_jobs
    ):
        self._rerun(session, prefs, profile, cross_source_duplicates + distinct_jobs)
        feed = signed_in.get("/").text
        assert f'id="job-{yesterday[3].id}"' in feed

    def test_tomorrows_email_skips_what_you_handled(
        self, session, prefs, profile, account, yesterday, cross_source_duplicates, distinct_jobs
    ):
        from app.notify.digest import select_jobs_for_digest

        self._rerun(session, prefs, profile, cross_source_duplicates + distinct_jobs)
        prefs.notifications.min_score = 0.0
        sent = {j.id for j in select_jobs_for_digest(session, prefs.notifications, user=account).jobs}
        assert yesterday[0].id not in sent
        assert yesterday[1].id not in sent

    def test_one_posting_on_five_sites_is_one_job_to_deal_with(
        self, session, prefs, profile, cross_source_duplicates
    ):
        from tests.test_persistence import run_pipeline

        run_pipeline(session, cross_source_duplicates, prefs, profile)
        assert session.query(Job).count() == 1


class TestTheTrackerRespondsInPlace:
    """Moving a job along the pipeline must not reload the page or be vague."""

    def _move(self, client, job, status):
        return client.post(
            f"/job/{job.id}/status",
            data={"status": status, "view": "tracker", "redirect_to": "/tracker"},
            headers={"HX-Request": "true"},
        )

    @pytest.fixture
    def applied_job(self, signed_in, session):
        job = make_job(session)
        act(signed_in, job, JobStatus.APPLIED.value)
        return job

    def test_the_board_comes_back_updated(self, signed_in, applied_job):
        response = self._move(signed_in, applied_job, JobStatus.INTERVIEW.value)
        assert 'id="kanban"' in response.text

    def test_the_card_is_in_its_new_column(self, signed_in, applied_job):
        response = self._move(signed_in, applied_job, JobStatus.INTERVIEW.value)
        board = response.text
        interview = board.index("Interview (1)")
        applied = board.index("Applied (0)")
        assert applied < interview
        assert f'id="card-{applied_job.id}"' in board

    def test_the_headline_counts_are_swapped_too(self, signed_in, applied_job):
        """A count that disagrees with the board under it is worse than no count.

        The board's own column headings carry the per-stage numbers, so the only
        counts left to refresh are the nav badges.
        """
        response = self._move(signed_in, applied_job, JobStatus.INTERVIEW.value)
        assert 'id="nav-applied-count"' in response.text
        assert 'hx-swap-oob' in response.text

    def test_each_stage_is_named_in_the_confirmation(self, signed_in, applied_job):
        assert "Moved to Interview" in self._move(
            signed_in, applied_job, JobStatus.INTERVIEW.value
        ).text

    def test_rejection_says_so_rather_than_updated(self, signed_in, applied_job):
        response = self._move(signed_in, applied_job, JobStatus.REJECTED.value)
        assert "Marked as rejected" in response.text
        assert "Updated" not in response.text

    def test_the_move_can_be_undone(self, signed_in, applied_job):
        response = self._move(signed_in, applied_job, JobStatus.REJECTED.value)
        assert "Undo" in response.text
        assert f'value="{JobStatus.APPLIED.value}"' in response.text

    def test_a_rejected_job_can_go_back_in_the_pipeline(self, signed_in, applied_job):
        self._move(signed_in, applied_job, JobStatus.REJECTED.value)
        response = self._move(signed_in, applied_job, JobStatus.APPLIED.value)
        assert "Applied (1)" in response.text

    def test_buttons_disable_themselves_while_working(self, signed_in, applied_job):
        """Guards against a second state change from an impatient second click."""
        assert 'hx-disabled-elt' in signed_in.get("/tracker").text

    def test_a_stage_change_still_works_without_javascript(self, signed_in, applied_job):
        response = signed_in.post(
            f"/job/{applied_job.id}/status",
            data={"status": JobStatus.OFFER.value, "view": "tracker", "redirect_to": "/tracker"},
        )
        assert response.status_code == 303
        assert "Moved%20to%20Offer" in response.headers["location"]


class TestCountersKeepUp:
    """A number describing the previous state is worse than no number."""

    def _hx(self, client, job, status, view="review"):
        return client.post(
            f"/job/{job.id}/status",
            data={"status": status, "view": view, "redirect_to": "/"},
            headers={"HX-Request": "true"},
        )

    def test_the_headline_is_refreshed(self, signed_in, session):
        make_job(session, n=1)
        second = make_job(session, n=2, title="Software Intern")
        response = self._hx(signed_in, second, JobStatus.DISMISSED.value)
        assert 'id="feed-headline"' in response.text
        assert "1 job waiting on a decision" in response.text

    def test_every_nav_count_is_refreshed(self, signed_in, session):
        """A decision moves a job from one tab to another, so both ends move.

        Refreshing only the badge for the page being looked at would leave the
        destination tab quietly describing the state before the click.
        """
        job = make_job(session)
        response = self._hx(signed_in, job, JobStatus.SAVED.value)
        assert 'id="nav-review-count"' in response.text
        assert 'id="nav-saved-count"' in response.text
        assert 'id="nav-applied-count"' in response.text

    def test_the_nav_badge_is_refreshed(self, signed_in, session):
        job = make_job(session)
        response = self._hx(signed_in, job, JobStatus.DISMISSED.value)
        assert 'id="nav-review-count"' in response.text

    def test_the_badge_empties_when_nothing_is_left(self, signed_in, session):
        job = make_job(session)
        response = self._hx(signed_in, job, JobStatus.APPLIED.value)
        badge = response.text.split('id="nav-review-count"')[1].split("</span>")[0]
        assert "0" not in badge

    def test_the_last_job_leaves_an_all_caught_up_headline(self, signed_in, session):
        job = make_job(session)
        assert "all caught up" in self._hx(signed_in, job, JobStatus.APPLIED.value).text.lower()

    def test_saving_moves_a_job_between_counters_without_leaving_the_feed(
        self, signed_in, session
    ):
        job = make_job(session)
        response = self._hx(signed_in, job, JobStatus.SAVED.value)
        # Still one to review (saved jobs stay), and now one saved.
        assert "1 job waiting on a decision" in response.text
        assert f'id="job-{job.id}"' in response.text

    def test_the_list_count_is_refreshed(self, signed_in, session):
        make_job(session, n=1)
        make_job(session, n=2, title="Software Intern")
        response = self._hx(signed_in, make_job(session, n=3, title="ML Intern"),
                            JobStatus.DISMISSED.value)
        assert 'id="list-count"' in response.text
        assert "2 jobs" in response.text

    def test_the_refreshed_count_respects_active_filters(self, signed_in, session):
        """Counting the unfiltered list would swap one wrong number for another."""
        make_job(session, n=1)
        other = make_job(session, n=2, title="Software Intern")
        response = signed_in.post(
            f"/job/{other.id}/status",
            data={
                "status": JobStatus.SAVED.value,
                "view": "review",
                "redirect_to": "/?q=software",
            },
            headers={"HX-Request": "true"},
        )
        # Only the one job matches "software", saved or not.
        assert "1 job" in response.text


class TestSettingsOwnsSponsorshipAndLocations:
    """One question, one control, one answer.

    Sponsorship and preferred locations used to be editable on both Settings
    and the profile page, backed by different stores. The two could disagree,
    and nothing on either page said which one the search actually used. Settings
    owns them now; these tests pin down the two ways that could go wrong.
    """

    def _stored_profile(self, session, account):
        from app.services.preferences import load_profile

        return load_profile(session, user=account)

    def _given_profile(self, session, account, **fields):
        from app.schemas.profile import CandidateProfileData
        from app.services.preferences import save_profile

        data = self._stored_profile(session, account).model_dump()
        data.update(fields)
        save_profile(session, CandidateProfileData.model_validate(data), user=account)
        session.flush()

    def test_saving_the_profile_leaves_what_settings_owns_alone(
        self, signed_in, session, account
    ):
        """The profile form no longer carries these, so it must not rewrite them.

        An unchecked box and an absent box are identical in a POST body. Before
        this, saving an unrelated field on the profile page silently cleared a
        sponsorship answer given on Settings.
        """
        self._given_profile(
            session, account, requires_sponsorship=True, preferred_locations=["Boston"]
        )

        signed_in.post("/profile", data={"school": "CMU"})

        after = self._stored_profile(session, account)
        assert after.school == "CMU", "the edit that was submitted should still apply"
        assert after.requires_sponsorship is True
        assert after.preferred_locations == ["Boston"]

    def test_settings_keeps_the_profile_copy_in_step(self, signed_in, session, account):
        """The ranking engine ORs the two copies together.

        So a stale `true` left in the profile would keep the search treating
        sponsorship as required after the box on Settings was cleared -- with no
        control anywhere on screen able to undo it.
        """
        self._given_profile(session, account, requires_sponsorship=True)

        signed_in.post("/settings", data={"notification_provider": "file"})

        assert self._stored_profile(session, account).requires_sponsorship is False

    def test_ticking_the_box_on_settings_reaches_the_profile_copy(
        self, signed_in, session, account
    ):
        self._given_profile(session, account, requires_sponsorship=False)

        signed_in.post(
            "/settings",
            data={"notification_provider": "file", "requires_sponsorship": "on"},
        )

        assert self._stored_profile(session, account).requires_sponsorship is True
