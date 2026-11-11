"""Swipe to apply: the deck, the decisions, and the auto-apply status page.

Kept deliberately cheap per request -- every call here wakes and reads the
hosted database. No page-wide counters, no blobs, and waking the worker
happens after the response is sent.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.apply import cloud
from app.db import get_db
from app.models import ApplyTask, User
from app.models.base import JobStatus
from app.services import user_jobs
from app.web.deps import current_user
from app.web.templating import templates

router = APIRouter()

#: Resumes are a page or two; anything near this is not a resume.
MAX_RESUME_BYTES = 5 * 1024 * 1024


def _wake_worker() -> None:
    """Runs after the response: a slow GitHub API never delays a swipe."""
    from app.config import get_settings

    if not get_settings().gh_dispatch_token:
        return
    from app.db import session_scope

    try:
        with session_scope() as session:
            cloud.dispatch_worker(session)
    except Exception:  # the worker's own schedule is the fallback
        pass


def _summary(db: Session, user: User) -> dict:
    counts = cloud.queue_counts(db, user)
    return {
        "open": counts.get(cloud.QUEUED, 0) + counts.get(cloud.RUNNING, 0),
        "applied": counts.get(cloud.SUBMITTED, 0),
        "needs_you": counts.get(cloud.NEEDS_YOU, 0),
    }


@router.get("/swipe", response_class=HTMLResponse)
def swipe_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request,
        "swipe.html",
        {
            "cards": cloud.deck(db, user, limit=10),
            "ready": not cloud.readiness(cloud.get_record(db, user)),
            "summary": _summary(db, user),
        },
    )


@router.get("/swipe/more")
def swipe_more(
    exclude: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)
) -> JSONResponse:
    skip = {int(x) for x in exclude.split(",")[:500] if x.strip().isdigit()}
    return JSONResponse({"cards": cloud.deck(db, user, limit=10, exclude=skip)})


@router.post("/swipe/{job_id}")
def swipe_decide(
    job_id: int,
    background: BackgroundTasks,
    decision: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> JSONResponse:
    job = cloud.lean_job(db, job_id)
    if job is None:
        raise HTTPException(404, "no such job")

    if decision == "yes":
        if cloud.enqueue(db, user, job) is None:
            db.rollback()
            return JSONResponse({"ok": False, "message": "No form the agent can fill for this one"})
        db.commit()
        background.add_task(_wake_worker)
    elif decision == "no":
        user_jobs.set_status(db, user, job, JobStatus.DISMISSED.value)
        db.commit()
    elif decision == "applied":
        cloud.mark_applied_elsewhere(db, user, job)
        db.commit()
    elif decision == "undo":
        status = user_jobs.status_of(user_jobs.get_state(db, user, job))
        if status == JobStatus.SAVED.value and not cloud.cancel(db, user, job):
            db.rollback()
            return JSONResponse({"ok": False, "message": "Too late — the agent already started it"})
        if status in (JobStatus.SAVED.value, JobStatus.DISMISSED.value, JobStatus.APPLIED.value):
            from app.services.actions import set_status

            # Through the tracker, so an "already applied" card leaves it too.
            set_status(db, job, JobStatus.NEW, user)
        db.commit()
    else:
        raise HTTPException(400, "decision must be yes, no, applied or undo")
    return JSONResponse({"ok": True})


@router.get("/autoapply", response_class=HTMLResponse)
def autoapply_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    record = cloud.get_record(db, user)
    flash = request.query_params.get("done")
    return templates.TemplateResponse(
        request,
        "autoapply.html",
        {
            "tasks": cloud.tasks_for(db, user),
            "labels": cloud.STATUS_LABELS,
            "record": record,
            "problems": cloud.readiness(record),
            "profile_json": cloud.profile_json(record),
            "flash": {"text": flash, "bad": request.query_params.get("bad") == "1"} if flash else None,
        },
    )


@router.post("/autoapply/profile")
async def autoapply_profile(
    profile_json: str = Form(""),
    resume: UploadFile | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        data = cloud.clean_profile_data(json.loads(profile_json or "{}"))
    except (ValueError, TypeError) as exc:
        return RedirectResponse(f"/autoapply?bad=1&done={quote('Not saved: ' + str(exc)[:120])}", 303)
    upload = None
    if resume is not None and resume.filename:
        content = await resume.read()
        if len(content) > MAX_RESUME_BYTES:
            return RedirectResponse(f"/autoapply?bad=1&done={quote('That resume is over 5 MB')}", 303)
        if content:
            upload = (resume.filename, content)
    cloud.save_record(db, user, data=data, resume=upload)
    db.commit()
    return RedirectResponse(f"/autoapply?done={quote('Profile saved')}", 303)


@router.post("/autoapply/task/{task_id}/retry")
def autoapply_retry(
    task_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    task = _own_task(db, user, task_id)
    cloud.enqueue(db, user, cloud.lean_job(db, task.job_id))
    db.commit()
    background.add_task(_wake_worker)
    return RedirectResponse(f"/autoapply?done={quote('Queued again')}", 303)


@router.post("/autoapply/task/{task_id}/answers")
async def autoapply_answers(
    task_id: int,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """The user answered what the agent could not; remember it and retry."""
    task = _own_task(db, user, task_id)
    form = await request.form()
    replies = {}
    for key, label in form.multi_items():
        if key.startswith("label_") and isinstance(label, str):
            answer = form.get("answer_" + key[len("label_"):])
            if isinstance(answer, str) and answer.strip():
                replies[label] = answer
    if not replies:
        return RedirectResponse(f"/autoapply?bad=1&done={quote('Type an answer first')}", 303)
    try:
        cloud.remember_answers(db, user, replies)
    except (TypeError, ValueError) as exc:
        return RedirectResponse(f"/autoapply?bad=1&done={quote('Not saved: ' + str(exc)[:120])}", 303)
    cloud.enqueue(db, user, cloud.lean_job(db, task.job_id))
    db.commit()
    background.add_task(_wake_worker)
    return RedirectResponse(f"/autoapply?done={quote('Saved. Applying again')}", 303)


@router.post("/autoapply/task/{task_id}/done")
def autoapply_done(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The user finished a handed-back application by hand."""
    from app.services.actions import set_status

    task = _own_task(db, user, task_id)
    set_status(db, cloud.lean_job(db, task.job_id), JobStatus.APPLIED, user)
    task.status, task.detail = cloud.SUBMITTED, "You submitted this one yourself."
    db.commit()
    return RedirectResponse(f"/autoapply?done={quote('Marked as applied')}", 303)


@router.get("/autoapply/task/{task_id}/screenshot")
def autoapply_screenshot(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _own_task(db, user, task_id)
    if not task.screenshot:
        raise HTTPException(404, "no screenshot")
    # Cached by the browser so reopening one never re-reads it from Neon.
    return Response(task.screenshot, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _own_task(db: Session, user: User, task_id: int) -> ApplyTask:
    task = db.get(ApplyTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "no such task")
    return task
