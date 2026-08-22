"""Server-rendered dashboard pages."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.logging_setup import get_logger
from app.models import User
from app.models.base import JobStatus
from app.services import jobs_query as q
from app.services import user_jobs
from app.services.actions import add_note, get_job, set_status, update_application_fields
from app.services.preferences import load_preferences, load_profile, save_preferences, save_profile
from app.services.resumes import application_assistant, list_resumes, save_resume
from app.web.deps import current_user
from app.web.templating import templates

log = get_logger("pages")
router = APIRouter()


#: Everything the four job pages need in common. `active_view` names the page;
#: `view` is the per-user data for the jobs on it. They are deliberately
#: different words -- conflating them is how a card ends up showing someone
#: else's status.
def _job_page_context(request: Request, db: Session, user: User, view_name: str) -> dict:
    params = dict(request.query_params)
    params["view"] = view_name
    filters = q.JobFilters.from_query(params)
    page = q.search_jobs(db, filters, user)
    return {
        "active_view": view_name,
        "counts": q.dashboard_counts(db, user),
        "page": page,
        "view": user_jobs.view_for(db, user, page.jobs),
        "filters": filters,
        "facets": q.facet_values(db, user=user),
        "runs": q.recent_runs(db, limit=1),
        "next_search_at": _next_search_at(request),
        "flash": _flash(request),
    }


def _next_search_at(request: Request):
    from app.scheduler import next_digest_at

    return next_digest_at(getattr(request.app.state, "scheduler", None))


def _with_flash(path: str, message: str, *, bad: bool = False) -> str:
    """Attach a confirmation to a redirect target, preserving its query string."""
    joiner = "&" if "?" in path else "?"
    suffix = f"{joiner}done={quote(message)}" + ("&bad=1" if bad else "")
    return f"{path}{suffix}"


def _visible_total(db: Session, user: User, view: str, back: str) -> int | None:
    """How many jobs the list the user is looking at now holds.

    The filters have to come from the page they acted on -- counting an
    unfiltered list would replace one wrong number with another. They travel in
    the redirect target, which already carries the exact query string.
    """
    from urllib.parse import parse_qsl, urlparse

    if view not in q.VIEWS:
        return None
    params = dict(parse_qsl(urlparse(back or "").query))
    params["view"] = view
    return q.count_jobs(db, q.JobFilters.from_query(params), user)


def _flash(request: Request) -> dict | None:
    """A one-line confirmation carried across a redirect."""
    text = request.query_params.get("done")
    if not text:
        return None
    return {"text": text, "bad": request.query_params.get("bad") == "1"}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    """The feed: everything still awaiting a decision."""
    return templates.TemplateResponse(
        request, "dashboard.html", _job_page_context(request, db, user, "review")
    )


@router.get("/saved", response_class=HTMLResponse)
def saved_jobs(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", _job_page_context(request, db, user, "saved")
    )


@router.get("/applied", response_class=HTMLResponse)
def applied_jobs(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", _job_page_context(request, db, user, "applied")
    )


@router.get("/dismissed", response_class=HTMLResponse)
def dismissed_jobs(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", _job_page_context(request, db, user, "dismissed")
    )


@router.get("/jobs", response_class=HTMLResponse)
def job_list(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    """HTMX partial: just the results list."""
    filters = q.JobFilters.from_query(dict(request.query_params))
    page = q.search_jobs(db, filters, user)
    return templates.TemplateResponse(
        request,
        "partials/job_list.html",
        {
            "page": page,
            "filters": filters,
            "view": user_jobs.view_for(db, user, page.jobs),
            "active_view": filters.view,
        },
    )


@router.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> HTMLResponse:
    job = get_job(db, job_id)
    if job is None:
        return templates.TemplateResponse(
            request, "not_found.html", {}, status_code=404
        )
    from sqlalchemy import select

    from app.models import Application, JobEvent

    # Scoped to the signed-in account: an application is one person's record,
    # and showing somebody else's would be both wrong and a disclosure.
    application = db.scalar(
        select(Application).where(
            Application.job_id == job.id, Application.user_id == user.id
        )
    )
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
            "assistant": application_assistant(db, job, user),
            "statuses": list(JobStatus),
            "view": user_jobs.view_for(db, user, [job]),
            "active_view": "review",
            "counts": q.dashboard_counts(db, user),
            "flash": _flash(request),
        },
    )


#: What each decision is called back to the user, in the past tense. Undo is
#: offered for every one of them, which is why none asks for confirmation first.
#: The tracker is about what happens *after* you apply. New and Saved have
#: their own pages, and repeating them here only made the board too wide to read
#: and left two places showing the same job.
PIPELINE_COLUMNS = (
    JobStatus.APPLIED,
    JobStatus.ASSESSMENT,
    JobStatus.INTERVIEW,
    JobStatus.OFFER,
    JobStatus.REJECTED,
)


OUTCOME: dict[str, str] = {
    JobStatus.SAVED.value: "Saved",
    JobStatus.APPLIED.value: "Marked as applied",
    JobStatus.DISMISSED.value: "Dismissed",
    JobStatus.NEW.value: "Moved back to review",
    # "Updated" tells the user nothing about which stage they just moved to,
    # which on a five-column board is the only thing they wanted confirmed.
    JobStatus.ASSESSMENT.value: "Moved to Assessment",
    JobStatus.INTERVIEW.value: "Moved to Interview",
    JobStatus.OFFER.value: "Moved to Offer",
    JobStatus.REJECTED.value: "Marked as rejected",
    JobStatus.EXPIRED.value: "Marked as expired",
}


@router.post("/job/{job_id}/status")
def job_status(
    job_id: str,
    request: Request,
    status: str = Form(...),
    redirect_to: str = Form("/"),
    view: str = Form("review"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Record a decision, and say plainly what happened.

    Every response answers three questions at once: what the job's state is now,
    whether it still belongs on this page, and how to take it back.
    """
    job = get_job(db, job_id)
    if job is None:
        return HTMLResponse("That job no longer exists.", status_code=404)
    try:
        new_status = JobStatus(status)
    except ValueError:
        return HTMLResponse("Invalid status", status_code=400)

    previous = user_jobs.status_of(user_jobs.get_state(db, user, job))
    set_status(db, job, new_status, user)
    db.commit()

    message = OUTCOME.get(status, "Updated")

    if request.headers.get("HX-Request") and view == "tracker":
        # The board is re-rendered whole rather than the card being spliced
        # from one column into another. It costs one extra query and removes an
        # entire class of bug -- a card in the right column over a stale count,
        # or a count that drifts after a few moves.
        return templates.TemplateResponse(
            request,
            "partials/tracker_result.html",
            {
                "board": q.kanban_board(db, user),
                "columns": list(PIPELINE_COLUMNS),
                "counts": q.dashboard_counts(db, user),
                "toast": {
                    "text": message,
                    "job_id": job.id,
                    "undo_status": previous,
                    "redirect_to": "/tracker",
                    "view": "tracker",
                },
            },
        )

    if request.headers.get("HX-Request"):
        # A job that has left this page is removed from it rather than left
        # sitting there greyed out, which is what made "did that work?" a real
        # question before.
        stays = status in q.VIEWS.get(view, frozenset({status}))
        return templates.TemplateResponse(
            request,
            "partials/action_result.html",
            {
                "job": job,
                "keep_card": stays,
                "view": user_jobs.view_for(db, user, [job]),
                "active_view": view,
                # Recomputed after the change, so the headline and the four
                # counters describe the list the user is actually looking at.
                "counts": q.dashboard_counts(db, user),
                "total": _visible_total(db, user, view, redirect_to),
                "filters": q.JobFilters.from_query({"view": view}),
                "toast": {
                    "text": message,
                    "job_id": job.id,
                    # Undo puts the job back where it actually was, rather than
                    # assuming everything came from the review feed.
                    "undo_status": previous,
                    "redirect_to": redirect_to,
                    "view": view,
                },
            },
        )

    return RedirectResponse(_with_flash(redirect_to or "/", message), status_code=303)


