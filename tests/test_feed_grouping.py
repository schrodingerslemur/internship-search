"""Collapsing same-employer, same-title postings in the feed.

These postings are not duplicates. GE Healthcare advertises eleven distinct
"Graduate Engineer Trainee" requisitions; Copart eleven "Software Engineering
Intern". They have separate requisition ids and separate application links, so
merging them in the database would delete real jobs somebody might want to
apply to.

Everything here is therefore about *presentation*: one card per role, every
requisition still present, still linked, still individually actionable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.models import Company, Job
from app.services import auth
from app.services.jobs_query import group_jobs


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


def make_job(
    session,
    *,
    n: int,
    title: str = "Graduate Engineer Trainee",
    company: str = "GE Healthcare",
    company_id: int | None = None,
    location: str = "Milwaukee, WI",
    requisition_id: str | None = None,
) -> Job:
    job = Job(
        canonical_job_id=f"job-{n}",
        fingerprint=f"fp-{n}",
        company_name=company,
        company_id=company_id,
        title=title,
        location_raw=location,
        application_url=f"https://example.com/apply/{n}",
        relevance_score=90.0 - n,
        priority="strong_match",
        requisition_id=requisition_id,
    )
    session.add(job)
    session.flush()
    return job


@pytest.fixture
def employer(session):
    company = Company(name="GE Healthcare", slug="ge-healthcare")
    session.add(company)
    session.flush()
    return company


class TestGroupingTheList:
    def test_one_posting_is_a_group_of_one(self, session, employer):
        job = make_job(session, n=1, company_id=employer.id)
        groups = group_jobs([job])
        assert len(groups) == 1
        assert not groups[0].is_group
        assert groups[0].count == 1

    def test_same_employer_and_title_collapse_together(self, session, employer):
        jobs = [make_job(session, n=i, company_id=employer.id) for i in range(3)]
        groups = group_jobs(jobs)
        assert len(groups) == 1
        assert groups[0].count == 3
        assert groups[0].lead is jobs[0]

    def test_a_different_title_is_a_different_group(self, session, employer):
        a = make_job(session, n=1, company_id=employer.id)
        b = make_job(session, n=2, company_id=employer.id, title="Software Intern")
        assert len(group_jobs([a, b])) == 2

    def test_a_different_employer_is_a_different_group(self, session, employer):
        other = Company(name="Copart", slug="copart")
        session.add(other)
        session.flush()
        a = make_job(session, n=1, company_id=employer.id)
        b = make_job(session, n=2, company_id=other.id, company="Copart")
        assert len(group_jobs([a, b])) == 2

    def test_titles_group_regardless_of_case_and_padding(self, session, employer):
        a = make_job(session, n=1, company_id=employer.id)
        b = make_job(
            session, n=2, company_id=employer.id, title="  graduate engineer trainee "
        )
        assert len(group_jobs([a, b])) == 1

    def test_postings_from_an_unregistered_employer_still_group(self, session):
        """No company row yet -- the name has to carry the grouping."""
        jobs = [make_job(session, n=i, company_id=None) for i in range(2)]
        assert len(group_jobs(jobs)) == 1

    def test_grouping_never_loses_a_posting(self, session, employer):
        jobs = [make_job(session, n=i, company_id=employer.id) for i in range(5)]
        jobs.append(make_job(session, n=9, company_id=employer.id, title="Other"))
        seen = [j for g in group_jobs(jobs) for j in (g.lead, *g.others)]
        assert sorted(j.id for j in seen) == sorted(j.id for j in jobs)

    def test_order_is_preserved_so_collapsing_promotes_nothing(self, session, employer):
        first = make_job(session, n=1, company_id=employer.id, title="Aardvark Intern")
        dupes = [make_job(session, n=i, company_id=employer.id) for i in (2, 3)]
        groups = group_jobs([first, *dupes])
        assert groups[0].lead is first
        assert groups[1].lead is dupes[0]

    def test_the_group_lists_its_distinct_locations(self, session, employer):
        a = make_job(session, n=1, company_id=employer.id, location="Austin, TX")
        b = make_job(session, n=2, company_id=employer.id, location="Boston, MA")
        c = make_job(session, n=3, company_id=employer.id, location="Austin, TX")
        assert group_jobs([a, b, c]).pop().locations == ["Austin, TX", "Boston, MA"]


class TestTheFeedShowsOneCardPerRole:
    def test_repeated_postings_produce_one_card(self, signed_in, session, employer):
        for i in range(4):
            make_job(session, n=i, company_id=employer.id)
        page = signed_in.get("/").text
        assert page.count('class="job"') == 1

    def test_the_extra_openings_are_announced(self, signed_in, session, employer):
        for i in range(4):
            make_job(session, n=i, company_id=employer.id)
        assert "3 more openings" in signed_in.get("/").text

    def test_a_single_posting_gets_no_expander(self, signed_in, session, employer):
        make_job(session, n=1, company_id=employer.id)
        assert "more opening" not in signed_in.get("/").text

    def test_every_requisition_keeps_its_own_apply_link(self, signed_in, session, employer):
        jobs = [make_job(session, n=i, company_id=employer.id) for i in range(3)]
        page = signed_in.get("/").text
        for job in jobs:
            assert job.application_url in page

    def test_every_requisition_keeps_its_own_detail_link(self, signed_in, session, employer):
        jobs = [make_job(session, n=i, company_id=employer.id) for i in range(3)]
        page = signed_in.get("/").text
        for job in jobs:
            assert f"/job/{job.id}" in page

    def test_requisition_ids_are_shown_when_known(self, signed_in, session, employer):
        make_job(session, n=1, company_id=employer.id, requisition_id="R-100")
        make_job(session, n=2, company_id=employer.id, requisition_id="R-200")
        assert "R-200" in signed_in.get("/").text

    def test_the_count_beside_the_list_still_counts_jobs(self, signed_in, session, employer):
        """Grouping is presentational: the user is still choosing among jobs."""
        for i in range(4):
            make_job(session, n=i, company_id=employer.id)
        assert "4" in signed_in.get("/").text

    def test_a_bulk_decision_reaches_every_requisition_in_the_group(
        self, signed_in, session, employer
    ):
        """The card stands for the whole role. Dismissing it must not clear one
        opening and leave three identical ones in the feed."""
        jobs = [make_job(session, n=i, company_id=employer.id) for i in range(4)]
        signed_in.post(
            "/jobs/bulk",
            data={"status": "dismissed", "job_ids": [str(j.id) for j in jobs],
                  "redirect_to": "/", "view": "review"},
        )
        page = signed_in.get("/").text
        assert "Graduate Engineer Trainee" not in page

    def test_the_siblings_are_selectable_alongside_the_card(
        self, signed_in, session, employer
    ):
        """They ride along with the lead's checkbox rather than being clicked,
        so they have to be present in the form to be submitted at all."""
        jobs = [make_job(session, n=i, company_id=employer.id) for i in range(3)]
        page = signed_in.get("/").text
        for sibling in jobs[1:]:
            assert f'name="job_ids" value="{sibling.id}"' in page
        assert 'class="job-pick group-pick"' in page
