"""Login, logout, and account creation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import auth
from app.web.templating import templates

router = APIRouter()


def _signing_key(request: Request) -> bytes:
    """The app-wide key loaded at startup, so cookies verify without a query."""
    return request.app.state.signing_key


def _redirect_with_session(user, key: bytes, destination: str = "/") -> RedirectResponse:
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(user, key),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # Set only over HTTPS in production; a plain-HTTP local run still works.
        secure=destination.startswith("https") or False,
        path="/",
    )
    return response


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"mode": "login", "any_accounts": auth.account_count(db) > 0},
    )


@router.post("/login", include_in_schema=False)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = auth.authenticate(db, email, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "mode": "login",
                "any_accounts": True,
                "error": "That email and password do not match an account.",
                "email": email,
            },
            status_code=401,
        )
    db.commit()
    return _redirect_with_session(user, _signing_key(request))


@router.get("/signup", response_class=HTMLResponse, include_in_schema=False)
async def signup_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"mode": "signup", "any_accounts": auth.account_count(db) > 0},
    )


@router.post("/signup", include_in_schema=False)
async def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    db: Session = Depends(get_db),
):
    def fail(message: str, status: int = 400):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"mode": "signup", "error": message, "email": email, "name": name},
            status_code=status,
        )

    email_clean = auth.normalise_email(email)
    if "@" not in email_clean:
        return fail("That does not look like an email address.")

    problem = auth.password_problem(password)
    if problem:
        return fail(problem)

    existing = auth.find_by_email(db, email_clean)
    if existing is not None:
        if existing.password_hash is None:
            auth.claim_account(
                db, existing, email=email_clean, password=password, name=name or None
            )
            db.commit()
            return _redirect_with_session(existing, _signing_key(request))
        return fail("An account with that email already exists.", status=409)

    # First signup on an instance that predates accounts: adopt the existing
    # row so its tracker, preferences and profile come with it.
    legacy = auth.claimable_legacy_account(db)
    if legacy is not None:
        auth.claim_account(db, legacy, email=email_clean, password=password, name=name or None)
        db.commit()
        return _redirect_with_session(legacy, _signing_key(request))

    user = auth.create_account(db, email=email_clean, password=password, name=name or None)
    db.commit()
    return _redirect_with_session(user, _signing_key(request))


@router.post("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response
