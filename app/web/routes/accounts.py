"""Login, logout, and account creation."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.logging_setup import get_logger
from app.services import auth
from app.web.templating import templates

log = get_logger("accounts")

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


# --------------------------------------------------------------------------
# Forgotten passwords
# --------------------------------------------------------------------------

#: Shown whether or not the address exists, so this page cannot be used to
#: discover who has an account on this instance.
SENT_MESSAGE = (
    "If that address has an account, a reset link is on its way. "
    "It expires in an hour."
)


@router.get("/forgot", response_class=HTMLResponse, include_in_schema=False)
async def forgot_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"mode": "forgot"})


@router.post("/forgot", include_in_schema=False)
async def forgot(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    from app.notify.base import NotificationMessage
    from app.notify.providers import EmailProvider
    from app.services import password_reset

    # Checked before the lookup, and without reference to any address: if this
    # answered differently for a known and an unknown email, the page would
    # become a way to discover who has an account here.
    if not get_settings().smtp_configured:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "mode": "forgot",
                "error": (
                    "Email is not configured on this deployment, so reset "
                    "links cannot be sent. Use the set-password command."
                ),
            },
            status_code=503,
        )

    user = auth.find_by_email(db, email)

    if user is not None and user.password_hash:
        # Sent to the account's login address, never to digest_email: a digest
        # may be forwarded somewhere shared, a reset link must not be.
        provider = EmailProvider(user.email)
        base = str(request.base_url).rstrip("/")
        link = f"{base}/reset/{password_reset.issue(user, _signing_key(request))}"
        subject, text, html = password_reset.build_email(user, link)
        result = await provider.send(NotificationMessage(text=text, subject=subject, html=html))
        log.info("auth.reset_requested", user_id=user.id, sent=result.ok)

    return templates.TemplateResponse(
        request, "login.html", {"mode": "forgot", "notice": SENT_MESSAGE}
    )


def _expired(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"mode": "forgot", "error": "That reset link has expired or already been used."},
        status_code=400,
    )


@router.get("/reset/{token}", response_class=HTMLResponse, include_in_schema=False)
async def reset_form(token: str, request: Request, db: Session = Depends(get_db)):
    from app.services import password_reset

    if password_reset.verify(db, token, _signing_key(request)) is None:
        return _expired(request)
    return templates.TemplateResponse(request, "login.html", {"mode": "reset", "token": token})


@router.post("/reset/{token}", include_in_schema=False)
async def reset(
    token: str, request: Request, password: str = Form(...), db: Session = Depends(get_db)
):
    from app.services import password_reset

    user = password_reset.verify(db, token, _signing_key(request))
    if user is None:
        return _expired(request)

    problem = auth.password_problem(password)
    if problem:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"mode": "reset", "token": token, "error": problem},
            status_code=400,
        )

    # Changing the hash re-derives the link-signing key, which retires this
    # link and every other outstanding one for the account.
    user.password_hash = auth.hash_password(password)
    db.flush()
    db.commit()
    log.info("auth.password_reset", user_id=user.id)
    return _redirect_with_session(user, _signing_key(request))


@router.post("/settings/password", include_in_schema=False)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Change your own password from Settings, proving you know the old one."""
    user_id = getattr(request.state, "user_id", None)
    user = auth.get_user(db, user_id) if user_id else None
    if user is None:
        return RedirectResponse("/login", status_code=303)

    def back(message: str, ok: bool = False):
        return RedirectResponse(
            f"/settings?{'password_ok' if ok else 'password_error'}={quote(message)}",
            status_code=303,
        )

    if not auth.verify_password(current_password, user.password_hash):
        return back("Your current password is not correct.")

    problem = auth.password_problem(new_password)
    if problem:
        return back(problem)

    user.password_hash = auth.hash_password(new_password)
    db.flush()
    db.commit()
    log.info("auth.password_changed", user_id=user.id)

    # Re-issue the cookie: it is signed with the app key, so it survives a
    # password change, but refreshing it keeps the session tied to the act.
    response = back("Password updated.", ok=True)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(user, _signing_key(request)),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response
