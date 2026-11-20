"""Admin: the owner's list of accounts and who may sign up.

Only the deployment's owner (OWNER_EMAIL, else the first account) can open
any of this; everyone else gets a plain 404, so the page doesn't even admit
to existing.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.logging_setup import get_logger
from app.models import AllowedEmail, ApplyTask, User, UserLlmSetting
from app.services import email_verify, user_llm
from app.web.deps import current_user
from app.web.templating import templates

log = get_logger("admin")

router = APIRouter()


def owner_only(db: Session = Depends(get_db), user: User = Depends(current_user)) -> User:
    if not user_llm.is_owner(db, user.id):
        raise HTTPException(404)
    return user


def _back(message: str, bad: bool = False) -> RedirectResponse:
    return RedirectResponse(f"/admin?done={quote(message)}{'&bad=1' if bad else ''}", status_code=303)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page(request: Request, db: Session = Depends(get_db), owner: User = Depends(owner_only)):
    users = list(db.scalars(select(User).order_by(User.id)).all())
    submitted = dict(
        db.execute(
            select(ApplyTask.user_id, func.count(ApplyTask.id))
            .where(ApplyTask.submitted_at.is_not(None))
            .group_by(ApplyTask.user_id)
        ).all()
    )
    with_model = set(db.scalars(select(UserLlmSetting.user_id)).all())
    settings = get_settings()
    done = request.query_params.get("done")
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "users": users,
            "owner_id": owner.id,
            "submitted": submitted,
            "with_model": with_model,
            "allowed": list(db.scalars(select(AllowedEmail).order_by(AllowedEmail.entry)).all()),
            "env_allowed": email_verify.parse_entries(settings.signup_allowed_emails or ""),
            "invite_code_set": bool((settings.signup_invite_code or "").strip()),
            "verify_on": settings.signup_verify_email,
            "smtp_ok": settings.smtp_configured,
            "flash": {"text": done, "bad": request.query_params.get("bad") == "1"} if done else None,
        },
    )


@router.post("/admin/allow", include_in_schema=False)
def allow(entries: str = Form(""), db: Session = Depends(get_db), owner: User = Depends(owner_only)):
    have = set(email_verify.allowed_entries(db))
    added, bad = [], []
    for entry in email_verify.parse_entries(entries):
        domain = entry.rpartition("@")[2]
        if entry.count("@") != 1 or "." not in domain or domain.startswith(".") or domain.endswith("."):
            bad.append(entry)
            continue
        if entry not in have:
            db.add(AllowedEmail(entry=entry))
            have.add(entry)
            added.append(entry)
    db.commit()
    log.info("admin.allowed", count=len(added))
    if bad:
        return _back(f"Not an email or @domain: {', '.join(bad)}", bad=True)
    return _back(f"Allowed {', '.join(added)}" if added else "Already on the list")


@router.post("/admin/allow/{entry_id}/remove", include_in_schema=False)
def disallow(entry_id: int, db: Session = Depends(get_db), owner: User = Depends(owner_only)):
    row = db.get(AllowedEmail, entry_id)
    if row is not None:
        db.delete(row)
        db.commit()
    return _back(f"Removed {row.entry}" if row else "Already removed")


def _other_account(db: Session, owner: User, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    if user.id == owner.id:
        raise HTTPException(400, "You can't do that to your own account here.")
    return user


@router.post("/admin/users/{user_id}/active", include_in_schema=False)
def set_active(
    user_id: int,
    active: str = Form(...),
    db: Session = Depends(get_db),
    owner: User = Depends(owner_only),
):
    user = _other_account(db, owner, user_id)
    user.is_active = active == "1"
    if not user.is_active:
        # The worker reads the queue, not accounts: stop what's waiting.
        from sqlalchemy import update

        from app.models.autoapply import CANCELLED, QUEUED, WAITING_CODE

        db.execute(
            update(ApplyTask)
            .where(ApplyTask.user_id == user.id, ApplyTask.status.in_((QUEUED, WAITING_CODE)))
            .values(status=CANCELLED, detail="Account turned off by the owner")
        )
    db.commit()
    log.info("admin.account_active", user_id=user.id, active=user.is_active)
    return _back(f"{'Turned on' if user.is_active else 'Turned off'} {user.email}")


@router.post("/admin/users/{user_id}/delete", include_in_schema=False)
def delete_account(
    user_id: int,
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    owner: User = Depends(owner_only),
):
    user = _other_account(db, owner, user_id)
    if confirm.strip().lower() != (user.email or "").lower():
        return _back("Type the account's email to confirm deleting it.", bad=True)
    email = user.email
    db.delete(user)
    db.commit()
    log.info("admin.account_deleted", user_id=user_id)
    return _back(f"Deleted {email} and everything it had")
