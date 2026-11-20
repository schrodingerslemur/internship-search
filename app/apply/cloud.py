"""Swipe-to-apply: the deck, the queue, and how the cloud worker is woken.

A right swipe is the user's approval for one specific application. It puts an
``ApplyTask`` on the queue and pokes the GitHub Actions worker; the worker
fills the Greenhouse form and submits it, or hands it back with a reason.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, defer, load_only, selectinload

from app.apply.formats import format_of
from app.apply.picker import greenhouse_form
from app.apply.profile import ApplicantProfile, StandingAnswer
from app.config import get_settings
from app.logging_setup import get_logger
from app.models import (
    ApplicantDocument,
    ApplicantRecord,
    ApplyRecipe,
    ApplyTask,
    Job,
    JobListing,
    User,
    UserJobState,
)
from app.models.autoapply import (
    CANCELLED,
    FAILED,
    NEEDS_YOU,
    OPEN_STATUSES,
    QUEUED,
    REVIEW,
    RUNNING,
    SUBMITTED,
    WAITING_CODE,
)
from app.models.base import JobStatus, Priority, utcnow
from app.services import user_jobs

log = get_logger("autoapply")

#: A swipe deck is the best of what is undecided, not everything undecided.
#: "skip" is the ranker saying the job is not for you at all.
DECK_PRIORITIES = (
    Priority.APPLY_NOW.value,
    Priority.STRONG_MATCH.value,
    Priority.WORTH_CONSIDERING.value,
    Priority.MAYBE.value,
)
#: A worker that died mid-application (runner cancelled, OOM) leaves its task
#: "running" forever unless something notices. Half an hour is far longer than
#: any one form takes.
STALE_RUNNING = timedelta(minutes=30)
#: How often a swipe may wake the worker. GitHub keeps one pending run per
#: concurrency group, so extra dispatches are harmless -- just wasted calls.
DISPATCH_EVERY = timedelta(seconds=90)
#: A card must have been seen by a crawl this recently (the digest crawls daily,
#: so this tolerates a couple of missed runs). Rows with no sighting recorded
#: fall back to posting age.
SEEN_WITHIN = timedelta(days=4)
MAX_POSTING_AGE = timedelta(days=120)
#: Statuses that mean an application went in, however it was sent.
APPLIED_STATES = frozenset(
    {
        JobStatus.APPLIED.value,
        JobStatus.ASSESSMENT.value,
        JobStatus.INTERVIEW.value,
        JobStatus.OFFER.value,
        JobStatus.REJECTED.value,
    }
)


# ------------------------------------------------------------------- the deck


#: Only the columns a card shows. A Job row also carries the full description
#: and model-read facts, and a listing its raw crawler payload; loading those
#: for every candidate is most of the egress a deck would otherwise cost.
_CARD_COLUMNS = (
    Job.id, Job.company_name, Job.title, Job.location_raw, Job.remote_status,
    Job.salary_min, Job.salary_max, Job.salary_currency, Job.salary_period,
    Job.relevance_score, Job.priority, Job.match_reasons, Job.concerns,
    Job.date_posted, Job.terms, Job.deadline, Job.application_url, Job.posting_url,
)
_LISTING_COLUMNS = (JobListing.source, JobListing.source_job_id, JobListing.apply_url, JobListing.url)
_STATE_COLUMNS = (
    UserJobState.relevance_score, UserJobState.priority, UserJobState.match_reasons,
    UserJobState.concerns,
)
_GREENHOUSE = "%greenhouse.io%"


def _has_greenhouse_form():
    """SQL prefilter: some URL or listing points at a Greenhouse board."""
    listing = (
        select(JobListing.id)
        .where(
            JobListing.job_id == Job.id,
            or_(
                JobListing.source == "greenhouse",
                JobListing.apply_url.ilike(_GREENHOUSE),
                JobListing.url.ilike(_GREENHOUSE),
            ),
        )
        .exists()
    )
    return or_(Job.application_url.ilike(_GREENHOUSE), Job.posting_url.ilike(_GREENHOUSE), listing)


def deck(
    session: Session,
    user: User,
    *,
    limit: int = 10,
    exclude: set[int] | None = None,
    wider: bool = False,
) -> list[dict]:
    """Undecided jobs the worker can apply to, best first, as card data.

    ``wider`` is the "Load more" pass once that runs dry: every undecided match,
    including ones on boards the agent cannot fill (SmartRecruiters, Workday,
    ...) and ones the crawler has not re-sighted lately -- the same jobs the
    Review tab lists. Each card says whether the agent can apply (``agent``);
    for the rest a right swipe saves the job for you to apply to yourself.
    """
    state = UserJobState
    score = func.coalesce(state.relevance_score, Job.relevance_score)
    priority = func.coalesce(state.priority, Job.priority)
    taken = select(ApplyTask.job_id).where(
        ApplyTask.user_id == user.id, ApplyTask.status != CANCELLED
    )
    now = utcnow()

    rows = session.execute(
        select(Job, state)
        .outerjoin(state, and_(state.job_id == Job.id, state.user_id == user.id))
        .where(
            Job.is_active.is_(True),
            priority.in_(DECK_PRIORITIES),
            func.coalesce(state.status, JobStatus.NEW.value) == JobStatus.NEW.value,
            Job.id.not_in(taken),
            Job.id.not_in(sorted(exclude or ())),
            or_(Job.deadline.is_(None), Job.deadline >= now),
            *_deck_filters(now, wider),
        )
        .options(
            load_only(*_CARD_COLUMNS),
            selectinload(Job.listings).load_only(*_LISTING_COLUMNS),
        )
        .order_by(score.desc(), Job.id.desc())
        .limit(limit * 2 + 10)
    ).all()

    blocked = _never_apply_to(session, user)
    picked: list[tuple[Job, UserJobState | None]] = []
    for job, st in rows:
        name = (job.company_name or "").lower()
        if any(b in name for b in blocked) or (not wider and greenhouse_form(job) is None):
            continue
        picked.append((job, st))
        if len(picked) >= limit:
            break
    if not picked:
        return []

    ids = [job.id for job, _ in picked]
    blurbs = {
        job_id: (facts, text)
        for job_id, facts, text in session.execute(
            select(Job.id, Job.enrichment, func.substr(Job.description, 1, 700)).where(Job.id.in_(ids))
        )
    }
    applied_before = _applied_companies(session, user)
    learned = learned_formats(session)
    return [_card(job, st, blurbs.get(job.id), applied_before, learned) for job, st in picked]


def _deck_filters(now: datetime, wider: bool) -> tuple:
    fresh_posting = or_(Job.date_posted.is_(None), Job.date_posted >= now - MAX_POSTING_AGE)
    if wider:
        # Seen lately, or posted recently enough to still be plausible.
        return (or_(Job.last_seen_at >= now - SEEN_WITHIN, fresh_posting),)
    return (
        # Still live: the crawler saw it recently. Posting age alone would hide
        # evergreen roles that are old but open; a sighting is what proves a
        # swipe will not just come back as "posting closed".
        or_(
            Job.last_seen_at >= now - SEEN_WITHIN,
            and_(Job.last_seen_at.is_(None), fresh_posting),
        ),
        _has_greenhouse_form(),
    )


def _board_name(job: Job) -> str:
    """Where the application form lives, for "Agent · Workday" on a card."""
    found = format_of(job_apply_url(job))
    return found[1] if found else "the company site"


def _card(job: Job, st, blurb, applied_before: set[str], learned: dict[str, int] | None = None) -> dict:
    from app.web.templating import salary_text

    facts, text = blurb or (None, "")
    summary = (facts or {}).get("summary") if isinstance(facts, dict) else None
    summary = " ".join(str(summary or text or "").split())
    if len(summary) > 360:
        summary = summary[:360].rsplit(" ", 1)[0] + "…"
    salary = salary_text(job)
    return {
        "id": job.id,
        "company": job.company_name,
        "title": job.title,
        "facts": [
            f
            for f in (
                job.location_raw,
                job.remote_status if job.remote_status in ("remote", "hybrid") else "",
                "" if salary == "Not listed" else salary,
                *(job.terms or [])[:2],
            )
            if f
        ],
        "score": round(user_jobs.score_of(st, job) or 0),
        "reasons": [str(r) for r in ((st.match_reasons if st else None) or job.match_reasons or [])[:3]],
        "concern": next(iter((st.concerns if st else None) or job.concerns or []), ""),
        "applied_before": (job.company_name or "").lower() in applied_before,
        "summary": summary,
        "url": job.posting_url or job.application_url or "",
        "agent": greenhouse_form(job) is not None or bool(job_apply_url(job)),
        # The general agent fills it; "new" until it has applied on that format once.
        "format": _format_state(job, learned or {}),
        "board": _board_name(job),
    }


def _format_state(job: Job, learned: dict[str, int]) -> str:
    """greenhouse, learned (the agent has applied on this format before) or new."""
    if greenhouse_form(job) is not None:
        return "greenhouse"
    found = format_of(job_apply_url(job))
    return "learned" if found and learned.get(found[0], 0) > 0 else "new"


def _applied_companies(session: Session, user: User) -> set[str]:
    """Employers this user has applied to, through the agent or on their own."""
    rows = session.scalars(
        select(func.lower(Job.company_name))
        .join(UserJobState, UserJobState.job_id == Job.id)
        .where(UserJobState.user_id == user.id, UserJobState.status.in_(sorted(APPLIED_STATES)))
        .distinct()
    )
    return {r for r in rows if r}


def _never_apply_to(session: Session, user: User) -> list[str]:
    data = session.scalar(select(ApplicantRecord.data).where(ApplicantRecord.user_id == user.id))
    names = (data or {}).get("never_apply_to", [])
    return [str(n).lower() for n in names if str(n).strip()]

# ------------------------------------------------------------------ the queue


def lean_job(session: Session, job_id: int) -> Job | None:
    """A job row without its description, for actions that only need its id."""
    return session.get(
        Job, job_id, options=[load_only(Job.id, Job.relevance_score, Job.deadline, Job.company_name,
                                        Job.application_url, Job.posting_url)]
    )


def form_for(session: Session, job: Job):
    """The job's Greenhouse form, reading only the listing columns that name it."""
    from app.apply.greenhouse import from_listing

    for source, source_job_id, apply_url, url in session.execute(
        select(*_LISTING_COLUMNS).where(JobListing.job_id == job.id)
    ):
        found = from_listing(source, source_job_id, [apply_url, url])
        if found:
            return found
    return from_listing("", None, [job.application_url, job.posting_url])


