"""The general agent: formats, the page model it reads, and the loop that acts on it."""

from __future__ import annotations

import re

import pytest

from app.apply import cloud, worker
from app.apply.agent import AgentResult, Observation, WebAgent, briefing
from app.apply.formats import format_of
from app.apply.profile import ApplicantProfile, StandingAnswer
from app.models import ApplyRecipe, ApplyTask
from app.models.autoapply import NEEDS_YOU, SUBMITTED
from tests.test_autoapply import PROFILE, account, make_job, worker_db  # noqa: F401 - fixtures


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://jobs.lever.co/acme/123/apply", "lever"),
        ("https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/X", "workday"),
        ("https://jobs.smartrecruiters.com/WesternDigital/1", "smartrecruiters"),
        ("https://careers.nvidia.com/job/1", "site:nvidia.com"),
        ("https://www.jobs.example.co.uk/x", "site:example.co.uk"),
    ],
)
def test_format_of_names_the_ats_or_the_site(url, key):
    assert format_of(url)[0] == key


def test_format_of_without_a_url():
    assert format_of(None) is None and format_of("not a url") is None


def test_briefing_reads_standing_answers_as_plain_questions():
    profile = ApplicantProfile(first_name="Ada", last_name="L")
    profile.standing_answers = [StandingAnswer(match=r"^\s*What is your GPA\?\s*$", answer="3.9")]
    text = briefing(profile, resume="FPGA work", company="Acme", title="Intern", posting="We build chips")
    assert "- What is your GPA? -> 3.9" in text
    assert "Standing answer for questions like" not in text


def test_an_observation_renders_ids_labels_and_state():
    obs = Observation(
        url="https://x", title="Apply", text="Apply now", errors=["Email is required"],
        items=[{"id": "e1", "tag": "input", "type": "email", "role": "", "label": "Email", "required": True,
                "value": "", "invalid": True},
               {"id": "f1.e2", "tag": "select", "type": "", "role": "", "label": "Country",
                "options": ["US", "Canada"], "value": "US"}],
    )
    text = obs.render()
    assert 'e1 input[email] "Email" REQUIRED INVALID' in text
    assert 'f1.e2 select "Country" value="US" options=["US", "Canada"]' in text
    assert "Errors shown: Email is required" in text


# ---------------------------------------------------------- in a real browser


FORM = """<!doctype html><html><body>
<h1>Apply</h1>
<iframe id="f" srcdoc='
<form id="p1">
  <label for="fn">First name *</label><input id="fn" required>
  <label for="cty">Country</label><select id="cty"><option>Select</option><option>United States</option></select>
  <label><input type="checkbox" id="ok"> I agree to the privacy policy</label>
  <label for="cv">Resume</label><input type="file" id="cv">
  <button type="button" id="go">Submit application</button>
<script>
  document.getElementById("go").onclick = () => {
    if (!document.getElementById("fn").value) return;
    document.body.innerHTML = "<p>Thank you for applying! We have your application.</p>";
  };
</script>
</form>'></iframe>
</body></html>"""


def _id(prompt: str, label: str) -> str:
    match = re.search(rf"^\s+(\S+) \S+ \"{re.escape(label)}", prompt, re.M)
    assert match, f"{label!r} not in the page model"
    return match.group(1)


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # no browser installed here
            pytest.skip(f"no chromium: {exc}")
        yield browser.new_page()
        browser.close()


def test_the_agent_fills_a_form_inside_an_iframe_and_confirms(page, tmp_path):
    html = tmp_path / "form.html"
    html.write_text(FORM, encoding="utf-8")
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF")
    turns = []

    def brain(system, prompt):
        turns.append(prompt)
        if len(turns) == 1:
            assert "How-to for this site" in prompt and "1. Click Apply" in prompt
            return {"actions": [
                {"do": "fill", "id": _id(prompt, "First name"), "value": "Ada"},
                {"do": "select", "id": _id(prompt, "Country"), "value": "United States"},
                {"do": "check", "id": _id(prompt, "I agree")},
                {"do": "upload", "id": _id(prompt, "Resume"), "file": "resume"},
            ], "status": "continue", "filled": [{"label": "First name", "value": "Ada"}]}
        if len(turns) == 2:
            assert 'value="Ada"' in prompt and "files=['cv.pdf']" in prompt and "checked" in prompt
            # Claiming success before submitting is not believed.
            return {"actions": [], "status": "submitted", "evidence": "Application received"}
        if len(turns) == 3:
            assert "no confirmation yet" in prompt
            return {"actions": [{"do": "click", "id": _id(prompt, "Submit application")}], "status": "continue"}
        return {"actions": [], "status": "submitted", "evidence": "Thank you for applying!",
                "lessons": "1. Fill the iframe form\n2. Click Submit application"}

    agent = WebAgent(page, brain, briefing="Candidate: Ada", files={"resume": str(resume)},
                     recipe="1. Click Apply")
    result = agent.run(html.as_uri())
    assert result.status == "submitted", result.detail
    assert result.lessons.startswith("1. Fill the iframe form")
    assert result.filled == [{"label": "First name", "value": "Ada"}]
    assert len(turns) == 4


