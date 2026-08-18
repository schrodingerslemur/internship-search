"""Ingestion: normalisation, parsing, missing fields, malformed listings."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.models.base import EmploymentType, RemoteStatus, SponsorshipStatus
from app.pipeline import extract as ex
from app.pipeline.normalize import extract_skills, normalize_all, normalize_job
from app.schemas.job import RawJob
from tests.conftest import FPGA_DESCRIPTION, make_raw


class TestNormalization:
    def test_normalizes_a_complete_listing(self, raw_fpga):
        job = normalize_job(raw_fpga)
        assert job is not None
        assert job.company == "NVIDIA"
        assert job.company_slug == "nvidia"
        assert job.employment_type is EmploymentType.INTERNSHIP
        assert job.city == "Santa Clara"
        assert job.state == "CA"
        assert job.fingerprint
        assert job.ats_identity == "greenhouse:nvidia:12345"

    def test_extracts_sections_from_a_single_blob(self, raw_fpga):
        job = normalize_job(raw_fpga)
        assert job.requirements is not None
        assert "Computer Engineering" in job.requirements
        assert job.preferred_qualifications is not None
        assert "PCIe" in job.preferred_qualifications

    def test_strips_html(self):
        raw = make_raw(description="<p>Build <b>FPGA</b> designs.</p><ul><li>Verilog</li></ul>")
        job = normalize_job(raw)
        assert "<p>" not in job.description
        assert "FPGA" in job.description

    def test_missing_company_is_dropped(self):
        assert normalize_job(make_raw(company="")) is None

    def test_missing_title_is_dropped(self):
        assert normalize_job(make_raw(title="   ")) is None

    def test_missing_optional_fields_are_tolerated(self):
        job = normalize_job(
            RawJob(source="x", source_job_id="1", title="Hardware Intern", company="Tiny Co")
        )
        assert job is not None
        assert job.location_raw is None
        assert job.remote_status is RemoteStatus.UNKNOWN
        assert job.sponsorship is SponsorshipStatus.UNKNOWN
        assert job.deadline is None

    def test_malformed_batch_does_not_abort_the_run(self):
        good = make_raw(source_job_id="ok")
        bad = make_raw(source_job_id="bad", company="")
        jobs, dropped = normalize_all([good, bad])
        assert len(jobs) == 1
        assert dropped == 1

    def test_skills_are_tagged_from_description(self, raw_fpga):
        job = normalize_job(raw_fpga)
        assert "fpga" in job.skills
        assert "systemverilog" in job.skills
        assert "uvm" in job.skills

    def test_short_skill_tokens_need_word_boundaries(self):
        # "c" must not match inside "electronic"; "go" must not match "google"
        skills = extract_skills("We use electronic design at Google.", "Engineer")
        assert "c" not in skills
        assert "go" not in skills

    def test_terms_are_extracted(self, raw_fpga):
        job = normalize_job(raw_fpga)
        assert "Summer 2026" in job.terms


class TestEmploymentType:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("FPGA Design Intern", EmploymentType.INTERNSHIP),
            ("Hardware Co-op - Fall 2026", EmploymentType.CO_OP),
            ("New Grad Software Engineer", EmploymentType.NEW_GRAD),
            ("Senior Hardware Engineer", EmploymentType.UNKNOWN),
        ],
    )
    def test_inference(self, title, expected):
        assert ex.infer_employment_type(title) is expected

    def test_declared_full_time_does_not_override_intern_title(self):
        assert (
            ex.infer_employment_type("FPGA Intern", declared="Full time")
            is EmploymentType.INTERNSHIP
        )


class TestSponsorship:
    """Unknown must never become a claim in either direction."""

    def test_silence_means_unknown(self):
        status, evidence = ex.extract_sponsorship("A great internship. Apply now!")
        assert status is SponsorshipStatus.UNKNOWN
        assert evidence is None

    def test_explicit_refusal(self):
        status, evidence = ex.extract_sponsorship(
            "We are unable to provide visa sponsorship for this role."
        )
        assert status is SponsorshipStatus.NOT_OFFERED
        assert evidence

    def test_explicit_offer(self):
        status, _ = ex.extract_sponsorship("We will sponsor visas for exceptional candidates.")
        assert status is SponsorshipStatus.OFFERED

    def test_citizenship_requirement_outranks_generic_language(self):
        status, _ = ex.extract_sponsorship(
            "Must be a U.S. citizen. We offer sponsorship for other roles."
        )
        assert status is SponsorshipStatus.CITIZENSHIP_REQUIRED

    def test_clearance_requirement(self):
        status, _ = ex.extract_sponsorship("Active TS/SCI clearance required.")
        assert status is SponsorshipStatus.SECURITY_CLEARANCE_REQUIRED


class TestDeadlines:
    def test_no_deadline_is_not_invented(self):
        assert ex.extract_deadline("Apply today! Rolling basis.") == (None, False)

    def test_explicit_future_deadline(self):
        deadline, explicit = ex.extract_deadline(
            "Applications close by December 15, 2026.", datetime(2026, 8, 18)
        )
        assert explicit is True
        assert deadline.year == 2026 and deadline.month == 12

    def test_past_deadline_is_rejected(self):
        deadline, explicit = ex.extract_deadline(
            "Applications closed March 1, 2026.", datetime(2026, 8, 18)
        )
        assert deadline is None and explicit is False


class TestSalary:
    def test_hourly_range(self):
        result = ex.extract_salary("$45 - $60 per hour")
        assert result["min"] == 45 and result["max"] == 60
        assert result["period"] == "hourly"

    def test_yearly_range(self):
        result = ex.extract_salary("Base salary $120,000-$150,000 annually")
        assert result["min"] == 120000
        assert result["period"] == "yearly"

    def test_years_of_experience_is_not_a_salary(self):
        assert ex.extract_salary("Requires 5+ years of experience")["min"] is None

    def test_absent_salary(self):
        assert ex.extract_salary("We offer competitive pay")["min"] is None


class TestExperience:
    def test_extracts_minimum_years(self):
        assert ex.extract_experience_years("Requires 5+ years of experience") == 5.0

    def test_none_when_unstated(self):
        assert ex.extract_experience_years("Open to current students") is None


class TestLocation:
    def test_parses_city_state(self):
        loc = ex.parse_location("Santa Clara, CA")
        assert loc.city == "Santa Clara" and loc.state == "CA"
        assert loc.metro == "bay_area"

    def test_remote_detection(self):
        assert ex.parse_location("Remote").is_remote is True

    def test_unknown_location_is_empty_not_wrong(self):
        loc = ex.parse_location(None)
        assert loc.city is None and loc.key == ""

    def test_description_html_only_listing(self):
        job = normalize_job(make_raw(description=FPGA_DESCRIPTION, location=None))
        assert job.location_raw is None