def job_apply_url(job: Job, session: Session | None = None) -> str | None:
    """Where a person would start applying: the application link, else the posting."""
    urls = [job.application_url]
    if session is not None:
        urls += [
            u
            for apply_url, url in session.execute(
                select(JobListing.apply_url, JobListing.url).where(JobListing.job_id == job.id)
            )
            for u in (apply_url, url)
        ]
    urls.append(job.posting_url)
    return next((u for u in urls if u and u.startswith("http")), None)


def enqueue(session: Session, user: User, job: Job) -> ApplyTask | None:
    """Approve one application. None when the job has no link to apply from.

    A Greenhouse form is filled by its own code; anything else goes to the
    general agent, which works the form out (app/apply/agent.py).
    """
    form = form_for(session, job)
    url = None
    if form is not None:
        key = form.key
    else:
        url = job_apply_url(job, session)
        found = format_of(url)
        if found is None:
            return None
        key = f"agent:{found[0]}"
    task = session.scalar(
        select(ApplyTask).where(ApplyTask.user_id == user.id, ApplyTask.job_id == job.id)
    )
    now = utcnow()
    if task is None:
        task = ApplyTask(user_id=user.id, job_id=job.id, form_key=key, queued_at=now)
        session.add(task)
    elif task.status in (SUBMITTED, RUNNING):
        return task
    task.form_key, task.apply_url = key, url
    task.status, task.detail, task.queued_at = QUEUED, None, now
    task.finished_at = None
    # A fresh go starts unapproved; approve() and redraft() set these after.
    task.approved, task.overrides, task.feedback = False, {}, None
    # Saved, not applied: the tracker says applied only once a form went in.
    user_jobs.set_status(session, user, job, JobStatus.SAVED.value, now=now)
    session.flush()
    return task