@router.post("/job/{job_id}/opened")
def job_opened(
    job_id: str,
    request: Request,
    view: str = Form("review"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """The user opened the application page. That is all this records.

    Opening a posting and submitting an application are different events, and
    treating a click as an application would fill the Applied list with jobs the
    user only glanced at. So the status is left alone and the card asks.
    """
    job = get_job(db, job_id)
    if job is None:
        return HTMLResponse("That job no longer exists.", status_code=404)
    user_jobs.mark_opened(db, user, job)
    db.commit()
    return templates.TemplateResponse(
        request,
        "partials/job_actions.html",
        {
            "job": job,
            "current_status": user_jobs.status_of(user_jobs.get_state(db, user, job)),
            "asking": True,
            "active_view": view,
        },
    )


@router.post("/job/{job_id}/not-applied")
def job_not_applied(
    job_id: str,
    request: Request,
    view: str = Form("review"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Not yet: clear the pending question and leave the status untouched."""
    job = get_job(db, job_id)
    if job is None:
        return HTMLResponse("That job no longer exists.", status_code=404)
    state = user_jobs.get_or_create_state(db, user, job)
    state.opened_at = None
    db.commit()
    return templates.TemplateResponse(
        request,
        "partials/job_actions.html",
        {
            "job": job,
            "current_status": user_jobs.status_of(state),
            "active_view": view,
        },
    )


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
        user,
    )
    db.commit()
    return RedirectResponse(
        _with_flash(f"/job/{job_id}", "Application details saved"), status_code=303
    )


@router.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "tracker.html",
        {
            "board": q.kanban_board(db, user),
            "columns": list(PIPELINE_COLUMNS),
            "counts": q.dashboard_counts(db, user),
            "flash": _flash(request),
        },
    )


@router.get("/coverage", response_class=HTMLResponse)
def coverage(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> HTMLResponse:
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
            "preferred_coverage": q.preferred_company_coverage(
                db, load_preferences(db, user=user).companies.preferred
            ),
            "counts": q.dashboard_counts(db, user),
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
def analytics(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> HTMLResponse:
    from app.services.learning import outcome_insights

    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "summary": q.analytics_summary(db),
            "insights": outcome_insights(db),
            "notifications": q.recent_notifications(db, limit=10),
            "counts": q.dashboard_counts(db, user),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "settings.html", _settings_context(request, db, user)
    )


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> HTMLResponse:
    """Emails and scheduling, split out of Settings.

    Same preference document, same POST handler -- only the sections the form
    declares are written, so the two pages cannot overwrite each other.
    """
    return templates.TemplateResponse(
        request, "notifications.html", _settings_context(request, db, user)
    )


def _settings_context(request: Request, db: Session, user: User) -> dict:
    from app.notify.providers import provider_catalog
    from app.services import notify_config
    from app.sources.registry import source_catalog

    channel = notify_config.load(db)
    prefs = load_preferences(db, user=user)
    return {
        "prefs": prefs,
        "catalog": source_catalog(),
        "providers": provider_catalog(config=channel),
        "channel": channel,
        "channel_ready": channel.ready_for(prefs.notifications.provider),
        "channel_missing": channel.missing_for(prefs.notifications.provider),
        "digest_email": user.notification_email,
        "counts": q.dashboard_counts(db, user),
        "saved": request.query_params.get("saved") == "1",
        "rescored": request.query_params.get("rescored"),
        # `error` means the settings did not save. A delivery test that fails
        # is a `notice`: the settings saved fine, the send did not.
        "error": request.query_params.get("error"),
        "notice": request.query_params.get("notice"),
        "notice_bad": request.query_params.get("notice_bad") == "1",
    }


@router.post("/settings")
async def settings_save(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Persist the settings form.

    The form is flat; this rebuilds the nested preference document from it and
    validates through Pydantic before saving, so an invalid edit cannot corrupt
    the stored configuration.
    """
    form = await request.form()
    prefs = load_preferences(db, user=user)
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

    # Settings are split across more than one page now, and an unchecked
    # checkbox is indistinguishable from a checkbox that was never on the page.
    # Each form declares which sections it carries, so submitting the search
    # page cannot silently switch every notification toggle off.
    sections = set(form.getlist("_section")) or {
        "search", "location", "constraints", "ranking", "scope",
        "notifications", "schedule",
    }

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
    if "location" in sections:
        # "Anywhere" is the empty list, which is also the schema default, so the
        # absence of a restriction is represented by an absence rather than a
        # sentinel country.
        data["locations"]["allowed_countries"] = [
            c for c in form.getlist("allowed_countries") if str(c).strip()
        ]

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

    if "notifications" in sections:
        data["notifications"]["enabled"] = flag("notifications_enabled")
        data["notifications"]["provider"] = str(form.get("notification_provider", "telegram"))
        data["notifications"]["min_score"] = num("notification_min_score", 80.0)
        data["notifications"]["max_jobs_per_notification"] = int(
            num("notification_max_jobs", 7)
        )
        data["notifications"]["send_when_empty"] = flag("send_when_empty")
        data["notifications"]["notify_on_updates"] = flag("notify_on_updates")

    if "schedule" in sections:
        data["schedule"]["enabled"] = flag("schedule_enabled")
        data["schedule"]["timezone"] = str(form.get("timezone", "America/New_York"))
        data["schedule"]["morning_enabled"] = flag("morning_enabled")
        data["schedule"]["morning_time"] = str(form.get("morning_time", "08:00"))
        data["schedule"]["afternoon_enabled"] = flag("afternoon_enabled")
        data["schedule"]["afternoon_time"] = str(form.get("afternoon_time", "16:00"))
        data["schedule"]["cadence"] = str(form.get("cadence", "all"))

    if "scope" in sections:
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

    # Channel credentials travel with the same form, so choosing a provider
    # and setting it up are one action rather than two screens apart.
    from app.services import notify_config

    notify_config.save(
        db,
        {
            "smtp_host": form.get("smtp_host"),
            "smtp_port": form.get("smtp_port"),
            "smtp_user": form.get("smtp_user"),
            "smtp_password": form.get("smtp_password"),
            "email_from": form.get("email_from"),
            "telegram_bot_token": form.get("telegram_bot_token"),
            "telegram_chat_id": form.get("telegram_chat_id"),
        },
    )

    # Refuse to *enable* a channel that cannot actually deliver. Turning it on
    # anyway is how you end up believing digests are being sent when every one
    # of them is quietly falling back to a file.
    #
    # Refusing the channel must not refuse the rest of the page. This used to
    # roll the whole transaction back, so an unconfigured notification channel
    # silently discarded every other change on the form -- roles, weights,
    # locations, thresholds. On an instance with no bot token that meant no
    # settings change ever persisted, and the only clue was an error naming
    # the channel: deleting a target role appeared to do nothing at all,
    # because the delete really was being thrown away.
    channel = notify_config.load(db)
    chosen = validated.notifications.provider
    channel_warning: str | None = None
    if validated.notifications.enabled and not channel.ready_for(chosen):
        missing = ", ".join(channel.missing_for(chosen))
        channel_warning = (
            f"Everything else was saved, but digests stay off: {chosen} still "
            f"needs {missing}."
        )
        # Keep the user's chosen provider so their setup is not undone; just
        # do not claim to be sending anything through it.
        validated.notifications.enabled = False

    if form.get("digest_email") is not None:
        user.digest_email = str(form.get("digest_email")).strip() or None

    # The candidate profile carries its own copy of this answer, and the ranking
    # engine ORs the two together (`pipeline/match.py`). Settings owns the only
    # control now, so the profile copy is kept in step: without this, unticking
    # the box here would leave a stale `true` behind that no page can reach and
    # the search would go on treating sponsorship as required.
    profile = load_profile(db, user=user)
    if bool(profile.requires_sponsorship) != validated.constraints.requires_sponsorship:
        from app.schemas.profile import CandidateProfileData

        profile_data = profile.model_dump()
        profile_data["requires_sponsorship"] = validated.constraints.requires_sponsorship
        save_profile(db, CandidateProfileData.model_validate(profile_data), user=user)

    save_preferences(db, validated, user=user)

    # Preferences that changed the ranking must reach the jobs already stored,
    # or the setting looks ignored: removing a target role would leave every
    # posting for it sitting at the top of the feed with its old score.
    rescored = user_jobs.rescore_all_for_user(db, user, validated)
    db.commit()

    # Reschedule so time/cadence changes take effect without a restart.
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        from app.scheduler import reschedule

        reschedule(scheduler, validated.schedule)

    back = str(form.get("_return") or "/settings")
    if not back.startswith("/") or back.startswith("//"):
        back = "/settings"          # never redirect off-site on form input
    target = f"{back}?saved=1&rescored={rescored}"
    if channel_warning:
        target += f"&notice={quote(channel_warning)}&notice_bad=1"
    return RedirectResponse(target, status_code=303)


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "profile": load_profile(db, user=user),
            "resumes": list_resumes(db, user),
            "saved": request.query_params.get("saved") == "1",
            "rescored": request.query_params.get("rescored"),
            "counts": q.dashboard_counts(db, user),
        },
    )


@router.post("/profile")
async def profile_save(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    form = await request.form()

    def lines(field: str) -> list[str]:
        return [ln.strip() for ln in str(form.get(field, "")).splitlines() if ln.strip()]

    def csv(field: str) -> list[str]:
        return [p.strip() for p in str(form.get(field, "")).split(",") if p.strip()]

    profile = load_profile(db, user=user)
    data = profile.model_dump()
    for name in ("school", "degree", "major", "minor", "work_authorization",
                 "security_clearance", "research_experience", "summary"):
        value = str(form.get(name, "")).strip()
        data[name] = value or None
    for name, parser in (
        ("technical_skills", csv), ("programming_languages", csv),
        ("hardware_skills", csv), ("software_skills", csv), ("tools", csv),
        ("coursework", csv), ("preferred_industries", csv),
        ("previous_internships", lines), ("projects", lines), ("publications", lines),
    ):
        data[name] = parser(name)

    for name, cast in (("graduation_year", int), ("graduation_month", int), ("gpa", float)):
        raw = str(form.get(name, "")).strip()
        try:
            data[name] = cast(raw) if raw else None
        except ValueError:
            data[name] = None

    # `requires_sponsorship` and `preferred_locations` are deliberately absent:
    # Settings owns both, and this form no longer offers them. Rewriting them
    # from a form that does not carry them would clear a real answer -- an
    # unchecked box and an absent box look identical in a POST body -- every
    # time somebody saved an unrelated field on this page.
    data["willing_to_relocate"] = str(form.get("willing_to_relocate", "")).lower() in (
        "1", "true", "on", "yes"
    )

    from app.schemas.profile import CandidateProfileData

    updated = CandidateProfileData.model_validate(data)
    save_profile(db, updated, user=user)
    # Your skills are half of the relevance score, so editing them has to reach
    # the stored jobs for the same reason a preference change does.
    rescored = user_jobs.rescore_all_for_user(db, user, None, updated)
    db.commit()
    return RedirectResponse(f"/profile?saved=1&rescored={rescored}", status_code=303)


@router.post("/resumes")
async def resume_upload(
    request: Request,
    name: str = Form(...),
    kind: str = Form("general"),
    make_default: str = Form(""),
    file: UploadFile | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    content = await file.read() if file is not None and file.filename else None
    save_resume(
        db,
        name=name,
        kind=kind,
        filename=file.filename if file is not None else None,
        content=content,
        make_default=make_default.lower() in ("1", "true", "on"),
        user=user,
    )
    db.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)


@router.post("/run-search")
async def run_search_now(request: Request):
    """Trigger a search from the UI, in the background.

    The crawl takes minutes, so the user is returned to the page they were on
    with an explanation, rather than being parked on a progress screen. The
    coverage page is offered for anyone who does want to watch it.
    """
    from app.pipeline.runner import run_search

    async def _run() -> None:
        try:
            await run_search(trigger="manual_ui", notify=True)
        except Exception:
            log.exception("manual_run.failed")

    asyncio.create_task(_run())

    back = request.headers.get("referer") or "/"
    if not back.startswith("/"):
        from urllib.parse import urlparse

        parsed = urlparse(back)
        back = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return RedirectResponse(
        _with_flash(
            back or "/",
            "Searching the job boards now. New matches appear here as they are found.",
        ),
        status_code=303,
    )


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
    redirect_to = str(form.get("redirect_to") or "/")

    if not job_ids:
        return RedirectResponse(
            _with_flash(redirect_to, "Nothing was selected.", bad=True), status_code=303
        )

    changed = user_jobs.bulk_set_status(db, user, job_ids, status)
    db.commit()
    log.info("pages.bulk_status", user_id=user.id, status=status, changed=changed)

    if changed:
        verb = OUTCOME.get(status, "Updated")
        message = f"{verb}: {changed} job{'' if changed == 1 else 's'}"
    else:
        # Every selected job was already in that state. Saying so beats a
        # silent reload that looks like the button did nothing.
        message = "Those jobs were already in that state."
    return RedirectResponse(_with_flash(redirect_to, message), status_code=303)


@router.post("/settings/test-channel")
async def settings_test_channel(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Save the channel credentials, then actually send through them.

    "Configured" only means the fields are non-empty; a wrong password looks
    identical until something tries to deliver. So the button proves it.
    """
    from app.notify.engine import send_test_notification
    from app.services import notify_config

    form = await request.form()
    notify_config.save(
        db,
        {
            "smtp_host": form.get("smtp_host"),
            "smtp_port": form.get("smtp_port"),
            "smtp_user": form.get("smtp_user"),
            "smtp_password": form.get("smtp_password"),
            "email_from": form.get("email_from"),
            "telegram_bot_token": form.get("telegram_bot_token"),
            "telegram_chat_id": form.get("telegram_chat_id"),
        },
    )
    if form.get("digest_email") is not None:
        user.digest_email = str(form.get("digest_email")).strip() or None
    db.flush()

    provider = str(form.get("notification_provider") or "email")
    channel = notify_config.load(db)
    if not channel.ready_for(provider):
        db.commit()
        missing = ", ".join(channel.missing_for(provider))
        return RedirectResponse(
            f"/settings?notice_bad=1&notice={quote(f'Saved. {provider} still needs: {missing}')}",
            status_code=303,
        )

    result = await send_test_notification(
        db, provider, recipient=user.notification_email
    )
    db.commit()

    if result.ok and result.provider == provider:
        target = user.notification_email if provider == "email" else provider
        return RedirectResponse(
            f"/settings?notice={quote(f'Settings saved. Test sent via {provider} to {target}.')}",
            status_code=303,
        )

    detail = result.error or f"it fell back to {result.provider}"
    return RedirectResponse(
        f"/settings?notice_bad=1&notice={quote(f'Settings saved, but the test did not send: {detail}')}",
        status_code=303,
    )
