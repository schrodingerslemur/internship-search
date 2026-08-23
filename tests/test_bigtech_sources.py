"""The three employers that had no board at all.

Google, Microsoft and Apple between them accounted for 30 jobs while NVIDIA
alone had 37, and every one of those 30 arrived second-hand through curated
GitHub lists. None of them is on an ATS this project already crawled, and none
answers a guessable endpoint, so each needed finding.

What they can give differs enough to be worth pinning separately: Microsoft and
Apple serve real APIs, while Google serves only a sitemap and therefore only a
title and a link.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.sources.ats.bigtech import AppleJobsSource, GoogleCareersSource
from app.sources.ats.eightfold import EightfoldSource
from app.sources.base import SearchQuery, SourceContext, first_text
from app.sources.http import HttpClient


@pytest.fixture
async def http():
    async with HttpClient(cache_ttl=0, rate_limit_delay=0, max_retries=1) as client:
        yield client


def ctx_for(http, **kwargs) -> SourceContext:
    kwargs.setdefault("queries", [SearchQuery(text="hardware intern")])
    return SourceContext(http=http, **kwargs)


class TestEightfold:
    """Microsoft's board. The host every guide names now serves a certificate
    for another domain and 404s regardless, and the careers page has moved to a
    platform speaking a different API."""

    HOST = "apply.careers.microsoft.com"

    def _row(self, pid, name, **over):
        row = {
            "id": pid,
            "displayJobId": "200045457",
            "name": name,
            "locations": ["United States, Washington, Redmond"],
            "department": "Silicon",
            "positionUrl": f"/careers/job/{pid}",
            "postedTs": 1787246759,
            "workLocationOption": "onsite",
        }
        row.update(over)
        return row

    #: Distinguishes "caller wants the default detail" from "caller wants an
    #: empty one" -- an empty dict is falsy, so `detail or default` silently
    #: hands back the default and the test passes for the wrong reason.
    _DEFAULT = object()

    def _mock(self, rows, detail=_DEFAULT):
        respx.get(url__regex=rf"https://{self.HOST}/api/pcsx/search.*").mock(
            return_value=httpx.Response(
                200, json={"status": 200, "data": {"positions": rows, "count": len(rows)}}
            )
        )
        respx.get(url__regex=rf"https://{self.HOST}/api/pcsx/position_details.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": 200,
                    "data": {
                        "jobDescription": "<p>Design <b>RTL</b> in SystemVerilog.</p>",
                        # Sent as a list. Passing it straight to the schema is
                        # what had silently killed Workable.
                        "efcustomTextEmploymentType": ["Internship"],
                    }
                    if detail is self._DEFAULT
                    else detail,
                },
            )
        )

    def _board(self):
        return {
            "provider": "eightfold",
            "board_token": "microsoft",
            "extra": {"host": self.HOST, "domain": "microsoft.com"},
        }

    @respx.mock
    async def test_parses_listings_and_hydrates_descriptions(self, http):
        self._mock([self._row(1, "Hardware Engineering Intern")])
        outcome = await EightfoldSource().run(ctx_for(http, boards=[self._board()]))
        assert outcome.status == "ok"
        job = outcome.jobs[0]
        assert job.title == "Hardware Engineering Intern"
        assert "SystemVerilog" in job.description
        assert "<b>" not in job.description
        assert job.location == "United States, Washington, Redmond"

    @respx.mock
    async def test_an_employment_type_sent_as_a_list_does_not_sink_the_board(self, http):
        self._mock([self._row(1, "Hardware Engineering Intern")])
        outcome = await EightfoldSource().run(ctx_for(http, boards=[self._board()]))
        assert outcome.status == "ok"
        assert outcome.jobs[0].employment_type == "Internship"

    @respx.mock
    async def test_the_requisition_id_is_the_one_a_human_sees(self, http):
        """``id`` is the platform's internal key and appears nowhere on the
        posting; ``displayJobId`` is the number printed on it."""
        self._mock([self._row(1970393556944773, "Research Intern")])
        outcome = await EightfoldSource().run(ctx_for(http, boards=[self._board()]))
        assert outcome.jobs[0].requisition_id == "200045457"

    @respx.mock
    async def test_title_gate_filters_at_ingestion(self, http):
        self._mock(
            [self._row(1, "Principal Program Manager"), self._row(2, "Research Intern")]
        )
        outcome = await EightfoldSource().run(
            ctx_for(
                http,
                boards=[self._board()],
                title_gate=lambda t: "intern" in t.lower(),
            )
        )
        assert {j.title for j in outcome.jobs} == {"Research Intern"}

    @respx.mock
    async def test_a_posting_with_no_description_is_still_returned(self, http):
        """Hydration is budgeted, so some postings arrive without one. That is
        a thinner listing, not a reason to discard a real job."""
        self._mock([self._row(1, "Research Intern")], detail={})
        outcome = await EightfoldSource().run(ctx_for(http, boards=[self._board()]))
        assert len(outcome.jobs) == 1
        assert outcome.jobs[0].description is None

    @respx.mock
    async def test_missing_host_metadata_fails_that_board_only(self, http):
        outcome = await EightfoldSource().run(
            ctx_for(http, boards=[{"provider": "eightfold", "board_token": "x", "extra": {}}])
        )
        assert outcome.status == "failed"


class TestAppleJobs:
    URL = "https://jobs.apple.com/api/v1/search"

    def _row(self, pid, title, **over):
        row = {
            "positionId": pid,
            "reqId": f"R-{pid}",
            "postingTitle": title,
            "transformedPostingTitle": title.lower().replace(" ", "-"),
            "jobSummary": "<p>Build <b>silicon</b> validation tooling.</p>",
            "locations": [
                {
                    "city": "Cupertino",
                    "stateProvince": "California",
                    "countryName": "United States",
                }
            ],
            "team": {"teamName": "Hardware"},
            "postDateInGMT": "2026-08-20T05:23:26Z",
            "homeOffice": False,
        }
        row.update(over)
        return row

    def _board(self):
        return {"provider": "apple", "board_token": "apple"}

    @respx.mock
    async def test_parses_listings_with_summaries_inline(self, http):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "res": {
                        "searchResults": [self._row("200", "Silicon Validation Intern")],
                        "totalRecords": 1,
                    }
                },
            )
        )
        outcome = await AppleJobsSource().run(ctx_for(http, boards=[self._board()]))
        assert outcome.status == "ok"
        job = outcome.jobs[0]
        assert job.title == "Silicon Validation Intern"
        assert "silicon" in job.description
        assert "<b>" not in job.description
        assert job.location == "Cupertino, California, United States"
        assert "/details/200/" in job.url

    @respx.mock
    async def test_the_full_filter_shape_is_sent(self, http):
        """Apple accepts ``filters: {}`` and silently returns zero results,
        which is an expensive way to discover the body was wrong."""
        seen: list[dict] = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"res": {"searchResults": [], "totalRecords": 0}})

        respx.post(self.URL).mock(side_effect=handler)
        await AppleJobsSource().run(ctx_for(http, boards=[self._board()]))
        assert seen
        assert "postingpostLocation" in seen[0]["filters"]
        assert "format" in seen[0]

    @respx.mock
    async def test_the_same_posting_across_terms_is_returned_once(self, http):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={"res": {"searchResults": [self._row("7", "Hardware Co-op")], "totalRecords": 1}},
            )
        )
        outcome = await AppleJobsSource().run(ctx_for(http, boards=[self._board()]))
        assert len(outcome.jobs) == 1

    @respx.mock
    async def test_title_gate_filters_at_ingestion(self, http):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "res": {
                        "searchResults": [
                            self._row("1", "Senior Silicon Architect"),
                            self._row("2", "Silicon Validation Intern"),
                        ],
                        "totalRecords": 2,
                    }
                },
            )
        )
        outcome = await AppleJobsSource().run(
            ctx_for(http, boards=[self._board()], title_gate=lambda t: "intern" in t.lower())
        )
        assert {j.title for j in outcome.jobs} == {"Silicon Validation Intern"}


class TestGoogleCareers:
    """Google publishes no API and no structured data — only a sitemap. These
    postings carry a title and a link and nothing else, deliberately: inventing
    a location or a date to fill the row would be worse than leaving the scorer
    to mark them unmeasured."""

    SITEMAP = "https://careers.google.com/jobs/sitemap"

    def _xml(self, *paths):
        locs = "".join(f"<loc>{p}</loc>" for p in paths)
        return '<?xml version="1.0"?><urlset>' + locs + "</urlset>"

    def _board(self):
        return {"provider": "google_careers", "board_token": "google"}

    @respx.mock
    async def test_reads_titles_out_of_the_sitemap_slugs(self, http):
        respx.get(self.SITEMAP).mock(
            return_value=httpx.Response(
                200,
                text=self._xml(
                    "https://careers.google.com/jobs/results/123-student-researcher/",
                    "https://careers.google.com/jobs/results/456-hardware-engineering-intern/",
                ),
            )
        )
        outcome = await GoogleCareersSource().run(ctx_for(http, boards=[self._board()]))
        assert outcome.status == "ok"
        assert {j.title for j in outcome.jobs} == {
            "Student Researcher",
            "Hardware Engineering Intern",
        }
        assert all(j.description is None for j in outcome.jobs)

    @respx.mock
    async def test_non_job_urls_are_ignored(self, http):
        respx.get(self.SITEMAP).mock(
            return_value=httpx.Response(
                200,
                text=self._xml(
                    "https://careers.google.com/benefits/",
                    "https://careers.google.com/jobs/results/9-rtl-design-intern/",
                ),
            )
        )
        outcome = await GoogleCareersSource().run(ctx_for(http, boards=[self._board()]))
        assert [j.title for j in outcome.jobs] == ["Rtl Design Intern"]

    @respx.mock
    async def test_the_same_job_listed_twice_is_returned_once(self, http):
        respx.get(self.SITEMAP).mock(
            return_value=httpx.Response(
                200,
                text=self._xml(
                    "https://careers.google.com/jobs/results/9-rtl-design-intern/",
                    "https://careers.google.com/jobs/results/9-rtl-design-intern/",
                ),
            )
        )
        outcome = await GoogleCareersSource().run(ctx_for(http, boards=[self._board()]))
        assert len(outcome.jobs) == 1

    @respx.mock
    async def test_the_title_gate_still_applies(self, http):
        respx.get(self.SITEMAP).mock(
            return_value=httpx.Response(
                200,
                text=self._xml(
                    "https://careers.google.com/jobs/results/1-senior-staff-engineer/",
                    "https://careers.google.com/jobs/results/2-student-researcher/",
                ),
            )
        )
        outcome = await GoogleCareersSource().run(
            ctx_for(
                http,
                boards=[self._board()],
                title_gate=lambda t: "student" in t.lower(),
            )
        )
        assert [j.title for j in outcome.jobs] == ["Student Researcher"]

    @respx.mock
    async def test_a_missing_sitemap_fails_the_source_not_the_run(self, http):
        respx.get(self.SITEMAP).mock(return_value=httpx.Response(503))
        outcome = await GoogleCareersSource().run(ctx_for(http, boards=[self._board()]))
        assert outcome.status == "failed"


class TestSharedLabelParsing:
    """Three providers send a single-valued field as a list, and two of them
    cost an entire source before the helper was shared."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (["Silicon Engineering"], "Silicon Engineering"),
            ([], None),
            (False, None),
            (None, None),
            ("Hardware", "Hardware"),
            ([None, "", "Verification", "Silicon"], "Verification"),
            (["  padded  "], "padded"),
        ],
    )
    def test_one_label_whatever_shape_it_arrived_in(self, value, expected):
        assert first_text(value) == expected
