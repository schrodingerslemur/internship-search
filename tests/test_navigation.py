"""The three-tab navigation, the Applications sub-tabs, and model links on Welcome."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import create_app
from app.services import auth, onboarding


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def signed_in(client, session):
    auth.create_account(session, email="owner@example.com", password="a-good-password")
    session.commit()
    client.post("/login", data={"email": "owner@example.com", "password": "a-good-password"})
    return client


@pytest.mark.parametrize("path", ["/saved", "/applied", "/tracker", "/autoapply"])
def test_every_application_page_has_the_sub_tabs(signed_in, path):
    html = signed_in.get(path).text
    assert 'aria-label="Applications"' in html
    assert html.count('id="nav-applied-count"') == 1
    assert ">Tracker</a>" in html and ">Agent</a>" in html


@pytest.mark.parametrize("path", ["/", "/settings", "/profile"])
def test_other_pages_keep_one_hidden_count_target(signed_in, path):
    html = signed_in.get(path).text
    assert html.count('id="nav-applied-count"') == 1
    assert 'aria-label="Applications"' not in html


def test_the_account_menu_is_short(signed_in):
    html = signed_in.get("/").text
    for gone in ('href="/coverage">Search insights', 'href="/dismissed">Dismissed jobs</a>\n        <a'):
        assert gone not in html
    assert 'href="/dismissed">Dismissed jobs' in html  # now on Review itself


def test_welcome_links_to_every_provider_key_page(client, session):
    auth.create_account(session, email="owner@example.com", password="a-good-password")
    user = auth.create_account(session, email="ada@example.com", password="a-good-password")
    onboarding.begin(session, user)
    session.commit()
    client.post("/login", data={"email": "ada@example.com", "password": "a-good-password"})
    html = client.get("/welcome").text
    for url in (
        "https://console.groq.com/keys",
        "https://console.anthropic.com/settings/keys",
        "https://aistudio.google.com/apikey",
        "https://platform.openai.com/api-keys",
    ):
        assert url in html
    assert "/model?provider=groq&next=/welcome" in html


def test_the_model_page_returns_to_welcome_but_nowhere_else(signed_in):
    html = signed_in.get("/model?provider=gemini&next=/welcome").text
    assert 'name="next" value="/welcome"' in html
    assert 'value="gemini" checked' in html
    assert 'name="next"' not in signed_in.get("/model?next=https://evil.example").text
