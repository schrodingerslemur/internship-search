"""Reading postings with a language model.

The model is exercised against a stub OpenAI-compatible endpoint rather than a
real one. That is the point of the transport being the wire format: the same
tests cover Ollama, Groq and every other gateway, and they run offline in CI.

The behaviour worth protecting is not "the model is clever" -- it is that a
model which answers badly, slowly, or not at all can only ever contribute
nothing, never something false.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.models import Job
from app.pipeline.llm import LlmClient, _sanitize_facts
from app.schemas.job import normalized_from_job_row
from app.schemas.preferences import default_preferences
from app.services.enrichment import (
    MIN_ROLE_AFFINITY,
    apply_facts,
    candidates,
    enrich_jobs,
    needs_enrichment,
)
from tests.conftest import NOW

GOOD_FACTS = {
    "skills": ["SystemVerilog", "UVM", "FPGA", "teamwork", "communication"],
    "domain": "hardware",
    "seniority": "intern",
    "is_internship": True,
    "sponsorship": "unknown",
    "min_years_experience": None,
    "terms": ["Summer 2027"],
    "summary": "Pre-silicon verification internship on the SoC team.",
}


def stub_client(handler, **kwargs) -> LlmClient:
    """An LlmClient wired to an in-process endpoint."""
    client = LlmClient(**kwargs)
    client.enabled = True
    client.model = "stub-model"
    client.base_url = "http://stub/v1"
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://stub/v1"
    )
    return client


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def make_job(**kwargs) -> Job:
    defaults = dict(
        id=1,
        canonical_job_id="j1",
        fingerprint="f1",
        company_name="NVIDIA",
        title="Hardware Engineering Intern",
        application_url="https://example.com/apply/1",
        description="Write SystemVerilog RTL and build UVM testbenches. " * 6,
        location_raw="Santa Clara, CA",
        skills=["rtl"],
        terms=[],
        degree_requirements=[],
        locations=[],
        content_hash="hash-1",
        is_active=True,
        remote_status="unknown",
        employment_type="internship",
        sponsorship="unknown",
        enrichment=None,
        enrichment_hash=None,
        enriched_at=None,
    )
    defaults.update(kwargs)
    return Job(**defaults)


class TestWireFormat:
    def test_sends_an_openai_chat_completion(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler)
        facts = client.extract_facts(normalized_from_job_row(make_job()))

        assert seen["url"].endswith("/chat/completions")
        assert seen["body"]["model"] == "stub-model"
        assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]
        # Determinism matters: the same posting must not produce different
        # facts on two runs, or a re-score becomes unreproducible.
        assert seen["body"]["temperature"] == 0
        assert facts["domain"] == "hardware"

    def test_a_local_endpoint_sends_no_authorization_header(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler)
        client._key = None
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://stub/v1"
        )
        client.extract_facts(normalized_from_job_row(make_job()))
        assert seen["auth"] is None

    def test_json_wrapped_in_a_code_fence_is_still_read(self):
        fenced = "```json\n" + json.dumps(GOOD_FACTS) + "\n```"
        client = stub_client(lambda r: completion(fenced))
        assert client.extract_facts(normalized_from_job_row(make_job()))["domain"] == "hardware"

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(500, text="upstream exploded"),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"choices": [{"message": {"content": "sorry, I cannot"}}]}),
        ],
    )
    def test_a_bad_response_yields_nothing_rather_than_raising(self, response, monkeypatch):
        # 429 is now waited out rather than failed, so without this the rate
        # limited case spends its full backoff in real time.
        monkeypatch.setattr("app.pipeline.llm.time.sleep", lambda _: None)
        client = stub_client(lambda r: response)
        assert client.extract_facts(normalized_from_job_row(make_job())) is None

    def test_a_timeout_yields_nothing_rather_than_raising(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        client = stub_client(handler)
        assert client.extract_facts(normalized_from_job_row(make_job())) is None

    def test_the_call_budget_is_enforced(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler, max_calls=2)
        for _ in range(5):
            client.extract_facts(normalized_from_job_row(make_job()))
        assert calls["n"] == 2

    def test_a_disabled_client_never_calls_out(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a disabled client must not make requests")

        client = stub_client(handler)
        client.enabled = False
        assert client.extract_facts(normalized_from_job_row(make_job())) is None


class TestSanitisation:
    """The parser is the contract, not the prompt.

    A 7B model asked for a closed vocabulary will sometimes answer with a
    synonym, a sentence, or a list of soft skills. None of that may reach the
    scorer.
    """

    def test_soft_skills_are_dropped(self):
        facts = _sanitize_facts(GOOD_FACTS)
        assert "teamwork" not in facts["skills"]
        assert "communication" not in facts["skills"]
        assert "systemverilog" in facts["skills"]

    def test_skills_are_lowercased_and_deduplicated(self):
        facts = _sanitize_facts({"skills": ["FPGA", "fpga", " FPGA "]})
        assert facts["skills"] == ["fpga"]

    def test_values_outside_the_vocabulary_become_unknown(self):
        facts = _sanitize_facts(
            {"domain": "quantum astrology", "seniority": "wizard", "sponsorship": "probably"}
        )
        assert facts["domain"] == "unknown"
        assert facts["seniority"] == "unknown"
        assert facts["sponsorship"] == "unknown"

    def test_absurd_experience_figures_are_discarded(self):
        assert _sanitize_facts({"min_years_experience": 900})["min_years_experience"] is None
        assert _sanitize_facts({"min_years_experience": "lots"})["min_years_experience"] is None
        assert _sanitize_facts({"min_years_experience": 2})["min_years_experience"] == 2.0

    def test_a_non_boolean_internship_answer_is_unknown_not_false(self):
        """"Probably" must not become "no". Absence of evidence, again."""
        assert _sanitize_facts({"is_internship": "probably"})["is_internship"] is None
        assert _sanitize_facts({"is_internship": True})["is_internship"] is True

    def test_garbage_shapes_do_not_crash(self):
        facts = _sanitize_facts({"skills": "not a list", "terms": 42})
        assert facts["skills"] == []
        assert facts["terms"] == []

    def test_the_skill_list_is_capped(self):
        facts = _sanitize_facts({"skills": [f"skill-{i}" for i in range(80)]})
        assert len(facts["skills"]) == 20


class TestSelection:
    def test_a_posting_with_no_body_is_never_read(self):
        """Nothing to read is exactly when a model invents things."""
        assert needs_enrichment(make_job(description="", requirements=None)) is False
        assert needs_enrichment(make_job()) is True

    def test_an_already_read_posting_is_skipped(self):
        job = make_job(enrichment={"skills": []}, enrichment_hash="hash-1", enriched_at=NOW)
        assert needs_enrichment(job) is False

    def test_a_rewritten_posting_becomes_eligible_again(self):
        job = make_job(enrichment={"skills": []}, enrichment_hash="old-hash", enriched_at=NOW)
        assert needs_enrichment(job) is True

    def test_only_plausible_roles_are_worth_a_call(self, session):
        prefs = default_preferences()
        session.add_all(
            [
                make_job(id=None, canonical_job_id="a", fingerprint="a",
                         title="Hardware Engineering Intern"),
                make_job(id=None, canonical_job_id="b", fingerprint="b",
                         title="Unpaid News Intern", company_name="Nexstar"),
                make_job(id=None, canonical_job_id="c", fingerprint="c",
                         title="Legal & Compliance Analyst Intern", company_name="PIMCO"),
            ]
        )
        session.flush()

        picked = {job.title for job in candidates(session, prefs, limit=10)}
        assert "Hardware Engineering Intern" in picked
        assert "Unpaid News Intern" not in picked
        assert "Legal & Compliance Analyst Intern" not in picked

    def test_selection_threshold_is_a_real_gate(self):
        assert 0 < MIN_ROLE_AFFINITY < 1


class TestApplyingFacts:
    def test_new_skills_are_merged_not_replaced(self):
        job = make_job(skills=["rtl"])
        added = apply_facts(job, _sanitize_facts(GOOD_FACTS), model="stub", now=NOW)
        assert "rtl" in job.skills, "vocabulary hits are precise and must survive"
        assert "systemverilog" in job.skills
        assert added == len(set(job.skills)) - 1

    def test_provenance_is_recorded(self):
        job = make_job()
        apply_facts(job, _sanitize_facts(GOOD_FACTS), model="qwen2.5:7b", now=NOW)
        assert job.enrichment_model == "qwen2.5:7b"
        assert job.enrichment_hash == job.content_hash
        assert job.enriched_at == NOW
        assert job.enrichment["summary"].startswith("Pre-silicon")

    def test_extracted_evidence_is_not_overwritten_by_the_model(self):
        """A stated fact beats a read one, so the model only fills blanks."""
        job = make_job(terms=["Fall 2026"], experience_required_years=1.0)
        facts = _sanitize_facts({**GOOD_FACTS, "min_years_experience": 5})
        apply_facts(job, facts, model="stub", now=NOW)
        assert job.terms == ["Fall 2026"]
        assert job.experience_required_years == 1.0

    def test_blanks_are_filled(self):
        job = make_job(terms=[], experience_required_years=None)
        facts = _sanitize_facts({**GOOD_FACTS, "min_years_experience": 2})
        apply_facts(job, facts, model="stub", now=NOW)
        assert job.terms == ["Summer 2027"]
        assert job.experience_required_years == 2.0


class TestEnrichRun:
    def test_reads_eligible_jobs_and_reports_what_happened(self, session):
        session.add(make_job(id=None, canonical_job_id="a", fingerprint="a"))
        session.flush()

        client = stub_client(lambda r: completion(json.dumps(GOOD_FACTS)))
        report = enrich_jobs(session, default_preferences(), limit=5, client=client, now=NOW)

        assert report.enriched == 1
        assert report.failed == 0
        assert report.skills_added > 0
        assert "stub-model" in report.summary()

    def test_a_failing_model_leaves_the_corpus_untouched(self, session):
        job = make_job(id=None, canonical_job_id="a", fingerprint="a", skills=["rtl"])
        session.add(job)
        session.flush()

        client = stub_client(lambda r: httpx.Response(503, text="down"))
        report = enrich_jobs(session, default_preferences(), limit=5, client=client, now=NOW)

        assert report.enriched == 0
        assert report.failed == 1
        assert job.skills == ["rtl"]
        assert job.enrichment is None

    def test_with_no_model_configured_it_says_so_and_does_nothing(self, session):
        session.add(make_job(id=None, canonical_job_id="a", fingerprint="a"))
        session.flush()

        client = stub_client(lambda r: completion(json.dumps(GOOD_FACTS)))
        client.enabled = False
        report = enrich_jobs(session, default_preferences(), limit=5, client=client)

        assert report.enriched == 0
        assert report.errors and "No model configured" in report.errors[0]


class TestSurvivingTheFreeTier:
    """The endpoints this project recommends are rate-limited by design, and
    reasoning models spend the answer's budget thinking first. Both look like
    a broken model unless the client knows better."""

    def test_a_rate_limited_call_is_waited_out_rather_than_failed(self, monkeypatch):
        """429 is an ordinary part of a backfill on a free tier. Treating it as
        a failure reported "did not return usable JSON" -- a different and much
        more alarming problem -- and burned the run's budget on nothing."""
        monkeypatch.setattr("app.pipeline.llm.time.sleep", lambda _: None)
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                return httpx.Response(429, headers={"retry-after": "1"}, json={"error": "slow down"})
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler)
        facts = client.extract_facts(normalized_from_job_row(make_job()))
        assert facts is not None
        assert attempts["n"] == 3

    def test_waiting_out_a_limit_costs_one_call_not_three(self, monkeypatch):
        """The budget counts postings read, not HTTP round trips."""
        monkeypatch.setattr("app.pipeline.llm.time.sleep", lambda _: None)
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                return httpx.Response(429, headers={"retry-after": "1"}, json={})
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler, max_calls=5)
        client.extract_facts(normalized_from_job_row(make_job()))
        assert client.calls == 1
        assert client.budget_left == 4

    def test_the_provider_is_obeyed_on_how_long_to_wait(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("app.pipeline.llm.time.sleep", slept.append)
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "7.5"}, json={})
            return completion(json.dumps(GOOD_FACTS))

        stub_client(handler).extract_facts(normalized_from_job_row(make_job()))
        assert slept == [7.5]

    def test_a_suggested_wait_is_capped(self, monkeypatch):
        """A backfill that pauses is working; one that sleeps for an hour is not."""
        slept: list[float] = []
        monkeypatch.setattr("app.pipeline.llm.time.sleep", slept.append)

        def handler(request):
            return httpx.Response(429, headers={"retry-after": "9000"}, json={})

        stub_client(handler).extract_facts(normalized_from_job_row(make_job()))
        assert slept and max(slept) <= 65.0

    def test_a_wall_of_rate_limits_eventually_gives_up_quietly(self, monkeypatch):
        monkeypatch.setattr("app.pipeline.llm.time.sleep", lambda _: None)

        def handler(request):
            return httpx.Response(429, headers={"retry-after": "1"}, json={})

        assert stub_client(handler).extract_facts(normalized_from_job_row(make_job())) is None

    def test_a_reasoning_model_gets_room_to_think_before_answering(self):
        """The ceiling covers private reasoning as well as the visible answer,
        so a limit sized for the answer alone is consumed before any JSON is
        written -- which arrives as an empty body, not an error."""
        seen: dict = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return completion(json.dumps(GOOD_FACTS))

        stub_client(handler).extract_facts(normalized_from_job_row(make_job()))
        assert seen["max_completion_tokens"] > 700
        # Both spellings travel together, so a gateway honouring either one
        # applies the same ceiling.
        assert seen["max_tokens"] == seen["max_completion_tokens"]

    def test_a_daily_quota_stops_the_run_instead_of_being_slept_through(self, monkeypatch):
        """A per-minute limit refills; a per-day one does not. Waiting out the
        second burns an hour to achieve nothing."""
        slept: list[float] = []
        monkeypatch.setattr("app.pipeline.llm.time.sleep", slept.append)

        def handler(request):
            return httpx.Response(
                429,
                headers={"retry-after": "60"},
                json={"error": {"message": "Rate limit reached ... on tokens per day (TPD): Limit 200000"}},
            )

        client = stub_client(handler)
        assert client.extract_facts(normalized_from_job_row(make_job())) is None
        assert client.exhausted is True
        assert slept == [], "a daily cap should not be waited on at all"

    def test_once_exhausted_no_further_calls_are_made(self, monkeypatch):
        monkeypatch.setattr("app.pipeline.llm.time.sleep", lambda _: None)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(429, json={"error": {"message": "tokens per day (TPD) exceeded"}})

        client = stub_client(handler, max_calls=10)
        for _ in range(4):
            client.extract_facts(normalized_from_job_row(make_job()))
        assert calls["n"] == 1

    #: Verbatim from Groq. Note the upgrade URL: every rate-limit message ends
    #: with one, so a fatal-quota check that matches on "billing" calls this
    #: recoverable limit fatal and abandons a healthy backfill.
    TPM_BODY = {
        "error": {
            "message": (
                "Rate limit reached for model `openai/gpt-oss-20b` in organization "
                "`org_01m0` service tier `on_demand` on tokens per minute (TPM): "
                "Limit 8000, Used 4839, Requested 6372. Please try again in 24.08s. "
                "Need more tokens? Upgrade to Dev Tier today at "
                "https://console.groq.com/settings/billing"
            ),
            "type": "tokens",
            "code": "rate_limit_exceeded",
        }
    }
    TPD_BODY = {
        "error": {
            "message": (
                "Rate limit reached for model `openai/gpt-oss-120b` in organization "
                "`org_01m0` service tier `on_demand` on tokens per day (TPD): "
                "Limit 200000, Used 199047, Requested 2000. Please try again in 12m. "
                "Need more tokens? Upgrade to Dev Tier today at "
                "https://console.groq.com/settings/billing"
            ),
            "type": "tokens",
            "code": "rate_limit_exceeded",
        }
    }

    def test_the_real_per_minute_message_is_not_mistaken_for_a_daily_one(self, monkeypatch):
        """Both messages end with the same upgrade link. Only the period differs."""
        monkeypatch.setattr("app.pipeline.llm.time.sleep", lambda _: None)
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, json=self.TPM_BODY)
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler)
        assert client.extract_facts(normalized_from_job_row(make_job())) is not None
        assert client.exhausted is False

    def test_the_real_daily_message_stops_the_run(self, monkeypatch):
        monkeypatch.setattr("app.pipeline.llm.time.sleep", lambda _: None)
        client = stub_client(lambda r: httpx.Response(429, json=self.TPD_BODY))
        assert client.extract_facts(normalized_from_job_row(make_job())) is None
        assert client.exhausted is True

    def test_a_per_minute_limit_is_still_waited_out(self, monkeypatch):
        """The daily check must not swallow the ordinary case."""
        monkeypatch.setattr("app.pipeline.llm.time.sleep", lambda _: None)
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(
                    429, json={"error": {"message": "Rate limit reached on tokens per minute (TPM)"}}
                )
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler)
        assert client.extract_facts(normalized_from_job_row(make_job())) is not None
        assert client.exhausted is False


