"""Application assistant: form identity, answering rules, picking, the ledger.

The browser half is exercised against live forms by hand (``apply --job URL
--check``); everything that decides *what* goes into a form is tested here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.apply import ledger
from app.apply.answers import (
    build_draft,
    graduation_option,
    loose_option,
    prepare_answers,
)
from app.apply.greenhouse import GreenhouseJob, identify, parse_questions
from app.apply.picker import pick
from app.apply.profile import ApplicantProfile, StandingAnswer
from app.models import Job, JobListing
from app.models.base import Freshness, JobStatus, Priority
from tests.conftest import NOW


@pytest.fixture
def applicant(tmp_path) -> ApplicantProfile:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    return ApplicantProfile(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.edu",
        phone="(555) 010-0000",
        location="Pittsburgh, PA",
        linkedin="https://www.linkedin.com/in/ada",
        github="https://github.com/ada",
        school="Carnegie Mellon University",
        degree="Bachelor's",
        discipline="Electrical and Computer Engineering",
        graduation="December 2027",
        graduation_month=12,
        graduation_year=2027,
        us_citizen=True,
        work_authorized_us=True,
        requires_sponsorship=False,
        willing_to_relocate=True,
        terms=["Summer 2027"],
        earliest_start="May 1, 2027",
        resume_path=str(resume),
    )


def q(name, label, type_="input_text", required=True, values=()):
    return {
        "label": label,
        "required": required,
        "fields": [
            {"name": name, "type": type_, "values": [{"label": v, "value": str(i)} for i, v in enumerate(values)]}
        ],
    }


# Shaped like Jump Trading's live form, which is where these rules were checked.
PAYLOAD = {
    "questions": [
        q("first_name", "First Name"),
        q("last_name", "Last Name"),
        q("email", "Email"),
        q("phone", "Phone"),
        {
            "label": "Resume/CV",
            "required": True,
            "fields": [
                {"name": "resume", "type": "input_file", "values": []},
                {"name": "resume_text", "type": "textarea", "values": []},
            ],
        },
        q("cover_letter", "Cover Letter", "input_file", required=False),
        q("question_1", "LinkedIn Profile", required=False),
        q("question_2", "Non-compete/Notice period comments"),
        q("question_3[]", "Review our Notice at Collection to learn how we will process your personal data.",
          "multi_value_multi_select", values=["Acknowledge/Confirm"]),
        q("question_4", "Will you require sponsorship for work authorization in the future?",
          "multi_value_single_select", values=["Yes", "No"]),
        q("question_5", "Please select your current school from the list below:",
          "multi_value_single_select", values=["Carleton University", "Carnegie Mellon University", "Case Western"]),
        q("question_6[]", "What degree are you currently pursuing?",
          "multi_value_multi_select", values=["Bachelor�s", "Master�s", "PhD"]),
        q("question_7", "What is your expected graduation date?",
          "multi_value_single_select", values=["Spring/Summer 2027", "Fall 2027", "Winter 2027", "Spring 2028"]),
        q("question_8", "Do you currently have any offers from other firms or deadlines we should be aware of?",
          "multi_value_single_select", values=["Yes", "No"]),
        q("question_9", "How did you hear about us?", "multi_value_single_select", required=False,
          values=["Friend / word of mouth", "Jump Website / Careers Page", "Other"]),
    ],
    "location_questions": [
        {"label": "Latitude", "required": True, "fields": [{"name": "latitude", "type": "input_hidden"}]},
        {"label": "Location", "required": True, "fields": [{"name": "location", "type": "input_text"}]},
    ],
    "compliance": [
        {
            "type": "eeoc",
            "questions": [
                q("gender", "Gender", "multi_value_single_select", required=False,
                  values=["Male", "Female", "Decline To Self Identify"]),
                q("veteran_status", "Veteran Status", "multi_value_single_select", required=False,
                  values=["I am not a protected veteran", "I don't wish to answer"]),
            ],
        }
    ],
}


def by_name(answers):
    return {a.name: a for a in answers}


class TestIdentify:
    @pytest.mark.parametrize(
        "url",
        [
            "https://job-boards.greenhouse.io/acme/jobs/123",
            "https://boards.greenhouse.io/acme/jobs/123?gh_src=x",
            "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=123",
        ],
    )
    def test_greenhouse_urls(self, url):
        assert identify(url) == GreenhouseJob("acme", "123")

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "https://jobs.lever.co/acme/abc",
            # A careers page carrying only the job id cannot address the form.
            "https://www.acme.com/careers?gh_jid=123",
        ],
    )
    def test_not_addressable(self, url):
        assert identify(url) is None

    def test_form_url_is_the_bare_embed(self):
        assert GreenhouseJob("acme", "123").form_url.endswith("embed/job_app?for=acme&token=123")


class TestParse:
    def test_alternative_fields_and_hidden_inputs_are_skipped(self):
        names = [x.name for x in parse_questions(PAYLOAD)]
        assert "resume" in names and "resume_text" not in names
        assert "latitude" not in names
        assert "location" in names and "gender" in names


class TestAnswers:
    def test_profile_and_rules(self, applicant):
        a = by_name(prepare_answers(parse_questions(PAYLOAD), applicant, company="Jump"))
        assert a["first_name"].value == "Ada"
        assert a["resume"].value == applicant.resume_path
        assert a["location"].value == "Pittsburgh, PA"
        assert a["question_1"].value == applicant.linkedin
        assert a["question_3[]"].value == ["Acknowledge/Confirm"]
        assert a["question_4"].value == "No"
        assert a["question_5"].value == "Carnegie Mellon University"
        assert a["question_6[]"].value == ["Bachelor�s"]
        assert a["question_9"].value == "Jump Website / Careers Page"

    def test_december_graduation_prefers_fall_over_winter(self, applicant):
        options = ["Spring/Summer 2027", "Winter 2027", "Fall 2027"]
        assert graduation_option(options, applicant) == "Fall 2027"
        assert graduation_option(["Dec 2027", "Fall 2027"], applicant) == "Dec 2027"

    def test_demographics_are_declined(self, applicant):
        a = by_name(prepare_answers(parse_questions(PAYLOAD), applicant))
        assert a["gender"].value == "Decline To Self Identify"
        assert a["veteran_status"].value == "I don't wish to answer"

    def test_personal_questions_are_left_for_the_applicant(self, applicant):
        answers = prepare_answers(parse_questions(PAYLOAD), applicant)
        draft = build_draft(key="k", canonical_job_id=None, company="", title="", form_url="",
                            answers=answers)
        assert {x.name for x in draft.unresolved} == {"question_2", "question_8"}

    def test_standing_answer_overrides(self, applicant):
        applicant.standing_answers = [StandingAnswer(match=r"offers from other", answer="No")]
        a = by_name(prepare_answers(parse_questions(PAYLOAD), applicant))
        assert a["question_8"].value == "No"
        assert a["question_8"].source == "standing"

    def test_cover_letter_is_never_invented(self, applicant):
        a = by_name(prepare_answers(parse_questions(PAYLOAD), applicant))
        assert not a["cover_letter"].answered


class FakeModel:
    def __init__(self, replies):
        self.replies = replies
        self.asked = []

    def answer_application_questions(self, *, facts, resume, posting, questions):
        self.asked = [x["name"] for x in questions]
        return self.replies


class TestModel:
    def test_only_unanswered_required_questions_are_asked(self, applicant):
        model = FakeModel({})
        prepare_answers(parse_questions(PAYLOAD), applicant, llm=model)
        assert set(model.asked) == {"question_2", "question_8"}

    def test_unconfident_answers_are_discarded(self, applicant):
        model = FakeModel({"question_2": {"answer": "None", "confident": False}})
        a = by_name(prepare_answers(parse_questions(PAYLOAD), applicant, llm=model))
        assert not a["question_2"].answered

    def test_a_select_answer_must_be_one_of_the_options(self, applicant):
        model = FakeModel({
            "question_8": {"answer": "Maybe", "confident": True},
            "question_2": {"answer": "No non-compete.", "confident": True},
        })
        a = by_name(prepare_answers(parse_questions(PAYLOAD), applicant, llm=model))
        assert not a["question_8"].answered
        assert a["question_2"].value == "No non-compete."
        assert a["question_2"].source == "ai"


def test_loose_option_matching():
    assert loose_option(["Yes", "No, I do not"], "No") == "No, I do not"
    assert loose_option(["Bachelor’s", "Master’s"], "bachelor's") == "Bachelor’s"
    assert loose_option(["Yes", "No"], "Maybe") is None


# ------------------------------------------------------------------- picking


@pytest.fixture
def user(session):
    from app.services.preferences import get_or_create_user

    return get_or_create_user(session)


def make_job(session, cid, *, source="greenhouse", source_job_id="acme:1", **kwargs) -> Job:
    defaults = dict(
        canonical_job_id=cid,
        fingerprint="fp",
        company_name="Acme",
        title="FPGA Intern",
        application_url="https://example.com/apply",
        relevance_score=90.0,
        priority=Priority.APPLY_NOW.value,
        status=JobStatus.NEW.value,
        freshness=Freshness.NEW.value,
        is_active=True,
        date_discovered=NOW,
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    session.add(job)
    session.flush()
    session.add(JobListing(job_id=job.id, source=source, source_job_id=source_job_id))
    session.flush()
    session.refresh(job)
    return job


class TestPick:
    def test_best_greenhouse_matches_first(self, session, user, applicant):
        make_job(session, "low", source_job_id="acme:1", relevance_score=80.0,
                 priority=Priority.STRONG_MATCH.value)
        make_job(session, "high", source_job_id="acme:2", relevance_score=95.0)
        picked = pick(session, user, applicant, limit=5, skip_keys=set())
        assert [c.job.canonical_job_id for c in picked] == ["high", "low"]
        assert picked[0].form == GreenhouseJob("acme", "2")

    def test_exclusions(self, session, user, applicant):
        from app.services import user_jobs

        make_job(session, "lever", source="lever", source_job_id="acme:x")
        make_job(session, "weak", source_job_id="acme:3", priority=Priority.MAYBE.value)
        make_job(session, "closed", source_job_id="acme:4", is_active=False)
        make_job(session, "past", source_job_id="acme:5", deadline=NOW - timedelta(days=400))
        applied = make_job(session, "applied", source_job_id="acme:6")
        user_jobs.set_status(session, user, applied, JobStatus.APPLIED.value)
        make_job(session, "blocked", source_job_id="evil:7", company_name="Evil Corp")
        make_job(session, "seen", source_job_id="acme:8")
        applicant.never_apply_to = ["evil"]

        picked = pick(session, user, applicant, limit=10, skip_keys={"greenhouse:acme:8"})
        assert picked == []

    def test_saved_jobs_are_still_candidates(self, session, user, applicant):
        from app.services import user_jobs

        job = make_job(session, "saved", source_job_id="acme:9")
        user_jobs.set_status(session, user, job, JobStatus.SAVED.value)
        assert [c.job.canonical_job_id for c in pick(session, user, applicant, limit=5, skip_keys=set())] == ["saved"]

    def test_limit(self, session, user, applicant):
        for i in range(4):
            make_job(session, f"j{i}", source_job_id=f"acme:{i}")
        assert len(pick(session, user, applicant, limit=2, skip_keys=set())) == 2


# -------------------------------------------------------------------- ledger


def test_ledger_round_trip_and_daily_cap(applicant):
    from datetime import date

    answers = prepare_answers(parse_questions(PAYLOAD), applicant)
    draft = build_draft(key="greenhouse:acme:1", canonical_job_id="c1", company="Acme",
                        title="FPGA Intern", form_url="u", answers=answers)
    ledger.record_draft(draft)
    assert ledger.prepared_on(date.today()) == 1
    [waiting] = ledger.waiting()
    assert waiting.key == draft.key
    assert {a.name for a in waiting.unresolved} == {"question_2", "question_8"}

    ledger.set_status(draft.key, ledger.SUBMITTED)
    assert ledger.waiting() == []
    assert "greenhouse:acme:1" in ledger.known_keys()


class TestResolveUser:
    def _two_accounts(self, session):
        from app.models.user import User

        first = User(email="first@example.com", is_active=True)
        second = User(email="me@example.com", is_active=True)
        session.add_all([first, second])
        session.flush()
        return first, second

    def test_shared_database_falls_back_to_the_applicants_email(self, session):
        from app.apply.run import resolve_user

        _, me = self._two_accounts(session)
        assert resolve_user(session, None, fallback_email="ME@example.com") is me

    def test_explicit_email_wins(self, session):
        from app.apply.run import resolve_user

        first, _ = self._two_accounts(session)
        assert resolve_user(session, "first@example.com", fallback_email="me@example.com") is first

    def test_no_match_still_asks_for_an_email(self, session, monkeypatch):
        from app.apply import run

        self._two_accounts(session)
        monkeypatch.setattr("app.config.get_settings", lambda: type("S", (), {"email_to": ""})())
        with pytest.raises(LookupError, match="pass --email"):
            run.resolve_user(session, None, fallback_email="nobody@example.com")


class TestLiveFormRules:
    """Questions met on live forms (QRT, SK hynix, Astera Labs) in November 2026."""

    def _answers(self, applicant, *questions):
        from app.apply.answers import prepare_answers

        parsed = parse_questions({"questions": list(questions)})
        return {a.name: a for a in prepare_answers(parsed, applicant, company="Acme")}

    def test_major_lands_on_the_closest_option(self, applicant):
        options = ["Computer Science", "Computer Engineering", "Electrical and Electronic Engineering",
                   "Finance", "Other"]
        a = self._answers(applicant, q("q1", "Field of Study/Major", "multi_value_single_select",
                                       values=options))["q1"]
        assert a.value == "Computer Engineering"

    def test_unrelated_majors_are_left_alone(self, applicant):
        a = self._answers(applicant, q("q1", "Field of Study/Major", "multi_value_single_select",
                                       values=["Finance", "Economics", "Physics"]))["q1"]
        assert not a.answered

    def test_onsite_requirement_follows_relocation(self, applicant):
        label = ("This role requires you to work onsite 5 days a week at our San Jose location. "
                 "Is that a requirement you can meet?")
        a = self._answers(applicant, q("q1", label, "multi_value_single_select",
                                       values=["Yes", "No"]))["q1"]
        assert a.value == "Yes"

    def test_legal_names_come_from_the_profile(self, applicant):
        got = self._answers(applicant, q("q1", "Legal First Name"), q("q2", "Legal Last Name"))
        assert (got["q1"].value, got["q2"].value) == ("Ada", "Lovelace")
        assert got["q1"].source == "rule"

    def test_pronouns_are_declined(self, applicant):
        a = self._answers(applicant, q("q1", "What are your preferred pronouns?", "multi_value_single_select",
                                       values=["She/Her", "He/Him", "They/Them", "Prefer not to say"]))["q1"]
        assert a.value == "Prefer not to say"
