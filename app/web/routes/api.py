"""JSON API.

The dashboard is server-rendered, but every capability is also exposed here so
a different frontend (or a script, or a mobile client) can drive the system
without touching the templates.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Job, JobSourceRecord, User
from app.models.base import JobStatus
from app.schemas.preferences import SearchPreferences
from app.schemas.profile import CandidateProfileData
from app.services import jobs_query as q
from app.services.actions import add_note, get_job, set_status
from app.services.learning import analytics_payload
from app.services.preferences import load_preferences, load_profile, save_preferences, save_profile
from app.web.deps import current_user

router = APIRouter(tags=["api"])


def _serialize_job(job: Job, *, detail: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": job.id,
        "canonical_job_id": job.canonical_job_id,
        "company": job.company_name,
        "title": job.title,
        "location": job.location_raw,
        "remote_status": job.remote_status,
        "employment_type": job.employment_type,
        "score": job.relevance_score,
        "priority": job.priority,
        "status": job.status,
        "freshness": job.freshness,
        "application_url": job.application_url,
        "date_posted": job.date_posted.isoformat() if job.date_posted else None,
        "date_discovered": job.date_discovered.isoformat() if job.date_discovered else None,
        "deadline": job.deadline.isoformat() if job.deadline else None,
        "deadline_is_explicit": job.deadline_is_explicit,
        "sponsorship": job.sponsorship,
        "salary": {
            "min": job.salary_min,
            "max": job.salary_max,
            "currency": job.salary_currency,
            "period": job.salary_period,
        },
        "skills": job.skills,
        "match_reasons": job.match_reasons,
        "concerns": job.concerns,
        "missing_requirements": job.missing_requirements,
        "source_count": job.source_count,
        "sources": job.source_names,
    }
    if detail:
        data.update(
            {
                "description": job.description,
                "requirements": job.requirements,
                "responsibilities": job.responsibilities,
                "preferred_qualifications": job.preferred_qualifications,
                "score_breakdown": job.score_breakdown,
                "terms": job.terms,
                "degree_requirements": job.degree_requirements,
                "experience_required_years": job.experience_required_years,
                "requisition_id": job.requisition_id,
                "listings": [
                    {
                        "source": listing.source,
                        "source_kind": listing.source_kind,
                        "url": listing.url,
                        "apply_url": listing.apply_url,
                        "merge_method": listing.merge_method,
                        "merge_confidence": listing.merge_confidence,
                        "last_seen_at": listing.last_seen_at.isoformat()
                        if listing.last_seen_at
                        else None,
                    }
                    for listing in job.listings
                ],
            }
        )
    return data


@router.get("/jobs")
def list_jobs(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    """Canonical, deduplicated job list. One row per underlying position.

    Unlike the dashboard, this defaults to every lifecycle state: an API client
    asking for "the jobs" means all of them, and silently omitting the ones the
    user has applied to would be a surprising thing for a data endpoint to do.
    Pass ``view=review`` for what the feed shows.
    """
    params = dict(request.query_params)
    params.setdefault("view", "all")
    filters = q.JobFilters.from_query(params)
    page = q.search_jobs(db, filters, user)
    return {
        "total": page.total,
        "page": page.page,
        "per_page": page.per_page,
        "pages": page.pages,
        "jobs": [_serialize_job(job) for job in page.jobs],
    }


@router.get("/jobs/{job_id}")
def job_detail(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _serialize_job(job, detail=True)


@router.post("/jobs/{job_id}/status")
def change_status(
    job_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        status = JobStatus(str(payload.get("status", "")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid status") from exc
    set_status(db, job, status, user)
    db.commit()
    return {"ok": True, "job_id": job.id, "status": status.value}


@router.post("/jobs/{job_id}/notes")
def create_note(
    job_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    note = add_note(db, job, str(payload.get("body", "")), user)
    db.commit()
    if note is None:
        raise HTTPException(status_code=400, detail="empty note")
    return {"ok": True, "note_id": note.id}


@router.get("/counts")
def counts(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, int]:
    return q.dashboard_counts(db, user)


@router.get("/preferences")
def get_preferences(db: Session = Depends(get_db)) -> dict[str, Any]:
    return load_preferences(db).model_dump(mode="json")


@router.put("/preferences")
def put_preferences(
    payload: SearchPreferences, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    save_preferences(db, payload)
    db.commit()
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        from app.scheduler import reschedule

        reschedule(scheduler, payload.schedule)
    return {"ok": True}


@router.get("/profile")
def get_profile(db: Session = Depends(get_db)) -> dict[str, Any]:
    return load_profile(db).model_dump(mode="json")


@router.put("/profile")
def put_profile(payload: CandidateProfileData, db: Session = Depends(get_db)) -> dict[str, Any]:
    save_profile(db, payload)
    db.commit()
    return {"ok": True}


@router.get("/coverage")
def coverage(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Measured coverage for the most recent run.

    Reports only what actually happened -- a source that did not run is
    reported as such rather than counted as searched.
    """
    from app.pipeline.discovery import discovery_summary

    runs = q.recent_runs(db, limit=1)
    latest = runs[0] if runs else None
    return {
        "latest_run": (
            {
                "id": latest.id,
                "status": latest.status,
                "started_at": latest.started_at.isoformat(),
                "duration_seconds": latest.duration_seconds,
                "queries_generated": latest.queries_generated,
                "sources_attempted": latest.sources_attempted,
                "sources_successful": latest.sources_successful,
                "sources_failed": latest.sources_failed,
                "sources_unconfigured": latest.sources_unconfigured,
                "raw_jobs_found": latest.raw_jobs_found,
                "duplicates_removed": latest.duplicates_removed,
                "unique_jobs": latest.unique_jobs,
                "relevant_jobs": latest.relevant_jobs,
                "high_priority_jobs": latest.high_priority_jobs,
                "new_jobs": latest.new_jobs,
                "sources": [
                    {
                        "source": s.source,
                        "status": s.status,
                        "jobs_returned": s.jobs_returned,
                        "boards": f"{s.sub_targets_successful}/{s.sub_targets_attempted}"
                        if s.sub_targets_attempted
                        else None,
                        "duration_seconds": s.duration_seconds,
                        "error": s.error,
                    }
                    for s in latest.source_stats
                ],
            }
            if latest
            else None
        ),
        "discovery": discovery_summary(db),
        "source_yield": q.source_yield_stats(db),
    }


