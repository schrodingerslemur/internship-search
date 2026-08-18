"""Relevance scoring: role, skills, location, eligibility, and exclusions."""

from __future__ import annotations

import pytest

from app.models.base import Priority, SponsorshipStatus
from app.pipeline.match import classify_priority, hard_exclusions, score_job
from app.pipeline.normalize import normalize_job
from app.pipeline.queries import expand_role, generate_queries
from app.schemas.preferences import TargetRole
from tests.conftest import NOW, SENIOR_DESCRIPTION, SOFTWARE_DESCRIPTION, make_raw


def score(raw, prefs, profile):
    return score_job(normalize_job(raw), prefs, profile, now=NOW)


class TestRoleMatching:
    def test_exact_role_match_scores_well(self, prefs, profile):
        result = score(make_raw(title="FPGA Engineer Intern"), prefs, profile)
        assert result.score >= 70
        assert any("target role" in r.lower() for r in result.match_reasons)

    def test_unrelated_role_scores_low(self, prefs, profile):
        result = score(
            make_raw(title="Marketing Intern", company="Coca-Cola",
                     description="Social media marketing internship.", location="Atlanta, GA"),
            prefs, profile,
        )
        assert result.score < 65
        assert result.priority is Priority.SKIP

    def test_hardware_beats_generic_software_for_this_profile(self, prefs, profile):
        hardware = score(make_raw(title="FPGA Design Intern"), prefs, profile)
        software = score(
            make_raw(title="Software Engineer Intern", description=SOFTWARE_DESCRIPTION),
            prefs, profile,
        )
        assert hardware.score > software.score

    def test_role_weight_influences_ranking(self, prefs, profile):
        prefs.roles = [
            TargetRole(name="Software Engineer Intern", weight=2.0, order=0),
            TargetRole(name="FPGA Engineer Intern", weight=0.5, order=1),
        ]
        result = score(make_raw(title="Software Engineer Intern",
                                description=SOFTWARE_DESCRIPTION), prefs, profile)
        assert any("Software Engineer Intern" in r for r in result.match_reasons)


class TestSkillMatching:
    def test_overlapping_skills_are_reported(self, prefs, profile):
        result = score(make_raw(), prefs, profile)
        assert result.matched_skills
        assert any("fpga" in s.lower() for s in result.matched_skills)

    def test_missing_skills_are_reported_without_inventing_any(self, prefs, profile):
        result = score(
            make_raw(description="Requires expertise in Kubernetes and Terraform."),
            prefs, profile,
        )
        assert all(s not in result.matched_skills for s in result.missing_skills)

    def test_empty_profile_does_not_crash(self, prefs):
        from app.schemas.profile import CandidateProfileData

        result = score(make_raw(), prefs, CandidateProfileData())
        assert 0 <= result.score <= 100


class TestExperienceAndEligibility:
    def test_senior_role_is_penalised(self, prefs, profile):
        result = score(
            make_raw(title="Senior Staff RTL Design Manager", description=SENIOR_DESCRIPTION),
            prefs, profile,
        )
        assert result.priority is Priority.SKIP
        assert any("years" in c for c in result.concerns)
        assert "10+ years experience" in result.missing_requirements

    def test_negative_keywords_in_title_hurt_more_than_in_body(self, prefs, profile):
        in_title = score(make_raw(title="Senior FPGA Intern"), prefs, profile)
        in_body = score(
            make_raw(title="FPGA Design Intern",
                     description="You will report to a senior engineer. FPGA and RTL work."),
            prefs, profile,
        )
        assert in_body.score > in_title.score

    def test_phd_requirement_is_flagged(self, prefs, profile):
        result = score(
            make_raw(title="Research Intern",
                     description="Must be pursuing a PhD in Computer Architecture."),
            prefs, profile,
        )
        assert "PhD" in result.missing_requirements


class TestLocationScoring:
    def test_preferred_location_beats_unlisted_one(self, prefs, profile):
        bay = score(make_raw(location="Santa Clara, CA"), prefs, profile)
        elsewhere = score(make_raw(location="Omaha, NE"), prefs, profile)
        assert bay.score > elsewhere.score

    def test_remote_is_credited(self, prefs, profile):
        result = score(make_raw(location="Remote"), prefs, profile)
        assert any("remote" in r.lower() for r in result.match_reasons)

    def test_unknown_location_is_not_penalised_into_oblivion(self, prefs, profile):
        result = score(make_raw(location=None), prefs, profile)
        assert result.score > 40

    def test_excluded_location_is_filtered(self, prefs, profile):
        from app.schemas.preferences import LocationRule

        prefs.locations.rules.append(LocationRule(pattern="Omaha", bonus=-999, excluded=True))
        excluded, reason = hard_exclusions(normalize_job(make_raw(location="Omaha, NE")), prefs)
        assert excluded is True and "Omaha" in reason


