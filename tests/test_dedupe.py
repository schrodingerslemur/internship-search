"""Deduplication: the hard requirement, and its equally hard inverse.

Covers both directions -- the same job on many boards must collapse to one, and
similar-but-distinct positions must never be conflated.
"""

from __future__ import annotations

from datetime import timedelta

from app.models.base import SourceKind
from app.pipeline.dedupe import (
    DIFFERENT_THRESHOLD,
    MERGE_THRESHOLD,
    deduplicate,
    elect_application_url,
    merge_cluster_facts,
    merge_guard,
    similarity,
)
from app.pipeline.identity import canonicalize_url, extract_ats_identity, url_hash
from app.pipeline.normalize import normalize_job
from tests.conftest import NOW, make_raw


def norm(raws):
    return [normalize_job(r) for r in raws]


class TestUrlCanonicalization:
    def test_strips_tracking_parameters(self):
        dirty = "https://job-boards.greenhouse.io/acme/jobs/12345?utm_source=li&gh_src=x&fbclid=y"
        assert canonicalize_url(dirty) == "https://job-boards.greenhouse.io/acme/jobs/12345"

    def test_preserves_identifying_parameters(self):
        url = canonicalize_url("https://www.acme.com/careers?gh_jid=999&utm_campaign=spring")
        assert "gh_jid=999" in url
        assert "utm_campaign" not in url

    def test_same_job_different_tracking_yields_one_hash(self):
        a = url_hash("https://jobs.lever.co/acme/abc-123?lever_source=twitter")
        b = url_hash("https://jobs.lever.co/acme/abc-123?utm_medium=email")
        assert a == b

    def test_unwraps_redirect_wrappers(self):
        wrapped = "https://click.appcast.io/track?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F55"
        assert "greenhouse.io/acme/jobs/55" in canonicalize_url(wrapped)

    def test_handles_garbage(self):
        assert canonicalize_url(None) is None
        assert canonicalize_url("") is None
        assert canonicalize_url("not a url") is not None  # coerced to https://


class TestAtsIdentity:
    def test_greenhouse(self):
        i = extract_ats_identity("https://job-boards.greenhouse.io/acme/jobs/12345")
        assert i.provider == "greenhouse" and i.job_key == "greenhouse:acme:12345"

    def test_lever(self):
        i = extract_ats_identity("https://jobs.lever.co/acme/abc-def-123")
        assert i.provider == "lever" and i.board_token == "acme"

    def test_workday_carries_crawl_metadata(self):
        i = extract_ats_identity(
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/Intern_JR1998"
        )
        assert i.provider == "workday"
        assert i.extra["host"] == "nvidia.wd5.myworkdayjobs.com"
        assert i.extra["site"] == "NVIDIAExternalCareerSite"

    def test_aggregator_apply_link_matches_direct_crawl(self):
        """The key cross-board mechanism."""
        via_board = extract_ats_identity(
            "https://www.linkedin.com/jobs/view/1", "https://boards.greenhouse.io/acme/jobs/77?utm_source=li"
        )
        direct = extract_ats_identity("https://job-boards.greenhouse.io/acme/jobs/77")
        assert via_board.job_key == direct.job_key

    def test_non_ats_url_returns_none(self):
        assert extract_ats_identity("https://example.com/careers") is None


