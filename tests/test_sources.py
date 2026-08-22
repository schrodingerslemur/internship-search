"""Source adapters, tested against recorded payload shapes (no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.models.base import SourceKind
from app.sources.ats.ashby import AshbySource
from app.sources.ats.greenhouse import GreenhouseSource
from app.sources.ats.lever import LeverSource
from app.sources.ats.misc_ats import WorkableSource
from app.sources.ats.phenom import PhenomSource
from app.sources.ats.workday import WorkdaySource
from app.sources.base import JobSource, SearchQuery, SourceContext
from app.sources.http import FetchError, HttpClient
from app.sources.lists.github_lists import GithubInternshipLists
from app.sources.registry import ALL_SOURCE_CLASSES, board_providers, build_sources, source_catalog


@pytest.fixture
async def http():
    async with HttpClient(cache_ttl=0, rate_limit_delay=0, max_retries=1) as client:
        yield client


def ctx_for(http, **kwargs) -> SourceContext:
    kwargs.setdefault("queries", [SearchQuery(text="fpga intern")])
    return SourceContext(http=http, **kwargs)


GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 12345,
            "title": "FPGA Design Intern",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/12345",
            "content": "&lt;p&gt;Build FPGA systems with SystemVerilog.&lt;/p&gt;",
            "location": {"name": "Santa Clara, CA"},
            "offices": [{"name": "Santa Clara, CA"}, {"name": "Austin, TX"}],
            "first_published": "2026-08-15T10:00:00Z",
            "updated_at": "2026-08-16T10:00:00Z",
            "requisition_id": "REQ-9",
            "departments": [{"name": "Silicon"}],
            "metadata": [],
        },
        {
            "id": 999,
            "title": "Account Executive",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/999",
            "content": "Sell things.",
            "location": {"name": "New York, NY"},
            "offices": [],
            "metadata": [],
        },
    ]
}

LEVER_PAYLOAD = [
    {
        "id": "abc-123",
        "text": "Hardware Engineer Intern",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
        "categories": {"location": "Boston, MA", "commitment": "Intern", "team": "Hardware"},
        "descriptionPlain": "Design hardware. Verilog required.",
        "lists": [{"text": "Requirements", "content": "<li>Verilog</li>"}],
        "createdAt": 1755244800000,
        "workplaceType": "onsite",
    }
]

ASHBY_PAYLOAD = {
    "jobs": [
        {
            "id": "uuid-1",
            "title": "ASIC Design Intern",
            "jobUrl": "https://jobs.ashbyhq.com/acme/uuid-1",
            "applyUrl": "https://jobs.ashbyhq.com/acme/uuid-1/application",
            "location": "Remote",
            "descriptionHtml": "<p>ASIC work</p>",
            "employmentType": "Intern",
            "isRemote": True,
            "publishedAt": "2026-08-14T00:00:00Z",
            "department": "Silicon",
            "secondaryLocations": [{"location": "Austin, TX"}],
            "compensation": {"compensationTierSummary": "$40 - $60/hr"},
        }
    ]
}


class TestGreenhouse:
    @respx.mock
    async def test_parses_listings(self, http):
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD)
        )
        source = GreenhouseSource()
        outcome = await source.run(
            ctx_for(http, boards=[{"provider": "greenhouse", "board_token": "acme"}])
        )
        assert outcome.status == "ok"
        job = outcome.jobs[0]
        assert job.title == "FPGA Design Intern"
        assert job.source_job_id == "acme:12345"
        assert job.requisition_id == "REQ-9"
        assert "Austin, TX" in job.locations

    @respx.mock
    async def test_title_gate_filters_at_ingestion(self, http):
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD)
        )
        context = ctx_for(
            http,
            boards=[{"provider": "greenhouse", "board_token": "acme"}],
            title_gate=lambda t: "intern" in t.lower(),
        )
        outcome = await GreenhouseSource().run(context)
        assert len(outcome.jobs) == 1
        assert outcome.jobs[0].title == "FPGA Design Intern"

    @respx.mock
    async def test_board_failure_is_reported_not_raised(self, http):
        respx.get("https://boards-api.greenhouse.io/v1/boards/gone/jobs").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        outcome = await GreenhouseSource().run(
            ctx_for(http, boards=[{"provider": "greenhouse", "board_token": "gone"}])
        )
        assert outcome.status == "failed"
        assert outcome.jobs == []

    @respx.mock
    async def test_one_bad_board_does_not_sink_the_others(self, http):
        respx.get("https://boards-api.greenhouse.io/v1/boards/good/jobs").mock(
            return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD)
        )
        respx.get("https://boards-api.greenhouse.io/v1/boards/bad/jobs").mock(
            return_value=httpx.Response(500)
        )
        outcome = await GreenhouseSource().run(
            ctx_for(http, boards=[
                {"provider": "greenhouse", "board_token": "good"},
                {"provider": "greenhouse", "board_token": "bad"},
            ])
        )
        assert outcome.jobs
        assert outcome.status == "degraded"
        assert outcome.sub_targets_successful == 1
        assert outcome.sub_targets_attempted == 2


class TestLeverAndAshby:
    @respx.mock
    async def test_lever(self, http):
        respx.get("https://api.lever.co/v0/postings/acme").mock(
            return_value=httpx.Response(200, json=LEVER_PAYLOAD)
        )
        outcome = await LeverSource().run(
            ctx_for(http, boards=[{"provider": "lever", "board_token": "acme"}])
        )
        job = outcome.jobs[0]
        assert job.title == "Hardware Engineer Intern"
        assert job.location == "Boston, MA"
        assert job.employment_type == "Intern"

    @respx.mock
    async def test_ashby(self, http):
        respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
            return_value=httpx.Response(200, json=ASHBY_PAYLOAD)
        )
        outcome = await AshbySource().run(
            ctx_for(http, boards=[{"provider": "ashby", "board_token": "acme"}])
        )
        job = outcome.jobs[0]
        assert job.title == "ASIC Design Intern"
        assert job.remote_status == "remote"
        assert job.salary_raw == "$40 - $60/hr"


class TestWorkday:
    BASE = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers"

    @respx.mock
    async def test_uses_intern_facet_when_available(self, http):
        facet_response = {
            "total": 500,
            "jobPostings": [],
            "facets": [
                {
                    "facetParameter": "workerSubType",
                    "descriptor": "Job Type",
                    "values": [
                        {"id": "abc", "descriptor": "Intern (Fixed Term)", "count": 2},
                        {"id": "xyz", "descriptor": "Regular", "count": 498},
                    ],
                }
            ],
        }
        filtered = {
            "total": 2,
            "jobPostings": [
                {"title": "FPGA Intern", "externalPath": "/job/US-CA/FPGA-Intern_R-100",
                 "locationsText": "Santa Clara", "bulletFields": ["R-100"],
                 "postedOn": "Posted 2 Days Ago"},
                {"title": "RTL Intern", "externalPath": "/job/US-TX/RTL-Intern_R-101",
                 "locationsText": "Austin", "bulletFields": ["R-101"],
                 "postedOn": "Posted Today"},
            ],
        }
        calls = {"n": 0}

        def responder(request):
            import json

            body = json.loads(request.content)
            if not body.get("appliedFacets"):
                return httpx.Response(200, json=facet_response)
            calls["n"] += 1
            return httpx.Response(200, json=filtered)

        respx.post(f"{self.BASE}/jobs").mock(side_effect=responder)
        respx.get(url__regex=rf"{self.BASE}/job/.*").mock(
            return_value=httpx.Response(200, json={"jobPostingInfo": {
                "jobDescription": "<p>FPGA work</p>", "externalUrl": "https://acme.com/job",
                "startDate": "2026-08-16"}})
        )

        outcome = await WorkdaySource().run(
            ctx_for(
                http,
                boards=[{"provider": "workday", "board_token": "acme",
                         "extra": {"host": "acme.wd5.myworkdayjobs.com", "site": "Careers"}}],
                title_gate=lambda t: "intern" in t.lower(),
            )
        )
        assert calls["n"] >= 1, "facet filter should have been applied"
        assert {j.title for j in outcome.jobs} == {"FPGA Intern", "RTL Intern"}
        assert outcome.jobs[0].requisition_id in {"R-100", "R-101"}

    @respx.mock
    async def test_missing_host_metadata_fails_that_board_only(self, http):
        outcome = await WorkdaySource().run(
            ctx_for(http, boards=[{"provider": "workday", "board_token": "acme", "extra": {}}])
        )
        assert outcome.status == "failed"


class TestWorkable:
    LIST = "https://apply.workable.com/api/v1/accounts/acme/jobs"

    def _row(self, **over):
        row = {
            "shortcode": "ABC123",
            "title": "FPGA Design Intern",
            "url": "https://apply.workable.com/acme/j/ABC123/",
            "description": "Verilog work.",
            "city": "Austin",
            "state": "TX",
            "country": "US",
            "department": ["Silicon Engineering"],
        }
        row.update(over)
        return row

    @respx.mock
    async def test_a_department_sent_as_a_list_does_not_sink_the_board(self, http):
        """Workable reports ``department`` as a list. The schema wants a
        string, and passing the list through raised inside make_job -- which
        failed the posting, then the board, then every board, then the whole
        source. An optional label must never cost a source."""
        respx.post(self.LIST).mock(
            return_value=httpx.Response(200, json={"results": [
                self._row(),
                self._row(shortcode="D4", title="RTL Intern", department=[]),
                self._row(shortcode="D5", title="ASIC Intern", department="Hardware"),
            ]})
        )
        outcome = await WorkableSource().run(
            ctx_for(http, boards=[{"provider": "workable", "board_token": "acme"}])
        )
        assert outcome.status == "ok"
        by_title = {j.title: j for j in outcome.jobs}
        assert by_title["FPGA Design Intern"].department == "Silicon Engineering"
        assert by_title["RTL Intern"].department is None
        assert by_title["ASIC Intern"].department == "Hardware"

    @respx.mock
    async def test_the_first_named_department_wins(self, http):
        respx.post(self.LIST).mock(
            return_value=httpx.Response(200, json={"results": [
                self._row(department=[None, "", "Verification", "Silicon"]),
            ]})
        )
        outcome = await WorkableSource().run(
            ctx_for(http, boards=[{"provider": "workable", "board_token": "acme"}])
        )
        assert outcome.jobs[0].department == "Verification"


class TestPhenom:
    """AMD is why this source exists: a preferred employer with 44 open
    internships that reported exactly one, because it is not on Workday."""

    HOST = "careers.amd.com"

    def _row(self, req_id: str, title: str, **over):
        data = {
            "req_id": req_id,
            "title": title,
            "description": "<p>Design <strong>RTL</strong> in SystemVerilog.</p>",
            "qualifications": "<li>Verilog</li>",
            "full_location": "Austin, Texas",
            "location_name": "US,TX,Austin",
            "apply_url": f"https://careers-amd.icims.com/jobs/{req_id}/login",
            "employment_type": "INTERN",
            "posted_date": "2026-08-20T17:01:00+0000",
            "hiring_organization": "AMD",
            "category": [" Engineering"],
            # Phenom sends a bare ``false`` here when a posting has one
            # location, so this field's type cannot be trusted.
            "multipleLocations": False,
        }
        data.update(over)
        return {"data": data}

    @respx.mock
    async def test_parses_listings_with_inline_descriptions(self, http):
        respx.get(url__regex=rf"https://{self.HOST}/api/jobs.*").mock(
            return_value=httpx.Response(
                200, json={"jobs": [self._row("90807", "2027 Firmware Engineering Intern")]}
            )
        )
        outcome = await PhenomSource().run(
            ctx_for(http, boards=[{"provider": "phenom", "board_token": "amd",
                                   "extra": {"host": self.HOST}}])
        )
        assert outcome.status == "ok"
        job = outcome.jobs[0]
        assert job.title == "2027 Firmware Engineering Intern"
        assert job.requisition_id == "90807"
        assert job.company == "AMD"
        # The description arrives with the list, so no hydration step is needed.
        assert "SystemVerilog" in job.description
        assert "<strong>" not in job.description

    @respx.mock
    async def test_a_bool_in_multiple_locations_does_not_crash_the_board(self, http):
        """The field is a list when there are several and ``false`` when there
        is one; iterating it blindly took the whole board down."""
        respx.get(url__regex=rf"https://{self.HOST}/api/jobs.*").mock(
            return_value=httpx.Response(
                200,
                json={"jobs": [
                    self._row("1", "Intern A", multipleLocations=False),
                    self._row("2", "Intern B", multipleLocations=["Austin", "San Jose"]),
                ]},
            )
        )
        outcome = await PhenomSource().run(
            ctx_for(http, boards=[{"provider": "phenom", "board_token": "amd",
                                   "extra": {"host": self.HOST}}])
        )
        assert outcome.status == "ok"
        by_title = {j.title: j for j in outcome.jobs}
        assert by_title["Intern A"].locations == []
        assert by_title["Intern B"].locations == ["Austin", "San Jose"]

    @respx.mock
    async def test_the_same_posting_across_search_terms_is_returned_once(self, http):
        """"intern" and "co-op" overlap heavily; the union must not duplicate."""
        respx.get(url__regex=rf"https://{self.HOST}/api/jobs.*").mock(
            return_value=httpx.Response(
                200, json={"jobs": [self._row("55", "Co-op/Intern, Silicon")]}
            )
        )
        outcome = await PhenomSource().run(
            ctx_for(http, boards=[{"provider": "phenom", "board_token": "amd",
                                   "extra": {"host": self.HOST}}])
        )
        assert len(outcome.jobs) == 1

    @respx.mock
    async def test_title_gate_filters_at_ingestion(self, http):
        respx.get(url__regex=rf"https://{self.HOST}/api/jobs.*").mock(
            return_value=httpx.Response(
                200,
                json={"jobs": [
                    self._row("1", "Product Development Engineer"),
                    self._row("2", "Summer 2027 Undergrad Intern"),
                ]},
            )
        )
        outcome = await PhenomSource().run(
            ctx_for(http,
                    boards=[{"provider": "phenom", "board_token": "amd",
                             "extra": {"host": self.HOST}}],
                    title_gate=lambda t: "intern" in t.lower())
        )
        assert {j.title for j in outcome.jobs} == {"Summer 2027 Undergrad Intern"}

    @respx.mock
    async def test_missing_host_metadata_fails_that_board_only(self, http):
        outcome = await PhenomSource().run(
            ctx_for(http, boards=[{"provider": "phenom", "board_token": "amd", "extra": {}}])
        )
        assert outcome.status == "failed"


class TestCuratedLists:
    @respx.mock
    async def test_parses_and_respects_active_flag(self, http):
        payload = [
            {"id": "1", "company_name": "Acme", "title": "FPGA Intern", "active": True,
             "url": "https://job-boards.greenhouse.io/acme/jobs/1",
             "locations": ["Austin, TX"], "terms": ["Summer 2026"],
             "sponsorship": "Does Not Offer Sponsorship", "date_posted": 1755244800},
            {"id": "2", "company_name": "Dead Co", "title": "Old Intern", "active": False,
             "url": "https://example.com/2", "locations": []},
        ]
        for _, url in __import__(
            "app.sources.lists.github_lists", fromlist=["LIST_SOURCES"]
        ).LIST_SOURCES:
            respx.get(url).mock(return_value=httpx.Response(200, json=payload))

        outcome = await GithubInternshipLists().run(ctx_for(http))
        titles = [j.title for j in outcome.jobs]
        assert "FPGA Intern" in titles
        assert "Old Intern" not in titles

    @respx.mock
    async def test_sponsorship_code_becomes_evidence_not_a_verdict(self, http):
        payload = [{"id": "1", "company_name": "Acme", "title": "FPGA Intern", "active": True,
                    "url": "https://job-boards.greenhouse.io/acme/jobs/1", "locations": [],
                    "sponsorship": "Other"}]
        for _, url in __import__(
            "app.sources.lists.github_lists", fromlist=["LIST_SOURCES"]
        ).LIST_SOURCES:
            respx.get(url).mock(return_value=httpx.Response(200, json=payload))
        outcome = await GithubInternshipLists().run(ctx_for(http))
        # "Other" is not a claim in either direction.
        assert outcome.jobs[0].sponsorship_hint is None


class TestCredentialGating:
    async def test_source_without_credentials_reports_unconfigured(self, http):
        from app.sources.boards.credentialed import AdzunaSource

        outcome = await AdzunaSource().run(ctx_for(http, credentials={}))
        assert outcome.status == "unconfigured"
        assert "app_id" in outcome.error
        assert outcome.jobs == []

    async def test_partial_credentials_still_unconfigured(self, http):
        from app.sources.boards.credentialed import AdzunaSource

        outcome = await AdzunaSource().run(ctx_for(http, credentials={"app_id": "x"}))
        assert outcome.status == "unconfigured"


class TestHttpClient:
    @respx.mock
    async def test_retries_then_succeeds(self, http):
        route = respx.get("https://example.com/data")
        route.side_effect = [httpx.Response(500), httpx.Response(200, json={"ok": True})]
        assert await http.get_json("https://example.com/data") == {"ok": True}
        assert route.call_count == 2

    @respx.mock
    async def test_client_error_is_not_retried(self, http):
        route = respx.get("https://example.com/missing").mock(
            return_value=httpx.Response(404, text="nope")
        )
        with pytest.raises(FetchError):
            await http.get_json("https://example.com/missing")
        assert route.call_count == 1

    @respx.mock
    async def test_invalid_json_raises_fetch_error(self, http):
        respx.get("https://example.com/bad").mock(
            return_value=httpx.Response(200, text="<html>not json</html>")
        )
        with pytest.raises(FetchError):
            await http.get_json("https://example.com/bad")


class TestRegistry:
    def test_every_source_has_a_unique_name(self):
        names = [c.name for c in ALL_SOURCE_CLASSES]
        assert len(names) == len(set(names))

    def test_sources_can_be_disabled_by_configuration(self):
        enabled = build_sources(disabled={"greenhouse", "lever"})
        names = {s.name for s in enabled}
        assert "greenhouse" not in names and "lever" not in names

    def test_board_providers_map_to_sources(self):
        providers = board_providers()
        assert providers["greenhouse"] == "greenhouse"
        assert providers["workday"] == "workday"

    def test_catalog_documents_credentials_and_limits(self):
        catalog = {entry["name"]: entry for entry in source_catalog()}
        assert catalog["adzuna"]["requires_credentials"] is True
        assert catalog["greenhouse"]["requires_credentials"] is False
        assert catalog["github_lists"]["is_discovery_source"] is True
        assert all(entry["notes"] for entry in catalog.values())

    def test_all_sources_declare_a_kind(self):
        for cls in ALL_SOURCE_CLASSES:
            assert isinstance(cls.kind, SourceKind)
            assert cls.kind is not SourceKind.UNKNOWN

    def test_every_source_subclasses_the_interface(self):
        assert all(issubclass(c, JobSource) for c in ALL_SOURCE_CLASSES)


class TestSpeculativeBoards:
    """A guessed board token that does not exist is not a source failure.

    Reproduces the first production run, where an empty registry meant the only
    Greenhouse/Lever/Ashby candidates were guesses derived from the user's
    preferred companies. All five 404'd -- correctly, since NVIDIA and Google
    do not use Greenhouse -- and three healthy sources were reported FAILED.
    """

    def _source(self, per_board):
        from app.models.base import SourceKind
        from app.sources.base import BoardJobSource

        class FakeBoards(BoardJobSource):
            name = "fake_ats"
            display_name = "Fake ATS"
            kind = SourceKind.ATS
            provider = "fake"

            async def fetch_board(self, board, ctx):
                return per_board(board)

        return FakeBoards()

    def _ctx(self, boards):
        from app.sources.base import SourceContext
        from app.sources.http import HttpClient

        return SourceContext(http=HttpClient(), queries=[], boards=boards)

    def _board(self, token, *, speculative=False):
        board = {"provider": "fake", "board_token": token}
        if speculative:
            board["speculative"] = True
        else:
            board["id"] = abs(hash(token)) % 10000
        return board

    async def test_all_speculative_misses_is_not_a_failure(self):
        from app.sources.base import FetchError

        def miss(board):
            raise FetchError("HTTP 404")

        source = self._source(miss)
        boards = [self._board(t, speculative=True) for t in ("nvidia", "amd", "google")]

        jobs = await source.fetch(self._ctx(boards))
        assert jobs == []
        assert source._last_board_stats == (0, 0), "guesses must not count as attempts"

    async def test_a_registered_board_failing_is_still_a_failure(self):
        from app.sources.base import FetchError

        def miss(board):
            raise FetchError("HTTP 500")

        source = self._source(miss)
        with pytest.raises(FetchError, match="all 1 registered boards failed"):
            await source.fetch(self._ctx([self._board("realco")]))

    async def test_speculative_misses_do_not_mask_a_real_failure(self):
        from app.sources.base import FetchError

        def miss(board):
            raise FetchError("HTTP 404")

        source = self._source(miss)
        boards = [self._board("nvidia", speculative=True), self._board("realco")]
        with pytest.raises(FetchError):
            await source.fetch(self._ctx(boards))
        assert source._last_board_stats == (1, 0)

    async def test_a_lucky_guess_still_contributes_its_jobs(self):
        """Guesses are excluded from health accounting, not from results."""
        from app.schemas.job import RawJob

        def hit(board):
            return [
                RawJob(
                    source="fake_ats",
                    source_job_id=f"{board['board_token']}-1",
                    title="FPGA Intern",
                    company=board["board_token"],
                    url="https://example.com/1",
                )
            ]

        source = self._source(hit)
        jobs = await source.fetch(self._ctx([self._board("stripe", speculative=True)]))
        assert len(jobs) == 1
        assert source._last_board_stats == (0, 0)

    async def test_health_ratio_counts_only_registered_boards(self):
        from app.schemas.job import RawJob
        from app.sources.base import FetchError

        def mixed(board):
            if board["board_token"] == "goodco":
                return [
                    RawJob(
                        source="fake_ats",
                        source_job_id="g-1",
                        title="Intern",
                        company="goodco",
                        url="https://example.com/g",
                    )
                ]
            raise FetchError("HTTP 404")

        source = self._source(mixed)
        boards = [
            self._board("goodco"),
            self._board("badco"),
            self._board("nvidia", speculative=True),
            self._board("google", speculative=True),
        ]
        jobs = await source.fetch(self._ctx(boards))
        assert len(jobs) == 1
        # 2 registered attempted, 1 succeeded -- the two guesses are invisible.
        assert source._last_board_stats == (2, 1)

    def test_seeded_candidates_are_marked_speculative(self, session):
        from app.pipeline.discovery import seed_boards_for_companies

        candidates = seed_boards_for_companies(session, ["NVIDIA", "AMD"])
        assert candidates, "expected guessed board candidates"
        assert all(c["speculative"] for c in candidates)
        assert all("id" not in c for c in candidates), "guesses are not registry rows"