class TestABackfillKeepsWhatItAlreadyRead:
    """Hundreds of postings against a rate-limited free tier take hours. One
    transaction around all of it means an interruption discards every posting
    already read, and the provider quota that paid for them."""

    def test_checkpointing_persists_work_before_the_batch_ends(self, session, prefs):
        jobs = [
            make_job(id=i, canonical_job_id=f"j{i}", fingerprint=f"f{i}", content_hash=f"h{i}")
            for i in range(1, 7)
        ]
        for job in jobs:
            session.add(job)
        session.flush()

        seen = {"n": 0}

        def handler(request):
            seen["n"] += 1
            if seen["n"] > 4:
                raise httpx.ReadTimeout("died mid-batch", request=request)
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler, max_calls=50)
        enrich_jobs(session, prefs, limit=50, client=client, commit_every=2)

        # Whatever was read before the model died is on disk, not in a
        # transaction that a kill would have discarded.
        session.expire_all()
        from sqlalchemy import func, select

        from app.models import Job

        persisted = session.scalar(
            select(func.count(Job.id)).where(Job.enriched_at.is_not(None))
        )
        assert persisted >= 4

    def test_without_checkpointing_the_caller_still_owns_the_transaction(self, session, prefs):
        """The search run enriches inside a larger unit of work that has to
        land or not land as a whole, so the default must not commit."""
        job = make_job()
        session.add(job)
        session.flush()

        committed = {"n": 0}
        original = session.commit
        session.commit = lambda: (committed.__setitem__("n", committed["n"] + 1), original())[1]

        client = stub_client(lambda r: completion(json.dumps(GOOD_FACTS)), max_calls=5)
        enrich_jobs(session, prefs, limit=5, client=client)
        assert committed["n"] == 0

    def test_a_model_that_never_reached_the_answer_is_given_more_room(self):
        """The provider validates the empty string against the requested JSON
        shape and reports invalid JSON -- which reads like a prompt problem and
        is really the reasoning budget running out on a long posting."""
        seen: list[int] = []

        def handler(request):
            body = json.loads(request.content)
            seen.append(body["max_completion_tokens"])
            if len(seen) == 1:
                return httpx.Response(400, json={"error": {
                    "message": "Failed to validate JSON. Please adjust your prompt.",
                    "code": "json_validate_failed",
                    "failed_generation": "",
                }})
            return completion(json.dumps(GOOD_FACTS))

        client = stub_client(handler)
        assert client.extract_facts(normalized_from_job_row(make_job())) is not None
        assert len(seen) == 2
        assert seen[1] > seen[0], "the retry has to be given more room than the attempt that failed"

    def test_the_retry_costs_one_call_not_two(self):
        def handler(request):
            return httpx.Response(400, json={"error": {
                "code": "json_validate_failed", "failed_generation": "",
            }})

        client = stub_client(handler, max_calls=5)
        client.extract_facts(normalized_from_job_row(make_job()))
        assert client.calls == 1

    def test_a_model_that_answered_badly_is_not_retried(self):
        """A non-empty generation means it got there and was wrong. More room
        buys nothing, and spending it twice is how quota disappears."""
        seen: list[int] = []

        def handler(request):
            seen.append(json.loads(request.content)["max_completion_tokens"])
            return httpx.Response(400, json={"error": {
                "code": "json_validate_failed",
                "failed_generation": "{not actually json",
            }})

        client = stub_client(handler)
        assert client.extract_facts(normalized_from_job_row(make_job())) is None
        assert len(seen) == 1