class TestCrossSourceDeduplication:
    def test_five_listings_collapse_to_one_job(self, cross_source_duplicates):
        result = deduplicate(norm(cross_source_duplicates))
        assert len(result.clusters) == 1
        assert result.clusters[0].size == 5
        assert result.duplicates_removed == 4

    def test_all_five_sources_are_retained_internally(self, cross_source_duplicates):
        cluster = deduplicate(norm(cross_source_duplicates)).clusters[0]
        assert set(cluster.sources) == {
            "greenhouse", "linkedin", "adzuna", "themuse", "company_careers",
        }

    def test_company_careers_url_wins_the_apply_button(self, cross_source_duplicates):
        cluster = deduplicate(norm(cross_source_duplicates)).clusters[0]
        apply_url, _ = elect_application_url(cluster)
        assert "nvidia.com" in apply_url

    def test_ats_page_beats_aggregator_when_no_company_page(self):
        listings = [
            make_raw(source="adzuna", kind=SourceKind.AGGREGATOR, source_job_id="1",
                     url="https://www.adzuna.com/land/ad/1",
                     apply_url="https://job-boards.greenhouse.io/acme/jobs/9"),
            make_raw(source="greenhouse", kind=SourceKind.ATS, source_job_id="2",
                     url="https://job-boards.greenhouse.io/acme/jobs/9"),
        ]
        cluster = deduplicate(norm(listings)).clusters[0]
        apply_url, _ = elect_application_url(cluster)
        assert "greenhouse.io" in apply_url

    def test_merged_job_inherits_facts_from_whichever_source_had_them(self):
        listings = [
            make_raw(source="company_careers", kind=SourceKind.COMPANY_CAREERS,
                     source_job_id="1", description=None,
                     url="https://acme.com/careers/1?gh_jid=5"),
            make_raw(source="adzuna", kind=SourceKind.AGGREGATOR, source_job_id="2",
                     url="https://adzuna.com/x", apply_url="https://boards.greenhouse.io/acme/jobs/5",
                     salary_raw="$50 per hour"),
        ]
        cluster = deduplicate(norm(listings)).clusters[0]
        merged = merge_cluster_facts(cluster)
        assert merged.salary_min == 50  # taken from the aggregator
        assert merged.description  # taken from whichever member had one

    def test_same_job_reposted_at_new_url_still_merges_by_ats_identity(self):
        listings = [
            make_raw(source="greenhouse", source_job_id="1",
                     url="https://boards.greenhouse.io/acme/jobs/42"),
            make_raw(source="linkedin", kind=SourceKind.JOB_BOARD, source_job_id="2",
                     url="https://linkedin.com/jobs/view/777",
                     apply_url="https://job-boards.greenhouse.io/acme/jobs/42"),
        ]
        assert len(deduplicate(norm(listings)).clusters) == 1


class TestOverDeduplicationGuards:
    def test_same_title_different_metros_stay_separate(self, distinct_jobs):
        austin, santa_clara = norm(distinct_jobs[:2])
        assert merge_guard(austin, santa_clara)[0] is False

    def test_location_guard_blocks_even_without_requisition_ids(self):
        """Location alone must be enough to keep two openings apart."""
        austin, santa_clara = norm([
            make_raw(source="themuse", kind=SourceKind.AGGREGATOR, source_job_id="1",
                     title="FPGA Engineer Intern", company="Acme Semi",
                     location="Austin, TX", url="https://themuse.com/jobs/acme/fpga-austin"),
            make_raw(source="themuse", kind=SourceKind.AGGREGATOR, source_job_id="2",
                     title="FPGA Engineer Intern", company="Acme Semi",
                     location="Santa Clara, CA", url="https://themuse.com/jobs/acme/fpga-sc"),
        ])
        assert austin.requisition_id is None and santa_clara.requisition_id is None
        allowed, reason = merge_guard(austin, santa_clara)
        assert allowed is False
        assert "location" in reason or "same source" in reason

    def test_verification_and_design_stay_separate(self, distinct_jobs):
        verification, design = norm([distinct_jobs[2], distinct_jobs[3]])
        allowed, _ = merge_guard(verification, design)
        assert allowed is False

    def test_summer_and_fall_stay_separate(self, distinct_jobs):
        summer, fall = norm([distinct_jobs[0], distinct_jobs[4]])
        allowed, _ = merge_guard(summer, fall)
        assert allowed is False

    def test_distinct_requisition_ids_block_merging(self):
        a, b = norm([
            make_raw(source_job_id="1", requisition_id="REQ-100",
                     url="https://acme.com/jobs/1"),
            make_raw(source="indeed", kind=SourceKind.AGGREGATOR, source_job_id="2",
                     requisition_id="REQ-200", url="https://acme.com/jobs/2"),
        ])
        allowed, reason = merge_guard(a, b)
        assert allowed is False and "requisition" in reason

    def test_all_five_distinct_jobs_survive(self, distinct_jobs):
        result = deduplicate(norm(distinct_jobs))
        assert len(result.clusters) == 5

    def test_same_source_distinct_postings_are_not_merged(self):
        """A board does not advertise one job twice under two ids."""
        listings = [
            make_raw(source="github_lists", kind=SourceKind.CURATED_LIST,
                     source_job_id=f"gl-{i}", title="Software Engineer Intern",
                     company="TikTok", location="San Jose, CA",
                     description="Software engineering internship.",
                     url=f"https://lifeattiktok.com/search/76685816362{i:04d}")
            for i in range(8)
        ]
        assert len(deduplicate(norm(listings)).clusters) == 8

    def test_different_companies_never_merge(self):
        a, b = norm([
            make_raw(company="NVIDIA", source_job_id="1", url="https://a.com/1"),
            make_raw(company="AMD", source_job_id="2", url="https://b.com/2"),
        ])
        assert merge_guard(a, b)[0] is False


