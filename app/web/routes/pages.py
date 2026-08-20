"""Server-rendered dashboard pages."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.logging_setup import get_logger
from app.models import User
from app.models.base import KANBAN_ORDER, JobStatus
from app.services import jobs_query as q
from app.services import user_jobs
from app.services.actions import add_note, get_job, set_status, update_application_fields
from app.services.preferences import load_preferences, load_profile, save_preferences, save_profile
from app.services.resumes import application_assistant, list_resumes, save_resume
from app.web.deps import current_user
from app.web.templating import templates

log = get_logger("pages")
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    filters = q.JobFilters.from_query(dict(request.query_params))
    page = q.search_jobs(db, filters, user)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "counts": q.dashboard_counts(db, user),
            "page": page,
            "view": user_jobs.view_for(db, user, page.jobs),
            "filters": filters,
            "facets": q.facet_values(db),
            "runs": q.recent_runs(db, limit=1),
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
def job_list(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    """HTMX partial: just the results table."""
    filters = q.JobFilters.from_query(dict(request.query_params))
    page = q.search_jobs(db, filters, user)
    return templates.TemplateResponse(
        request,
        "partials/job_list.html",
        {"page": page, "filters": filters, "view": user_jobs.view_for(db, user, page.jobs)},
    )


@router.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    job = get_job(db, job_id)
    if job is None:
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)
    from sqlalchemy import select

    from app.models import Application, JobEvent

    application = db.scalar(select(Application).where(Application.job_id == job.id))
    events = list(
        db.scalars(
            select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.created_at.desc())
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "application": application,
            "events": events,
            "assistant": application_assistant(db, job),
            "statuses": list(JobStatus),
        },
    )


@router.post("/job/{job_id}/status")
def job_status(
    job_id: str,
    request: Request,
    status: str = Form(...),
    redirect_to: str = Form("/"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    job = get_job(db, job_id)
    if job is None:
        return HTMLResponse("Job not found", status_code=404)
    try:
        new_status = JobStatus(status)
    except ValueError:
        return HTMLResponse("Invalid status", status_code=400)

    set_status(db, job, new_status, user)
    db.commit()

    # HTMX inline actions swap just the card's action bar.
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "partials/job_actions.html", {"job": job, "redirect_to": redirect_to}
        )
    return RedirectResponse(redirect_to or "/", status_code=303)


@router.post("/job/{job_id}/note")
def job_note(
    job_id: str,
    body: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    job = get_job(db, job_id)
    if job is None:
        return HTMLResponse("Job not found", status_code=404)
    add_note(db, job, body, user)
    db.commit()
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@router.post("/job/{job_id}/application")
def job_application(
    job_id: str,
    request: Request,
    resume_version: str = Form(""),
    cover_letter_version: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    referral: str = Form(""),
    follow_up_at: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    job = get_job(db, job_id)
    if job is None:
        return HTMLResponse("Job not found", status_code=404)
    update_application_fields(
        db,
        job,
        {
            "resume_version": resume_version,
            "cover_letter_version": cover_letter_version,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "referral": referral,
            "follow_up_at": follow_up_at,
        },
    )
    db.commit()
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@router.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "tracker.html",
        {
            "board": q.kanban_board(db, user),
            "columns": list(KANBAN_ORDER) + [JobStatus.REJECTED],
            "counts": q.dashboard_counts(db, user),
        },
    )


@router.get("/coverage", response_class=HTMLResponse)
def coverage(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from sqlalchemy import select

    from app.models import JobSourceRecord
    from app.pipeline.discovery import discovery_summary
    from app.sources.registry import source_catalog

    runs = q.recent_runs(db, limit=15)
    records = {
        row.name: row for row in db.scalars(select(JobSourceRecord)).all()
    }
    return templates.TemplateResponse(
        request,
        "coverage.html",
        {
            "runs": runs,
            "latest": runs[0] if runs else None,
            "catalog": source_catalog(),
            "records": records,
            "discovery": discovery_summary(db),
            "yields": q.source_yield_stats(db),
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app.services.learning import outcome_insights

    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "summary": q.analytics_summary(db),
            "insights": outcome_insights(db),
            "notifications": q.recent_notifications(db, limit=10),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app.notify.providers import provider_catalog
    from app.sources.registry import source_catalog

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "prefs": load_preferences(db),
            "catalog": source_catalog(),
            "providers": provider_catalog(),
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("/settings")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    """Persist the settings form.

    The form is flat; this rebuilds the nested preference document from it and
    validates through Pydantic before saving, so an invalid edit cannot corrupt
    the stored configuration.
    """
    form = await request.form()
    prefs = load_preferences(db)
    data = prefs.model_dump()

    def lines(field: str) -> list[str]:
        return [ln.strip() for ln in str(form.get(field, "")).splitlines() if ln.strip()]

    def num(field: str, default: float) -> float:
        try:
            return float(str(form.get(field, default)))
        except (TypeError, ValueError):
            return default

    def flag(field: str) -> bool:
        return str(form.get(field, "")).lower() in ("1", "true", "on", "yes")

    # Roles: one per line, optional "name | weight".
    roles = []
    for index, raw in enumerate(lines("roles")):
        parts = [p.strip() for p in raw.split("|")]
        weight = 1.0
        if len(parts) > 1:
            try:
                weight = float(parts[1])
            except ValueError:
                weight = 1.0
        roles.append({"name": parts[0], "weight": weight, "enabled": True, "order": index,
                      "extra_queries": []})
    if roles:
        data["roles"] = roles

    data["keywords"]["positive"] = lines("keywords_positive")
    data["keywords"]["negative"] = lines("keywords_negative")
    data["keywords"]["hard_exclude"] = lines("keywords_exclude")

    # Locations: "pattern | bonus | alias, alias".
    rules = []
    for raw in lines("locations"):
        parts = [p.strip() for p in raw.split("|")]
        bonus = 0.0
        if len(parts) > 1:
            try:
                bonus = float(parts[1])
            except ValueError:
                bonus = 0.0
        aliases = [a.strip() for a in parts[2].split(",")] if len(parts) > 2 else []
        rules.append(
            {"pattern": parts[0], "bonus": bonus, "aliases": aliases,
             "excluded": bonus <= -900}
        )
    if rules:
        data["locations"]["rules"] = rules
    data["locations"]["remote_bonus"] = num("remote_bonus", 7.0)
    data["locations"]["other_us_bonus"] = num("other_us_bonus", 2.0)

    data["companies"]["preferred"] = lines("companies_preferred")
    data["companies"]["blacklisted"] = lines("companies_blacklisted")
    data["companies"]["monitored"] = lines("companies_monitored")
    data["companies"]["preferred_types"] = lines("company_types")

    data["constraints"]["internship_only"] = flag("internship_only")
    data["constraints"]["seasons"] = lines("seasons")
    data["constraints"]["requires_sponsorship"] = flag("requires_sponsorship")
    data["constraints"]["hard_filter_sponsorship"] = flag("hard_filter_sponsorship")
    data["constraints"]["max_experience_years"] = num("max_experience_years", 2.0)
    graduation = form.get("graduation_year")
    if graduation:
        try:
            data["constraints"]["graduation_year"] = int(str(graduation))
        except ValueError:
            pass
    min_comp = form.get("min_compensation_hourly")
    data["constraints"]["min_compensation_hourly"] = (
        num("min_compensation_hourly", 0.0) or None
    ) if min_comp else None

    for name in (
        "role_match", "technical_skills", "candidate_fit", "location",
        "company_preference", "freshness", "internship_constraints",
    ):
        data["weights"][name] = num(f"weight_{name}", data["weights"][name])

    for name in ("apply_now", "strong_match", "worth_considering", "maybe"):
        data["thresholds"][name] = num(f"threshold_{name}", data["thresholds"][name])

    data["notifications"]["enabled"] = flag("notifications_enabled")
    data["notifications"]["provider"] = str(form.get("notification_provider", "telegram"))
    data["notifications"]["min_score"] = num("notification_min_score", 80.0)
    data["notifications"]["max_jobs_per_notification"] = int(
        num("notification_max_jobs", 7)
    )
    data["notifications"]["send_when_empty"] = flag("send_when_empty")
    data["notifications"]["notify_on_updates"] = flag("notify_on_updates")

    data["schedule"]["enabled"] = flag("schedule_enabled")
    data["schedule"]["timezone"] = str(form.get("timezone", "America/New_York"))
    data["schedule"]["morning_enabled"] = flag("morning_enabled")
    data["schedule"]["morning_time"] = str(form.get("morning_time", "08:00"))
    data["schedule"]["afternoon_enabled"] = flag("afternoon_enabled")
    data["schedule"]["afternoon_time"] = str(form.get("afternoon_time", "16:00"))
    data["schedule"]["cadence"] = str(form.get("cadence", "all"))

    data["scope"]["max_ats_boards_per_run"] = int(num("max_ats_boards", 400))
    data["scope"]["max_expanded_queries"] = int(num("max_queries", 60))
    data["scope"]["query_expansion"] = flag("query_expansion")
    data["scope"]["min_score_to_store"] = num("min_score_to_store", 25.0)
    data["scope"]["llm_semantic_matching"] = flag("llm_semantic_matching")
    data["scope"]["llm_dedup_adjudication"] = flag("llm_dedup_adjudication")
    data["scope"]["disabled_sources"] = form.getlist("disabled_sources")

    from app.schemas.preferences import SearchPreferences

    try:
        validated = SearchPreferences.model_validate(data)
    except Exception as exc:
        log.warning("settings.invalid", error=str(exc)[:300])
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "prefs": prefs,
                "error": str(exc)[:600],
                "catalog": __import__("app.sources.registry", fromlist=["x"]).source_catalog(),
                "providers": __import__("app.notify.providers", fromlist=["x"]).provider_catalog(),
            },
            status_code=400,
        )

    save_preferences(db, validated)
    db.commit()

    # Reschedule so time/cadence changes take effect without a restart.
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        from app.scheduler import reschedule

        reschedule(scheduler, validated.schedule)

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "profile": load_profile(db),
            "resumes": list_resumes(db),
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("/profile")
async def profile_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    def lines(field: str) -> list[str]:
        return [ln.strip() for ln in str(form.get(field, "")).splitlines() if ln.strip()]

    def csv(field: str) -> list[str]:
        return [p.strip() for p in str(form.get(field, "")).split(",") if p.strip()]

    profile = load_profile(db)
    data = profile.model_dump()
    for name in ("school", "degree", "major", "minor", "work_authorization",
                 "security_clearance", "research_experience", "summary"):
        value = str(form.get(name, "")).strip()
        data[name] = value or None
    for name, parser in (
        ("technical_skills", csv), ("programming_languages", csv),
        ("hardware_skills", csv), ("software_skills", csv), ("tools", csv),
        ("coursework", csv), ("preferred_industries", csv), ("preferred_locations", csv),
        ("previous_internships", lines), ("projects", lines), ("publications", lines),
    ):
        data[name] = parser(name)

    for name, cast in (("graduation_year", int), ("graduation_month", int), ("gpa", float)):
        raw = str(form.get(name, "")).strip()
        try:
            data[name] = cast(raw) if raw else None
        except ValueError:
            data[name] = None

    data["requires_sponsorship"] = str(form.get("requires_sponsorship", "")).lower() in (
        "1", "true", "on", "yes"
    )
    data["willing_to_relocate"] = str(form.get("willing_to_relocate", "")).lower() in (
        "1", "true", "on", "yes"
    )

    from app.schemas.profile import CandidateProfileData

    save_profile(db, CandidateProfileData.model_validate(data))
    db.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)


@router.post("/resumes")
async def resume_upload(
    request: Request,
    name: str = Form(...),
    kind: str = Form("general"),
    make_default: str = Form(""),
    file: UploadFile | None = None,
    db: Session = Depends(get_db),
):
    content = await file.read() if file is not None and file.filename else None
    save_resume(
        db,
        name=name,
        kind=kind,
        filename=file.filename if file is not None else None,
        content=content,
        make_default=make_default.lower() in ("1", "true", "on"),
    )
    db.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)


@router.post("/run-search")
async def run_search_now(request: Request):
    """Trigger a search from the UI, in the background."""
    from app.pipeline.runner import run_search

    async def _run() -> None:
        try:
            await run_search(trigger="manual_ui", notify=True)
        except Exception:
            log.exception("manual_run.failed")

    asyncio.create_task(_run())
    return RedirectResponse("/coverage?started=1", status_code=303)


@router.post("/jobs/bulk")
async def jobs_bulk(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Apply one decision to every selected job."""
    form = await request.form()
    status = str(form.get("status", ""))
    job_ids = [int(v) for v in form.getlist("job_ids") if str(v).isdigit()]

    changed = user_jobs.bulk_set_status(db, user, job_ids, status)
    db.commit()
    log.info("pages.bulk_status", user_id=user.id, status=status, changed=changed)

    redirect_to = str(form.get("redirect_to") or "/")
    return RedirectResponse(redirect_to, status_code=303)