def approve(session: Session, user: User, task: ApplyTask, answers: dict[str, str]) -> ApplyTask | None:
    """The user checked the draft (perhaps editing it): queue it to be sent as is."""
    job = lean_job(session, task.job_id)
    queued = enqueue(session, user, job) if job is not None else None
    if queued is not None:
        queued.overrides = {k: v for k, v in answers.items() if k.strip()}
        queued.approved = True
        session.flush()
    return queued


def redraft(session: Session, user: User, task: ApplyTask, feedback: str) -> ApplyTask | None:
    """Send the draft back to the agent with the user's feedback; it comes back to check."""
    job = lean_job(session, task.job_id)
    queued = enqueue(session, user, job) if job is not None else None
    if queued is not None:
        queued.feedback = feedback.strip()[:2000]
        session.flush()
    return queued


#: Contact fields the fill-it-myself bookmarklet recognises by field name.
FILL_PROFILE_KEYS = ("first_name", "last_name", "email", "phone", "linkedin", "github", "website", "location")


def fill_code(task: ApplyTask, record: ApplicantRecord | None) -> str:
    """Everything the user's own browser needs to fill this form: a base64url
    JSON of the drafted answers, contact details, and what is still theirs.

    It travels in the form URL's fragment, which browsers never send to a
    server (app/web/static/fill.js reads it, then removes it).
    """
    data = (record.data or {}) if record is not None else {}
    answers = [
        {"label": a.get("label", ""), "value": a.get("value")}
        for a in task.answers or []
        if a.get("type") != "file" and a.get("value") not in (None, "", [])
    ]
    ask = [
        (q.get("label") if isinstance(q, dict) else str(q))
        for q in task.left_for_you or []
        if not (isinstance(q, dict) and q.get("type") == "file")
    ]
    payload = {
        "answers": answers,
        "profile": {k: data.get(k) for k in FILL_PROFILE_KEYS if data.get(k)},
        "ask": [a for a in ask if a],
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def cancel(session: Session, user: User, job: Job) -> bool:
    """Withdraw a swipe the worker has not acted on yet."""
    task = session.scalar(
        select(ApplyTask).where(ApplyTask.user_id == user.id, ApplyTask.job_id == job.id)
    )
    if task is None or task.status == CANCELLED:
        return True  # nothing to withdraw
    if task.status != QUEUED:
        return False
    task.status = CANCELLED
    task.finished_at = utcnow()
    session.flush()
    return True


def mark_applied_elsewhere(session: Session, user: User, job: Job) -> None:
    """The user already applied on their own: record it, never queue it."""
    from app.services.actions import add_note, set_status

    set_status(session, job, JobStatus.APPLIED, user)
    add_note(session, job, "Applied outside the dashboard.", user)


def tasks_for(session: Session, user: User, limit: int = 60) -> list[ApplyTask]:
    """Recent tasks, newest first, without loading any screenshot bytes.

    Each task gets a plain ``has_screenshot`` attribute instead: a status page
    that pulled every JPEG would spend Neon's egress allowance on images
    nobody opened.
    """
    rows = session.execute(
        select(ApplyTask, ApplyTask.screenshot.is_not(None))
        .options(defer(ApplyTask.screenshot), selectinload(ApplyTask.job).load_only(Job.id, Job.company_name, Job.title))
        .where(ApplyTask.user_id == user.id, ApplyTask.status != CANCELLED)
        .order_by(ApplyTask.queued_at.desc())
        .limit(limit)
    ).all()
    out = []
    for task, has_shot in rows:
        task.has_screenshot = bool(has_shot)
        out.append(task)
    return out


def prune_screenshots(session: Session, *, older_than: timedelta = timedelta(days=14)) -> int:
    """Drop old screenshots; the answers and outcome stay. Keeps storage flat."""
    from sqlalchemy import update

    result = session.execute(
        update(ApplyTask)
        .where(ApplyTask.screenshot.is_not(None), ApplyTask.finished_at < utcnow() - older_than)
        .values(screenshot=None)
    )
    return result.rowcount or 0


def code_key(task_id: int) -> str:
    return f"autoapply.code.{task_id}"


def give_code(session: Session, task: ApplyTask, code: str) -> bool:
    """Hand the worker the security code the user typed in."""
    code = re.sub(r"\s+", "", code or "")
    if task.status != WAITING_CODE or not code or len(code) > 20:
        return False
    from app.services.appconfig import set_config

    set_config(session, code_key(task.id), code)
    session.flush()
    return True


def take_code(session: Session, task_id: int) -> str | None:
    """The typed code, if any, consumed so it is entered only once."""
    from app.services.appconfig import get_config, set_config

    code = get_config(session, code_key(task_id))
    if code:
        set_config(session, code_key(task_id), "")
    return code or None


def queue_counts(session: Session, user: User) -> dict[str, int]:
    rows = session.execute(
        select(ApplyTask.status, func.count())
        .where(ApplyTask.user_id == user.id)
        .group_by(ApplyTask.status)
    ).all()
    return {status: n for status, n in rows}


def claim_next(
    session: Session,
    *,
    now: datetime | None = None,
    skip: set[int] | None = None,
    skip_users: set[int] | None = None,
) -> ApplyTask | None:
    """Take the oldest queued task, reclaiming any a dead worker abandoned.

    One worker runs at a time (the workflow's concurrency group), so a plain
    read-then-write is enough; no row locks are needed.
    """
    now = now or utcnow()
    for stale in session.scalars(
        select(ApplyTask).where(
            ApplyTask.status.in_((RUNNING, WAITING_CODE)),
            ApplyTask.started_at < now - STALE_RUNNING,
        )
    ):
        stale.status = QUEUED if stale.attempts < 2 else FAILED
        stale.detail = "The worker stopped mid-application."
    task = session.scalar(
        select(ApplyTask)
        .where(
            ApplyTask.status == QUEUED,
            ApplyTask.id.not_in(sorted(skip or ())),
            ApplyTask.user_id.not_in(sorted(skip_users or ())),
        )
        .order_by(ApplyTask.queued_at)
        .limit(1)
    )
    if task is not None:
        task.status, task.started_at = RUNNING, now
        task.attempts = (task.attempts or 0) + 1
    session.flush()
    return task


def submitted_since(session: Session, since: datetime, user_id: int | None = None) -> int:
    """Submissions since ``since``: everyone's, or one user's."""
    query = select(func.count()).select_from(ApplyTask).where(ApplyTask.submitted_at >= since)
    if user_id is not None:
        query = query.where(ApplyTask.user_id == user_id)
    return session.scalar(query) or 0


def pending_count(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(ApplyTask).where(ApplyTask.status.in_(OPEN_STATUSES))
    ) or 0


# ---------------------------------------------------------- the applicant


def get_record(session: Session, user: User) -> ApplicantRecord | None:
    """The applicant record, with the resume bytes left in the database.

    Only the worker needs the PDF; touching ``resume_bytes`` loads it then.
    """
    return session.scalar(
        select(ApplicantRecord)
        .options(defer(ApplicantRecord.resume_bytes))
        .where(ApplicantRecord.user_id == user.id)
    )


def profile_data(profile: ApplicantProfile) -> dict:
    data = asdict(profile)
    data.pop("resume_path", None)
    data.pop("documents", None)
    return data


def save_record(
    session: Session,
    user: User,
    *,
    data: dict | None = None,
    resume: tuple[str, bytes] | None = None,
) -> ApplicantRecord:
    record = get_record(session, user)
    if record is None:
        record = ApplicantRecord(user_id=user.id, data={})
        session.add(record)
    if data is not None:
        record.data = clean_profile_data(data)
    if resume is not None:
        record.resume_filename, record.resume_bytes = resume
    session.flush()
    return record


def remember_answers(session: Session, user: User, replies: dict[str, str]) -> int:
    """Save the user's typed replies as standing answers, keyed by the exact label.

    Standing answers win over every rule and the model, so the retried form
    gets these verbatim, and any later form asking the same question does too.
    """
    record = get_record(session, user)
    if record is None:
        return 0
    data = dict(record.data or {})
    standing = [s for s in data.get("standing_answers") or [] if isinstance(s, dict)]
    saved = 0
    for label, answer in replies.items():
        label, answer = label.strip(), answer.strip()
        if not label or not answer:
            continue
        match = rf"^\s*{re.escape(label)}\s*$"
        standing = [s for s in standing if s.get("match") != match]
        # Newest first: an exact-label answer beats any older broad pattern.
        standing.insert(0, {"match": match, "answer": answer})
        saved += 1
    if saved:
        data["standing_answers"] = standing
        record.data = clean_profile_data(data)
        session.flush()
    return saved


def clean_profile_data(raw: dict) -> dict:
    """Keep only fields ApplicantProfile knows; raise on anything malformed."""
    if not isinstance(raw, dict):
        raise ValueError("the profile must be a JSON object")
    known = {f.name for f in fields(ApplicantProfile)} - {"resume_path", "documents"}
    data = {k: v for k, v in raw.items() if k in known}
    standing = data.get("standing_answers") or []
    data["standing_answers"] = [asdict(StandingAnswer(**s)) for s in standing]
    if "other_education" in data:
        from app.apply.profile import clean_education

        data["other_education"] = clean_education(data["other_education"])
    ApplicantProfile(**{**data, "standing_answers": []})  # type check by construction
    return data


def documents_for(session: Session, user_id: int) -> list[ApplicantDocument]:
    """The user's documents, without their bytes."""
    return list(session.scalars(
        select(ApplicantDocument)
        .options(defer(ApplicantDocument.content))
        .where(ApplicantDocument.user_id == user_id)
        .order_by(ApplicantDocument.kind)
    ))


def save_document(
    session: Session, user: User, kind: str, filename: str, content: bytes, *, label: str | None = None
) -> ApplicantDocument:
    """Add or replace the user's document of one kind.

    ``label`` is the exact question it was uploaded to answer; it is kept, so
    every later form asking that same question gets the same file.
    """
    from app.apply.answers import DOCUMENT_KINDS

    if kind not in DOCUMENT_KINDS:
        raise ValueError(f"unknown document kind {kind!r}")
    doc = session.scalar(
        select(ApplicantDocument).where(ApplicantDocument.user_id == user.id, ApplicantDocument.kind == kind)
    )
    if doc is None:
        doc = ApplicantDocument(user_id=user.id, kind=kind, labels=[])
        session.add(doc)
    doc.filename, doc.content = Path(filename).name[:300] or "document.pdf", content
    if label and label.strip() and label.strip() not in (doc.labels or []):
        doc.labels = [*(doc.labels or []), label.strip()][-20:]
    session.flush()
    return doc


def delete_document(session: Session, user: User, kind: str) -> bool:
    doc = session.scalar(
        select(ApplicantDocument).where(ApplicantDocument.user_id == user.id, ApplicantDocument.kind == kind)
    )
    if doc is None:
        return False
    session.delete(doc)
    session.flush()
    return True


def profile_from_record(record: ApplicantRecord, resume_path: str = "") -> ApplicantProfile:
    data = dict(record.data or {})
    data.pop("documents", None)
    standing = [StandingAnswer(**s) for s in data.pop("standing_answers", []) or []]
    profile = ApplicantProfile(**data)
    profile.standing_answers = standing
    profile.resume_path = resume_path
    return profile


def readiness(record: ApplicantRecord | None) -> list[str]:
    """What is missing before the worker can apply for this user."""
    if record is None:
        return ["No applicant profile yet."]
    problems = []
    data = record.data or {}
    for name in ("first_name", "last_name", "email", "phone"):
        if not data.get(name):
            problems.append(f"{name.replace('_', ' ')} is empty")
    if not record.resume_filename:
        problems.append("no resume uploaded")
    return problems


def profile_json(record: ApplicantRecord | None) -> str:
    if record is None or not record.data:
        from app.config import PROJECT_ROOT

        example = PROJECT_ROOT / "app" / "apply" / "profile.example.json"
        try:
            raw = json.loads(example.read_text(encoding="utf-8"))
            raw.pop("_comment", None)
            raw.pop("resume_path", None)
            return json.dumps(raw, indent=2)
        except OSError:
            return json.dumps(profile_data(ApplicantProfile()), indent=2)
    return json.dumps(record.data, indent=2)


# ------------------------------------------------- how-tos, per format


def learned_formats(session: Session) -> dict[str, int]:
    """format key -> successful applications, for every format tried so far."""
    return {k: n or 0 for k, n in session.execute(select(ApplyRecipe.format_key, ApplyRecipe.successes))}


def recipe_for(session: Session, format_key: str) -> ApplyRecipe | None:
    return session.scalar(select(ApplyRecipe).where(ApplyRecipe.format_key == format_key))


def recipes(session: Session) -> list[ApplyRecipe]:
    return list(session.scalars(select(ApplyRecipe).order_by(ApplyRecipe.updated_at.desc())))


def save_recipe(
    session: Session, format_key: str, name: str, *, notes: str, outcome: str, url: str | None
) -> ApplyRecipe:
    """Record how an attempt on this format went, and the agent's rewritten how-to.

    An attempt that learned nothing (no notes) keeps the previous how-to.
    """
    recipe = recipe_for(session, format_key)
    if recipe is None:
        recipe = ApplyRecipe(format_key=format_key, name=name[:120], notes="", successes=0, failures=0)
        session.add(recipe)
    if notes.strip():
        recipe.notes = notes.strip()
    if outcome == SUBMITTED:
        recipe.successes = (recipe.successes or 0) + 1
    elif outcome in (NEEDS_YOU, FAILED):
        recipe.failures = (recipe.failures or 0) + 1
    recipe.last_outcome, recipe.last_url = outcome, url
    session.flush()
    return recipe


# ------------------------------------------------------ waking the worker


def dispatch_worker(session: Session, *, now: datetime | None = None) -> str:
    """Start the GitHub Actions worker now. Returns what happened, in words.

    Never raises: a swipe must land even when GitHub is unreachable, because
    the worker's own schedule will reach the queue regardless.
    """
    settings = get_settings()
    if not settings.gh_dispatch_token:
        return "scheduled"
    from app.services.appconfig import get_config, set_config

    now = now or utcnow()
    last = get_config(session, "autoapply.last_dispatch")
    if last:
        try:
            if now - datetime.fromisoformat(last) < DISPATCH_EVERY:
                return "already running"
        except ValueError:
            pass
    try:
        response = httpx.post(
            f"https://api.github.com/repos/{settings.gh_repo}/actions/workflows/"
            f"{settings.gh_autoapply_workflow}/dispatches",
            headers={
                "Authorization": f"Bearer {settings.gh_dispatch_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        log.warning("autoapply.dispatch_failed", error=type(exc).__name__)
        return "scheduled"
    if response.status_code >= 300:
        log.warning("autoapply.dispatch_rejected", status=response.status_code)
        return "scheduled"
    set_config(session, "autoapply.last_dispatch", now.isoformat())
    return "started"


STATUS_LABELS = {
    QUEUED: ("Queued", "warn"),
    RUNNING: ("Applying…", "accent"),
    SUBMITTED: ("Submitted", "ok"),
    NEEDS_YOU: ("Needs you", "bad"),
    FAILED: ("Couldn't apply", "dim"),
    CANCELLED: ("Cancelled", "dim"),
    WAITING_CODE: ("Code needed", "bad"),
    REVIEW: ("Ready to check", "warn"),
}