class TestSponsorship:
    def test_unknown_sponsorship_never_filters_a_job(self, prefs, profile):
        prefs.constraints.requires_sponsorship = True
        prefs.constraints.hard_filter_sponsorship = True
        job = normalize_job(make_raw())
        assert job.sponsorship is SponsorshipStatus.UNKNOWN
        excluded, _ = hard_exclusions(job, prefs)
        assert excluded is False

    def test_unknown_sponsorship_is_raised_as_a_concern(self, prefs, profile):
        prefs.constraints.requires_sponsorship = True
        result = score(make_raw(), prefs, profile)
        assert any("sponsorship" in c.lower() for c in result.concerns)

    def test_explicit_refusal_can_be_hard_filtered_when_asked(self, prefs, profile):
        prefs.constraints.requires_sponsorship = True
        prefs.constraints.hard_filter_sponsorship = True
        job = normalize_job(
            make_raw(description="We are unable to provide visa sponsorship for this role.")
        )
        assert hard_exclusions(job, prefs)[0] is True

    def test_sponsorship_irrelevant_when_not_required(self, prefs, profile):
        prefs.constraints.requires_sponsorship = False
        result = score(
            make_raw(description="We are unable to provide visa sponsorship."), prefs, profile
        )
        assert not any("sponsor" in c.lower() for c in result.concerns)


class TestCompanyPreferences:
    def test_preferred_company_gets_a_boost(self, prefs, profile):
        preferred = score(make_raw(company="NVIDIA"), prefs, profile)
        unknown = score(make_raw(company="Obscure Startup LLC"), prefs, profile)
        assert preferred.score > unknown.score

    def test_unknown_company_still_competes(self, prefs, profile):
        """The company list is a preference, not a search boundary."""
        result = score(
            make_raw(company="Nobody Has Heard Of Us Inc", title="FPGA Design Intern"),
            prefs, profile,
        )
        assert result.score >= prefs.thresholds.maybe

    def test_blacklisted_company_is_excluded(self, prefs, profile):
        prefs.companies.blacklisted = ["Evil Corp"]
        excluded, reason = hard_exclusions(normalize_job(make_raw(company="Evil Corp")), prefs)
        assert excluded is True and "blacklisted" in reason

    def test_company_alias_is_respected_for_blacklisting(self, prefs, profile):
        prefs.companies.blacklisted = ["AMD"]
        job = normalize_job(make_raw(company="Advanced Micro Devices"))
        assert hard_exclusions(job, prefs)[0] is True


class TestFreshness:
    def test_newer_postings_rank_above_older_ones(self, prefs, profile):
        fresh = score(make_raw(days_ago=0), prefs, profile)
        stale = score(make_raw(days_ago=45), prefs, profile)
        assert fresh.score > stale.score

    def test_unknown_date_is_neutral(self, prefs, profile):
        raw = make_raw()
        raw.date_posted = None
        result = score(raw, prefs, profile)
        assert any("date unknown" in r.lower() for r in result.match_reasons)


class TestPriorityThresholds:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (95, Priority.APPLY_NOW),
            (85, Priority.STRONG_MATCH),
            (75, Priority.WORTH_CONSIDERING),
            (65, Priority.MAYBE),
            (30, Priority.SKIP),
        ],
    )
    def test_default_bands(self, prefs, value, expected):
        assert classify_priority(value, prefs) is expected

    def test_thresholds_are_configurable(self, prefs):
        prefs.thresholds.apply_now = 70
        assert classify_priority(75, prefs) is Priority.APPLY_NOW


class TestWeightConfiguration:
    def test_weights_are_normalised(self, prefs):
        prefs.weights.role_match = 200
        weights = prefs.weights.normalized()
        assert abs(sum(weights.values()) - 1.0) < 1e-9

    def test_reweighting_changes_ranking(self, prefs, profile):
        raw = make_raw(company="Obscure Co", location="Omaha, NE")
        prefs.weights.location = 0
        prefs.weights.company_preference = 0
        without = score(raw, prefs, profile).score
        prefs.weights.location = 60
        prefs.weights.company_preference = 60
        with_location = score(raw, prefs, profile).score
        assert without != with_location


class TestQueryGeneration:
    def test_queries_come_from_configured_roles(self, prefs):
        queries = generate_queries(prefs)
        text = " ".join(q.text.lower() for q in queries)
        assert "fpga" in text
        assert len(queries) > 5

    def test_no_duplicate_queries(self, prefs):
        queries = generate_queries(prefs)
        keys = [q.key() for q in queries]
        assert len(keys) == len(set(keys))

    def test_expansion_produces_related_terminology(self):
        variants = " ".join(expand_role("FPGA Engineer Intern")).lower()
        assert "rtl" in variants or "programmable logic" in variants

    def test_expansion_can_be_disabled(self, prefs):
        prefs.scope.query_expansion = False
        queries = generate_queries(prefs)
        assert len(queries) <= len(prefs.roles) + 12

    def test_query_budget_is_respected(self, prefs):
        prefs.scope.max_expanded_queries = 7
        assert len(generate_queries(prefs)) <= 7

    def test_empty_roles_still_yields_a_query(self, prefs):
        prefs.roles = []
        assert generate_queries(prefs)
