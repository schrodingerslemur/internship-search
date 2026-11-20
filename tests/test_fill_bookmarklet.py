"""Fill it myself: the bookmarklet in a real browser, and the code that feeds it."""

from __future__ import annotations

import base64
import json

import pytest

from app.apply import cloud
from app.config import PROJECT_ROOT
from app.models import ApplyTask
from tests.test_agent import page  # noqa: F401 - fixture

FILL_JS = (PROJECT_ROOT / "app" / "web" / "static" / "fill.js").read_text(encoding="utf-8")

FORM = """<!doctype html><html><body><form onsubmit="window.SUBMITTED=1;return false">
<label for="fn">First Name *</label><input id="fn" name="first_name">
<label for="ln">Last Name *</label><input id="ln" name="last_name">
<label for="em">Email</label><input id="em" type="email">
<label for="why">Why do you want to work at Reliable Robotics? *</label><textarea id="why"></textarea>
<label for="auth">Are you authorized to work in the US?</label>
<select id="auth"><option value="">Select</option><option value="y">Yes</option><option value="n">No</option></select>
<fieldset><legend>Will you require sponsorship?</legend>
  <label><input type="radio" name="sp" value="1"> Yes</label>
  <label><input type="radio" name="sp" value="0"> No</label></fieldset>
<label for="cv">Resume</label><input id="cv" type="file">
<label for="pre">Preferred name</label><input id="pre" value="Kept">
<button type="submit">Submit application</button></form></body></html>"""


def _code(payload: dict) -> str:
    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_it_fills_what_matches_and_never_submits(page, tmp_path):  # noqa: F811
    form = tmp_path / "form.html"
    form.write_text(FORM, encoding="utf-8")
    code = _code({
        "answers": [
            {"label": "Why do you want to work at Reliable Robotics?", "value": "I build FPGA flight systems — é."},
            {"label": "Are you authorized to work in the US?", "value": "Yes"},
            {"label": "Will you require sponsorship?", "value": "No"},
            {"label": "Preferred name", "value": "Overwritten?"},
            {"label": "Favourite colour", "value": "Blue"},
        ],
        "profile": {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
        "ask": ["Desired salary"],
    })
    page.goto(form.as_uri() + "#isfill=" + code)
    page.evaluate(FILL_JS)

    assert page.input_value("#fn") == "Ada" and page.input_value("#ln") == "Lovelace"
    assert page.input_value("#em") == "ada@example.com"
    assert page.input_value("#why") == "I build FPGA flight systems — é."
    assert page.input_value("#auth") == "y"
    assert page.is_checked('input[name=sp][value="0"]')
    assert page.input_value("#pre") == "Kept"  # never overwrites
    assert page.evaluate("window.SUBMITTED") is None
    assert "isfill" not in page.url  # cleared from the address bar

    panel = page.inner_text("#isfill-panel")
    assert "Nothing was submitted" in panel
    assert "Upload yourself: Resume" in panel
    assert "Still yours to answer: Desired salary" in panel
    assert "Favourite colour: Blue" in panel and "Preferred name" in panel


def test_the_code_carries_answers_contacts_and_whats_left(session):
    from app.services import auth

    user = auth.create_account(session, email="me@example.com", password="a-good-password")
    record = cloud.save_record(session, user, data={"first_name": "Ada", "phone": "555", "us_citizen": True})
    task = ApplyTask(answers=[
        {"label": "Why us?", "value": "Because FPGAs.", "type": "textarea"},
        {"label": "Resume", "value": "cv.pdf", "type": "file"},
        {"label": "Empty", "value": ""},
    ], left_for_you=[{"label": "GPA", "type": "text"}, {"label": "Writing sample", "type": "file"}, "Salary"])
    code = cloud.fill_code(task, record)
    data = json.loads(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)))
    assert data == {
        "answers": [{"label": "Why us?", "value": "Because FPGAs."}],
        "profile": {"first_name": "Ada", "phone": "555"},
        "ask": ["GPA", "Salary"],
    }


def test_the_agent_page_offers_it_only_where_useful(session):
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import create_app
    from app.models.autoapply import NEEDS_YOU
    from app.services import auth
    from tests.test_autoapply import make_job

    user = auth.create_account(session, email="me@example.com", password="a-good-password")
    job = make_job(session, 1)
    cloud.save_record(session, user, data={"first_name": "Ada"})
    task = cloud.enqueue(session, user, job)
    session.commit()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"email": "me@example.com", "password": "a-good-password"})
        assert 'data-fill="' not in c.get("/autoapply").text  # still queued
        task.status = NEEDS_YOU
        session.commit()
        page_html = c.get("/autoapply").text
        assert 'data-fill="' in page_html and 'href="javascript:' in page_html


@pytest.mark.parametrize("bad", ["not-base64!", ""])
def test_a_bad_code_is_reported_not_thrown(page, tmp_path, bad):  # noqa: F811
    form = tmp_path / "form.html"
    form.write_text(FORM, encoding="utf-8")
    page.goto(form.as_uri() + "#isfill=" + bad)
    seen = []
    page.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    page.evaluate(FILL_JS)
    assert seen and "could not be read" in seen[0]
