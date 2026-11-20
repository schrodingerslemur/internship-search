"""Welcome: a new account's documents, short chat and review
(app/services/onboarding.py)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services import onboarding, user_llm
from app.web.deps import current_user
from app.web.templating import templates

router = APIRouter()

#: Larger uploads are refused rather than read into memory.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: The review form, in order: (heading, [(field, label, help)]).
SECTIONS = [
    ("What you want", [
        ("roles", "Roles", "Internship titles, one per line."),
        ("keywords", "Keywords", "Skills and topics a good posting would mention."),
        ("avoid_keywords", "Avoid", "Words that make a posting wrong for you."),
        ("locations", "Locations", "Cities you'd like, one per line."),
        ("remote_ok", "Remote is fine", ""),
        ("willing_to_relocate", "Willing to relocate", ""),
        ("seasons", "Terms", "e.g. Summer 2027."),
        ("companies", "Companies you'd love", ""),
        ("industries", "Industries", ""),
        ("avoid_companies", "Never apply to", "Company names."),
    ]),
    ("Whether you can work there", [
        ("us_citizen", "US citizen", ""),
        ("work_authorized_us", "Authorized to work in the US", ""),
        ("requires_sponsorship", "Needs visa sponsorship", ""),
    ]),
    ("You, for application forms", [
        ("first_name", "First name", ""),
        ("last_name", "Last name", ""),
        ("phone", "Phone", ""),
        ("location", "Where you live", "As a form would want it, e.g. Pittsburgh, PA."),
        ("linkedin", "LinkedIn", ""),
        ("github", "GitHub", ""),
        ("website", "Website", ""),
    ]),
    ("School", [
        ("school", "School", ""),
        ("degree", "Degree", "e.g. Bachelor's."),
        ("major", "Major", ""),
        ("graduation_month", "Graduation month", "1-12."),
        ("graduation_year", "Graduation year", ""),
        ("gpa", "GPA", ""),
        ("coursework", "Coursework", ""),
    ]),
    ("What you've done", [
        ("skills", "Skills", ""),
        ("programming_languages", "Languages", ""),
        ("projects", "Projects", "One per line."),
        ("previous_internships", "Internships", "One per line."),
        ("summary", "Summary", "One sentence about you."),
    ]),
]


def _page_has_model(db: Session, user: User) -> bool:
    llm = user_llm.answerer_for(db, user.id, max_calls=1)
    try:
        return bool(llm.names)
    finally:
        llm.close()


def _page(request: Request, db: Session, user: User, state, *, status: int = 200, **extra):
    has_model = _page_has_model(db, user)
    return templates.TemplateResponse(
        request,
        "welcome.html",
        {
            "state": state,
            "has_model": has_model,
            "providers": [p for p in user_llm.PROVIDERS.values()],
            "sections": SECTIONS,
            "kinds": onboarding.FIELDS,
            "max_questions": onboarding.MAX_QUESTIONS,
            "asked": onboarding.questions_asked(state),
            **extra,
        },
        status_code=status,
    )


def _state(db: Session, user: User):
    return onboarding.begin(db, user)


async def _upload(file: UploadFile | None) -> tuple[str, bytes] | None:
    if file is None or not file.filename:
        return None
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        return None
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{file.filename} is over 10 MB.")
    return file.filename, content


def _run_turn(db: Session, user: User, state, message: str | None) -> str | None:
    """One chat turn on the user's own model; the error to show, if any."""
    llm = user_llm.answerer_for(db, user.id, max_calls=2)
    try:
        if not llm.names:
            return "Connect a model first, or skip to the form."
        onboarding.chat_turn(db, user, state, llm, message)
        return None
    except onboarding.ChatUnavailable as exc:
        return str(exc)
    finally:
        llm.close()


@router.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    state = _state(db, user)
    db.commit()
    if state.finished_at is not None:
        return RedirectResponse("/", status_code=303)
    return _page(request, db, user, state, error=request.query_params.get("error"))


@router.post("/welcome/documents", response_class=HTMLResponse)
async def welcome_documents(
    request: Request,
    resume: UploadFile | None = None,
    transcript: UploadFile | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    state = _state(db, user)
    try:
        resume_file, transcript_file = await _upload(resume), await _upload(transcript)
    except ValueError as exc:
        return _page(request, db, user, state, status=400, error=str(exc))
    if resume_file is None:
        return _page(request, db, user, state, status=400, error="Choose your resume (PDF or Word).")
    state = onboarding.save_documents(db, user, resume=resume_file, transcript=transcript_file)
    db.commit()
    if not _page_has_model(db, user):
        state.step = "review"
        db.commit()
        return RedirectResponse("/welcome", status_code=303)
    error = _run_turn(db, user, state, None) if not state.messages else None
    db.commit()
    return _page(request, db, user, state, error=error) if error else RedirectResponse("/welcome", status_code=303)


@router.post("/welcome/chat", response_class=HTMLResponse)
def welcome_chat(
    request: Request,
    message: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    state = _state(db, user)
    if state.step != "chat":
        return RedirectResponse("/welcome", status_code=303)
    error = _run_turn(db, user, state, message)
    db.commit()
    if error:
        return _page(request, db, user, state, error=error)
    return RedirectResponse("/welcome#latest", status_code=303)


@router.post("/welcome/review")
def welcome_to_review(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Stop the chat (or skip it) and show what is known so far."""
    state = _state(db, user)
    if state.step in ("documents", "chat"):
        state.step = "review"
    db.commit()
    return RedirectResponse("/welcome", status_code=303)


@router.post("/welcome/finish")
async def welcome_finish(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    state = _state(db, user)
    form = await request.form()
    onboarding.finish(db, user, state, onboarding.draft_from_form(form))
    db.commit()
    return RedirectResponse("/?welcome=1", status_code=303)


@router.post("/welcome/skip")
def welcome_skip(db: Session = Depends(get_db), user: User = Depends(current_user)):
    onboarding.skip(db, _state(db, user))
    db.commit()
    return RedirectResponse("/", status_code=303)
