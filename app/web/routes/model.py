"""Your model: the provider and API key a user brings for everything the app
asks a model on their behalf (app/services/user_llm.py)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services import secretbox, user_llm
from app.web.deps import current_user
from app.web.templating import templates

router = APIRouter()


def _page(request: Request, db: Session, user: User, *, status: int = 200, **extra):
    return templates.TemplateResponse(
        request,
        "model.html",
        {
            "providers": list(user_llm.PROVIDERS.values()),
            "saved": user_llm.get(db, user.id),
            "owner": user_llm.is_owner(db, user.id),
            "can_store": secretbox.available(),
            **extra,
        },
        status_code=status,
    )


@router.get("/model", response_class=HTMLResponse)
def model_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _page(request, db, user, notice=request.query_params.get("notice"))


@router.post("/model", response_class=HTMLResponse)
def model_save(
    request: Request,
    provider: str = Form(...),
    api_key: str = Form(""),
    model: str = Form(""),
    base_url: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        row = user_llm.save(
            db, user, provider=provider, api_key=api_key, model=model, base_url=base_url
        )
    except user_llm.SetupError as exc:
        db.rollback()
        return _page(request, db, user, status=400, error=str(exc), picked=provider)
    ok, _ = user_llm.check(db, row)
    db.commit()
    return RedirectResponse("/model?notice=" + ("works" if ok else "failed"), status_code=303)


@router.post("/model/test")
def model_test(db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = user_llm.get(db, user.id)
    if row is None:
        return RedirectResponse("/model", status_code=303)
    ok, _ = user_llm.check(db, row)
    db.commit()
    return RedirectResponse("/model?notice=" + ("works" if ok else "failed"), status_code=303)


@router.post("/model/delete")
def model_delete(db: Session = Depends(get_db), user: User = Depends(current_user)):
    user_llm.forget(db, user.id)
    db.commit()
    return RedirectResponse("/model?notice=removed", status_code=303)