def test_a_missing_fact_comes_back_as_a_question(page, tmp_path):
    html = tmp_path / "form.html"
    html.write_text("<label for=g>GPA *</label><input id=g required>", encoding="utf-8")

    def brain(system, prompt):
        return {"actions": [], "status": "needs_you", "reason": "It asks for a GPA.",
                "questions": [{"label": "GPA *", "type": "text"}], "lessons": "1. One page"}

    result = WebAgent(page, brain, briefing="", files={}).run(html.as_uri())
    assert result.status == "needs_you" and result.detail == "It asks for a GPA."
    assert result.questions == [{"label": "GPA *", "type": "text", "options": []}]


def test_a_silent_model_hands_the_task_back(page, tmp_path):
    html = tmp_path / "form.html"
    html.write_text("<input>", encoding="utf-8")
    result = WebAgent(page, lambda s, p: None, briefing="", files={}).run(html.as_uri())
    assert result.status == "needs_you"


# ------------------------------------------------------------------ worker


def test_the_worker_runs_the_agent_and_keeps_its_how_to(session, account, worker_db, monkeypatch):  # noqa: F811
    job = make_job(session, 1, greenhouse=False, application_url="https://jobs.lever.co/acme/1/apply")
    cloud.save_record(session, account, data=PROFILE, resume=("cv.pdf", b"%PDF"))
    cloud.save_document(session, account, "transcript", "grades.pdf", b"%PDF-t")
    task = cloud.enqueue(session, account, job)
    session.commit()
    seen = {}

    def fake_run(url, answerer, *, briefing, files, recipe, task_id, dry_run):
        seen.update(url=url, files=sorted(files), recipe=recipe)
        return AgentResult("submitted", "ok", lessons="1. Click Apply\n2. Upload resume",
                           filled=[{"label": "First name", "value": "Ada"}]), b"jpg"

    monkeypatch.setattr(worker, "_run_agent", fake_run)
    assert worker.run(say=lambda _: None) == {SUBMITTED: 1}
    assert seen == {"url": "https://jobs.lever.co/acme/1/apply", "files": ["resume", "transcript"], "recipe": ""}
    session.expire_all()
    recipe = session.query(ApplyRecipe).filter_by(format_key="lever").one()
    assert recipe.successes == 1 and recipe.notes.startswith("1. Click Apply")
    done = session.get(ApplyTask, task.id)
    assert done.status == SUBMITTED and done.answers[0]["source"] == "agent"

    # The next Lever form starts from what this one taught it.
    job2 = make_job(session, 2, greenhouse=False, application_url="https://jobs.lever.co/acme/2/apply")
    cloud.enqueue(session, account, job2)
    session.commit()

    def needs_gpa(url, answerer, **k):
        seen["recipe"] = k["recipe"]
        return AgentResult("needs_you", "It asks for a GPA.", lessons="",
                           questions=[{"label": "GPA", "type": "text", "options": []},
                                      {"label": "Writing sample", "type": "file", "options": []}]), None

    monkeypatch.setattr(worker, "_run_agent", needs_gpa)
    assert worker.run(say=lambda _: None) == {NEEDS_YOU: 1}
    assert seen["recipe"].startswith("1. Click Apply")
    session.expire_all()
    recipe = session.query(ApplyRecipe).filter_by(format_key="lever").one()
    assert recipe.failures == 1 and recipe.notes.startswith("1. Click Apply")  # kept: nothing new learned
    left = session.query(ApplyTask).filter_by(job_id=job2.id).one().left_for_you
    assert left[1]["kind"] == "writing_sample"