class TestSimilarity:
    def test_identical_postings_score_high(self, cross_source_duplicates):
        a, b = norm(cross_source_duplicates[:2])
        score, _ = similarity(a, b)
        assert score > 0.85

    def test_unrelated_postings_score_low(self):
        a, b = norm([
            make_raw(title="FPGA Design Intern", source_job_id="1"),
            make_raw(title="Marketing Intern", source_job_id="2",
                     description="Social media marketing internship."),
        ])
        score, _ = similarity(a, b)
        assert score < 0.7

    def test_matching_requisition_ids_boost_confidence(self):
        a, b = norm([
            make_raw(source_job_id="1", requisition_id="R-500", url="https://a.com/1"),
            make_raw(source="indeed", source_job_id="2", requisition_id="R-500",
                     url="https://b.com/2", title="FPGA Design Internship"),
        ])
        score, reason = similarity(a, b)
        assert "req-id match" in reason
        assert score > 0.85


class TestLlmAdjudication:
    @staticmethod
    def _uncertain_pair():
        """Two listings whose similarity lands between the two thresholds."""
        shared = (
            "You will join the silicon team as an intern and write SystemVerilog "
            "RTL for high speed data paths, build UVM testbenches, and run "
            "pre-silicon verification on FPGA prototypes using Xilinx devices. "
        )
        return [
            make_raw(source="greenhouse", source_job_id="1",
                     title="FPGA Design Intern", url="https://a.com/1",
                     description=shared + "The team focuses on networking silicon."),
            make_raw(source="indeed", source_job_id="2",
                     title="FPGA Design Intern - Silicon Engineering", url="https://b.com/2",
                     description=shared + "The group focuses on networking chips."),
        ]

    def test_uncertain_pairs_consult_the_adjudicator(self):
        pair = norm(self._uncertain_pair())
        score, _ = similarity(*pair)
        assert DIFFERENT_THRESHOLD <= score < MERGE_THRESHOLD, (
            f"fixture must sit in the uncertain band, got {score:.3f}"
        )

        calls = []

        def adjudicator(a, b):
            calls.append((a.key, b.key))
            return "SAME", 0.95

        result = deduplicate(pair, llm_adjudicator=adjudicator, max_llm_calls=5)
        assert calls, "adjudicator should have been consulted"
        assert len(result.clusters) == 1
        assert result.llm_calls == 1

    def test_llm_verdict_different_prevents_merging(self):
        pair = norm(self._uncertain_pair())
        result = deduplicate(pair, llm_adjudicator=lambda a, b: ("DIFFERENT", 0.9),
                             max_llm_calls=5)
        assert len(result.clusters) == 2

    def test_low_confidence_same_does_not_merge(self):
        """Only merge when the model is confident."""
        pair = norm(self._uncertain_pair())
        result = deduplicate(pair, llm_adjudicator=lambda a, b: ("SAME", 0.4),
                             max_llm_calls=5)
        assert len(result.clusters) == 2

    def test_adjudicator_is_not_called_without_budget(self):
        calls = []

        def adjudicator(a, b):
            calls.append(1)
            return "SAME", 1.0

        deduplicate(norm(self._uncertain_pair()), llm_adjudicator=adjudicator, max_llm_calls=0)
        assert calls == []

    def test_adjudicator_failure_does_not_break_the_run(self):
        def broken(a, b):
            raise RuntimeError("provider down")

        result = deduplicate(norm(self._uncertain_pair()), llm_adjudicator=broken,
                             max_llm_calls=5)
        assert result.clusters  # run survives a broken adjudicator


class TestScale:
    def test_empty_input(self):
        assert deduplicate([]).clusters == []

    def test_large_batch_completes(self):
        listings = [
            make_raw(source="greenhouse", source_job_id=str(i), company=f"Company {i % 50}",
                     title="FPGA Design Intern", url=f"https://job-boards.greenhouse.io/c{i%50}/jobs/{i}")
            for i in range(300)
        ]
        result = deduplicate(norm(listings))
        assert len(result.clusters) == 300

    def test_dates_far_apart_reduce_similarity(self):
        a = make_raw(source="a", source_job_id="1", url="https://a.com/1", days_ago=1)
        b = make_raw(source="b", source_job_id="2", url="https://b.com/2", days_ago=200)
        na, nb = norm([a, b])
        nb.date_posted = NOW - timedelta(days=200)
        score, reason = similarity(na, nb)
        assert "far apart" in reason
