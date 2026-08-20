"""One-click actions from a digest email.

These are the only routes that act without a session, which is the whole point:
a digest is read on a phone that may not be signed in. Authority comes from the
signed token in the URL, which names exactly one user, one job and one action.

Both GET and POST are accepted. Strictly a state change should not be a GET,
but a link in an email *is* a GET, and refusing that would defeat the feature.
The mitigations are that the token is unforgeable, expires, authorises only one
of three harmless and reversible actions, and every result page offers the
reverse action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.logging_setup import get_logger
from app.models.base import JobStatus
from app.services import action_tokens, auth, user_jobs
from app.services.actions import get_job, set_status
from app.web.templating import templates

log = get_logger("quick_actions")

router = APIRouter()

#: What the user is told happened, and how to undo it.
OUTCOMES: dict[str, dict[str, str]] = {
    "applied": {"verb": "Marked as applied", "undo": "saved", "undo_label": "Just save it instead"},
    "saved": {"verb": "Saved", "undo": "dismissed", "undo_label": "Actually, dismiss it"},
    "dismissed": {"verb": "Dismissed", "undo": "saved", "undo_label": "Undo — save it instead"},
}


def _render(request: Request, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "quick_action.html", context, status_code=context.pop("status_code", 200)
    )


@router.api_route("/a/{token}", methods=["GET", "POST"], include_in_schema=False)
async def quick_action(token: str, request: Request, db: Session = Depends(get_db)):
    claims = action_tokens.verify(token, request.app.state.signing_key)
    if claims is None:
        return _render(
            request,
            ok=False,
            headline="That link is no longer valid",
            detail=(
                "It may have expired, or been altered in transit. Open the "
                "dashboard and act on the job there."
            ),
            status_code=400,
        )

    user = auth.get_user(db, claims["user_id"])
    job = get_job(db, claims["job_id"])
    if user is None or job is None:
        return _render(
            request,
            ok=False,
            headline="That job is no longer available",
            detail="It may have been removed since the email was sent.",
            status_code=404,
        )

    status = JobStatus(claims["action"])
    set_status(db, job, status, user)
    db.commit()

    outcome = OUTCOMES.get(claims["action"], {})
    log.info(
        "quick_action.applied", user_id=user.id, job_id=job.id, action=claims["action"]
    )
    return _render(
        request,
        ok=True,
        headline=outcome.get("verb", "Updated"),
        job=job,
        detail=None,
        undo_url=(
            f"/a/{action_tokens.issue(user.id, job.id, outcome['undo'], request.app.state.signing_key)}"
            if outcome.get("undo")
            else None
        ),
        undo_label=outcome.get("undo_label"),
        current_status=user_jobs.status_of(user_jobs.get_state(db, user, job)),
    )