@router.get("/sources")
def sources(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    from app.sources.registry import source_catalog

    records = {row.name: row for row in db.scalars(select(JobSourceRecord)).all()}
    out = []
    for entry in source_catalog():
        record = records.get(str(entry["name"]))
        out.append(
            {
                **entry,
                "health": record.health if record else "not_run",
                "last_success_at": record.last_success_at.isoformat()
                if record and record.last_success_at
                else None,
                "last_error": record.last_error if record else None,
                "total_jobs_returned": record.total_jobs_returned if record else 0,
            }
        )
    return out


@router.get("/analytics")
def analytics(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"summary": q.analytics_summary(db), "outcomes": analytics_payload(db)}


@router.post("/search/run")
async def trigger_search(payload: dict = Body(default={})) -> dict[str, Any]:
    """Run the pipeline synchronously and return the measured report."""
    from app.pipeline.runner import run_search

    report = await run_search(
        trigger="api",
        notify=bool(payload.get("notify", False)),
        dry_run=bool(payload.get("dry_run", False)),
    )
    return {
        "run_id": report.run_id,
        "status": report.status,
        "duration_seconds": round(report.duration_seconds, 2),
        "raw_jobs_found": report.raw_jobs_found,
        "unique_jobs": report.unique_jobs,
        "duplicates_removed": report.duplicates_removed,
        "relevant_jobs": report.relevant_jobs,
        "new_jobs": report.new_jobs,
        "companies_discovered": report.companies_discovered,
        "boards_discovered": report.boards_discovered,
        "errors": report.errors,
    }


@router.post("/notifications/test")
async def notify_test(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    from app.notify.engine import send_test_notification

    result = await send_test_notification(db, str(payload.get("provider", "telegram")))
    db.commit()
    return {"ok": result.ok, "provider": result.provider, "error": result.error}


@router.get("/notifications")
def notifications(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [
        {
            "id": n.id,
            "kind": n.kind,
            "provider": n.provider,
            "status": n.status,
            "job_count": n.job_count,
            "created_at": n.created_at.isoformat(),
            "sent_at": n.sent_at.isoformat() if n.sent_at else None,
            "error": n.error,
        }
        for n in q.recent_notifications(db, limit=50)
    ]


@router.get("/schedule")
def schedule(request: Request) -> dict[str, Any]:
    from app.scheduler import describe_jobs

    return {"jobs": describe_jobs(getattr(request.app.state, "scheduler", None))}
