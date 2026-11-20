"""The cloud worker: drain the swipe queue, one application at a time.

Runs on GitHub Actions (see .github/workflows/autoapply.yml). A form on any
site but Greenhouse goes to the general agent (app/apply/agent.py, see
``_process_agent``). For each Greenhouse task:

1. Prepare every answer before touching a browser -- profile fields, the
   user's standing answers, rules, then a model for the free-text "why us".
2. Stop and hand the task back if any *required* question is still open. A
   blank the user fills in beats a guess sent under their name.
3. Fill the form in headless Chrome, click Submit, and only call it submitted
   when Greenhouse shows its confirmation. Anything else -- a validation
   error, a CAPTCHA challenge, silence -- is reported as "needs you" with a
   screenshot, never retried blindly.

The repository is public, so its Actions logs are too: this module logs task
ids and outcomes, never names, companies or answers.
"""

from __future__ import annotations

import contextvars
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.apply import greenhouse
from app.apply.answers import Draft, document_kind
from app.apply.cloud import (
    claim_next,
    profile_from_record,
    prune_screenshots,
    readiness,
    submitted_since,
    take_code,
)
from app.apply.run import draft_for
from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ApplicantDocument, ApplicantRecord, ApplyTask, Job, User
from app.models.autoapply import FAILED, NEEDS_YOU, QUEUED, REVIEW, RUNNING, SUBMITTED, WAITING_CODE
from app.models.base import JobStatus, utcnow

log = get_logger("autoapply.worker")

CONFIRMED = re.compile(
    r"thank you for (?:applying|your application|your interest)|application (?:has been |was )?"
    r"(?:submitted|received)|we.?ve received your application|successfully submitted",
    re.IGNORECASE,
)
SECURITY_CODE = re.compile(r"security code|verification code|confirm you.?re a human", re.IGNORECASE)
SUBMIT = 'form#application-form button[type="submit"], button:has-text("Submit application")'
ERRORS = (
    '[aria-invalid="true"], .helper-text--error, .error-message, [class*="error"][role="alert"]'
)


class Outcome:
    def __init__(self, status: str, detail: str, shot: bytes | None = None) -> None:
        self.status, self.detail, self.shot = status, detail, shot


def run(*, max_tasks: int = 25, dry_run: bool = False, say=print) -> dict[str, int]:
    """Process queued tasks until the queue is empty or a limit is reached.

    Each task thinks with its own user's model (app/services/user_llm.py):
    the deployment's credentials serve the owner's tasks only.
    """
    from app.services.user_llm import answerer_for, is_owner

    settings = get_settings()
    tally: dict[str, int] = {}
    seen: set[int] = set()
    capped: set[int] = set()  # users at their own daily limit; their tasks wait
    with session_scope() as session:
        prune_screenshots(session)
    for _ in range(max_tasks):
        with session_scope() as session:
            day_start = utcnow() - timedelta(hours=24)
            if not dry_run and submitted_since(session, day_start) >= settings.autoapply_daily_limit:
                say(f"daily limit of {settings.autoapply_daily_limit} reached; the rest wait")
                break
            task = claim_next(session, skip=seen, skip_users=capped)
            if task is None:
                break
            task_id, user_id = task.id, task.user_id
            seen.add(task_id)
            if (
                not dry_run
                and submitted_since(session, day_start, user_id=user_id)
                >= settings.autoapply_user_daily_limit
            ):
                # Back in the queue untouched: tomorrow's run takes it.
                task.status, task.started_at = QUEUED, None
                task.attempts = max(0, (task.attempts or 1) - 1)
                capped.add(user_id)
                say(f"task {task_id}: its user reached {settings.autoapply_user_daily_limit} today; waits")
                continue
            owner = is_owner(session, user_id)
            answerer = answerer_for(session, user_id)
        say(f"task {task_id}: started ({', '.join(answerer.names) or 'no model'})")
        token = _INBOX_ALLOWED.set(owner)
        try:
            outcome = _process(task_id, answerer, dry_run=dry_run)
        except Exception as exc:  # one broken form must not stop the queue
            log.exception("autoapply.task_crashed", task_id=task_id)
            outcome = Outcome(NEEDS_YOU, f"The worker hit an error ({type(exc).__name__}).")
        finally:
            _INBOX_ALLOWED.reset(token)
            answerer.close()
        _finish(task_id, outcome, dry_run=dry_run)
        tally[outcome.status] = tally.get(outcome.status, 0) + 1
        say(f"task {task_id}: {outcome.status}")
    return tally


#: Whether the task being processed may read the APPLY_IMAP_* inbox for its
#: security code. That inbox is the owner's: anyone else's code is never in it,
#: and a code from it would be the owner's, entered on someone else's form.
_INBOX_ALLOWED: contextvars.ContextVar[bool] = contextvars.ContextVar("inbox_allowed", default=False)


# ----------------------------------------------------------------- one task


def _process(task_id: int, answerer, *, dry_run: bool) -> Outcome:
    # Read everything in one short session: the browser can take minutes, and a
    # hosted Postgres drops connections held idle that long.
    with session_scope() as session:
        task = session.get(ApplyTask, task_id)
        job = session.get(Job, task.job_id)
        record = session.query(ApplicantRecord).filter_by(user_id=task.user_id).one_or_none()
        problems = readiness(record)
        if problems:
            return Outcome(NEEDS_YOU, "Your auto-apply profile is incomplete: " + "; ".join(problems) + ".")
        user = session.get(User, task.user_id)
        # Someone who checks applications first gets a draft to approve; once
        # approved, it is filled exactly as they left it.
        check_first = bool(user and user.review_applications and not task.approved)
        overrides, feedback = dict(task.overrides or {}), task.feedback or ""
        if task.by_agent:
            return _process_agent(
                session, task, job, record, answerer, dry_run=dry_run,
                check_first=check_first, overrides=overrides, feedback=feedback,
            )
        approved = bool(task.approved)
        _, token, job_id = task.form_key.split(":", 2)
        form = greenhouse.GreenhouseJob(token, job_id)
        company, title, canonical = job.company_name, job.title, job.canonical_job_id
        resume_name = Path(record.resume_filename or "resume.pdf").name
        resume_bytes = record.resume_bytes
        profile = profile_from_record(record)
        documents = [
            (d.kind, Path(d.filename).name, d.content, list(d.labels or []))
            for d in session.query(ApplicantDocument).filter_by(user_id=task.user_id)
        ]

    with tempfile.TemporaryDirectory() as tmp:
        # The original filename is what the recruiter sees attached.
        resume = Path(tmp) / resume_name
        resume.write_bytes(resume_bytes)
        profile.resume_path = str(resume)
        for kind, filename, content, labels in documents:
            # A folder each, so two documents with the same filename both survive.
            path = Path(tmp) / kind / filename
            path.parent.mkdir()
            path.write_bytes(content)
            profile.documents[kind] = {"path": str(path), "labels": labels}
        try:
            draft = draft_for(
                form, profile, company=company, title=title, canonical_job_id=canonical,
                # Approved: nothing new is written; what they saw is what goes.
                llm=None if approved else answerer,
                overrides=overrides, feedback=feedback,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return Outcome(FAILED, "The posting has closed.")
            return Outcome(NEEDS_YOU, f"Greenhouse answered HTTP {exc.response.status_code}.")
        _store_answers(task_id, draft)

        if draft.unresolved:
            labels = [a.label for a in draft.unresolved]
            # Enough of each question for the status page to ask it there.
            _store_left(task_id, [
                {"label": a.label, "type": a.type, "options": a.options[:100],
                 **({"kind": document_kind(a.label, a.name) or "other"} if a.type == "file" else {})}
                for a in draft.unresolved
            ])
            return Outcome(
                NEEDS_YOU, f"{len(labels)} required question(s) it would not answer for you."
            )
        if check_first:
            return Outcome(REVIEW, "Answers ready for you to check. Nothing has been sent.")
        return _fill_and_submit(draft, profile, dry_run=dry_run, task_id=task_id)

def _launch(p):
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        return p.chromium.launch(headless=True)


# ------------------------------------------------- any other format: the agent


def _process_agent(
    session,
    task: ApplyTask,
    job: Job,
    record,
    answerer,
    *,
    dry_run: bool,
    check_first: bool = False,
    overrides: dict | None = None,
    feedback: str = "",
) -> Outcome:
    """A form with no code of its own: the general agent works it out.

    Called inside the reading session; everything needed is copied out of it
    first, then the session is left alone while the browser runs.
    """
    from app.apply.cloud import recipe_for
    from app.apply.formats import format_of

    task_id, url = task.id, task.form_url
    found = format_of(url)
    if found is None:
        return Outcome(FAILED, "There is no application link for this job.")
    format_key, format_name = found
    if not answerer.names:
        return Outcome(
            NEEDS_YOU,
            f"Applying on {format_name} needs a model to read the form. Connect one under "
            "Your model (account menu), then press Try again.",
        )
    recipe = recipe_for(session, format_key)
    notes = recipe.notes if recipe else ""
    company, title = job.company_name or "", job.title or ""
    posting = " ".join((job.description or "").split())[:3000]
    resume_name = Path(record.resume_filename or "resume.pdf").name
    resume_bytes = record.resume_bytes
    profile = profile_from_record(record)
    documents = [
        (d.kind, Path(d.filename).name, d.content)
        for d in session.query(ApplicantDocument).filter_by(user_id=task.user_id)
    ]
    session.commit()  # release the connection before minutes of browsing

    from app.apply.agent import briefing
    from app.apply.profile import resume_text

    with tempfile.TemporaryDirectory() as tmp:
        files = {"resume": str(Path(tmp) / resume_name)}
        Path(files["resume"]).write_bytes(resume_bytes)
        profile.resume_path = files["resume"]
        for kind, filename, content in documents:
            path = Path(tmp) / kind / filename
            path.parent.mkdir()
            path.write_bytes(content)
            files[kind] = str(path)
        try:
            resume = resume_text(profile)
        except Exception:  # an unreadable PDF still gets uploaded; the model just reads less
            resume = ""
        brief = briefing(profile, resume=resume, company=company, title=title, posting=posting)
        if overrides:
            brief += "\n\nThe candidate checked these answers and wants them entered exactly:\n" + "\n".join(
                f"- {label}: {value}" for label, value in overrides.items()
            )
        if feedback.strip():
            brief += (
                "\n\nThe candidate read your previous draft for this form and asked for this "
                "(follow it, but never invent facts):\n" + feedback.strip()[:2000]
            )
        # Checking first: fill everything, stop before the final submit.
        result, shot = _run_agent(
            url, answerer, briefing=brief, files=files, recipe=notes, task_id=task_id,
            dry_run=dry_run or check_first,
        )

    if isinstance(result, str):
        return Outcome(NEEDS_YOU, f"The agent hit an error ({result}).", shot)
    ready = REVIEW if check_first and not dry_run else QUEUED
    status = {"submitted": SUBMITTED, "ready": ready, "failed": FAILED}.get(result.status, NEEDS_YOU)
    if not dry_run:
        with session_scope() as s:
            from app.apply.cloud import save_recipe

            save_recipe(s, format_key, format_name, notes=result.lessons, outcome=status, url=url)
    with session_scope() as s:
        t = s.get(ApplyTask, task_id)
        t.answers = [{**f, "source": "agent", "required": False} for f in result.filled]
        t.left_for_you = [
            {**q, **({"kind": document_kind(q["label"]) or "other"} if q["type"] == "file" else {})}
            for q in result.questions
        ]
    if status == REVIEW:
        return Outcome(REVIEW, f"{format_name}: filled in and ready for you to check. Nothing has been sent.", shot)
    say = f"{format_name}: " if status != SUBMITTED else ""
    detail = result.detail if status != SUBMITTED else f"Submitted on {format_name}; the site confirmed it."
    return Outcome(status, (say + detail)[:500], shot)


def _run_agent(url: str, answerer, *, briefing: str, files: dict, recipe: str, task_id: int, dry_run: bool):
    """(AgentResult, screenshot), or (the exception's name, screenshot) on a crash."""
    from playwright.sync_api import sync_playwright

    from app.apply.agent import WebAgent

    submitted_at = datetime.now().astimezone()
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            agent = WebAgent(
                page,
                answerer.complete_json,
                briefing=briefing,
                files=files,
                recipe=recipe,
                on_code=lambda retry: _wait_for_code(task_id, submitted_at, retry=retry),
                dry_run=dry_run,
            )
            try:
                result = agent.run(url)
            except Exception as exc:
                log.exception("autoapply.agent_crashed", task_id=task_id)
                result = type(exc).__name__
            return result, _shot(page)
        finally:
            browser.close()


def _fill_and_submit(draft: Draft, profile, *, dry_run: bool, task_id: int) -> Outcome:
    from playwright.sync_api import sync_playwright

    from app.apply.browser import fill

    with sync_playwright() as p:
        browser = _launch(p)
        try:
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            report = fill(page, draft, profile)
            required = {a.label for a in draft.answers if a.required}
            missed = [label for label in report.failed if label in required]
            if missed:
                _store_left(task_id, missed)
                return Outcome(
                    NEEDS_YOU, f"Could not place {len(missed)} required answer(s) on the form.", _shot(page)
                )
            if dry_run:
                return Outcome(QUEUED, "Dry run: filled in, not submitted.", _shot(page))

            submitted_at = datetime.now().astimezone()
            button = page.locator(SUBMIT).first
            if button.count() == 0:
                return Outcome(NEEDS_YOU, "Could not find the Submit button.", _shot(page))
            button.scroll_into_view_if_needed()
            button.click()
            return _await_result(page, profile, submitted_at, task_id=task_id)
        finally:
            browser.close()


#: How long the form is held open for the user to type in an emailed code.
CODE_WAIT_S = 15 * 60
CODE_POLL_S = 4
#: Wrong codes the user may retype before the task is handed back.
CODE_TRIES = 3


def _await_result(page, profile, submitted_at: datetime, timeout_s: float = 60, *, task_id: int | None = None) -> Outcome:
    deadline = time.monotonic() + timeout_s
    codes_entered = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        try:
            text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            continue
        if "confirmation" in page.url.lower() or CONFIRMED.search(text):
            return Outcome(SUBMITTED, "Submitted; Greenhouse confirmed it.", _shot(page, full=False))
        if _visible(page, 'iframe[title*="challenge" i]'):
            return Outcome(NEEDS_YOU, "Greenhouse showed a CAPTCHA challenge; submit this one yourself.", _shot(page))
        if SECURITY_CODE.search(text) and _code_box_visible(page):
            if codes_entered >= CODE_TRIES:
                return Outcome(NEEDS_YOU, "Greenhouse did not accept the security codes entered.", _shot(page))
            retry = codes_entered > 0
            code = _wait_for_code(task_id, submitted_at, retry=retry)
            if not code:
                return Outcome(
                    NEEDS_YOU,
                    "Greenhouse emailed you a security code and none was entered within "
                    f"{CODE_WAIT_S // 60} minutes. Retry, and enter the code when it asks.",
                    _shot(page),
                )
            _enter_code(page, code)
            codes_entered += 1
            # Give the code time to be checked before looking for the prompt again.
            page.wait_for_timeout(6000)
            deadline = time.monotonic() + timeout_s
            continue
        errors = _errors(page)
        if errors:
            return Outcome(NEEDS_YOU, "The form rejected it: " + "; ".join(errors[:3]), _shot(page))
    return Outcome(NEEDS_YOU, "No confirmation appeared after clicking Submit.", _shot(page))


def _code_box_visible(page) -> bool:
    return _visible(page, 'input[id^="security-input"], input[maxlength="1"], '
                          'input[id*="security" i], input[autocomplete="one-time-code"]')


def _wait_for_code(task_id: int | None, submitted_at: datetime, *, retry: bool) -> str | None:
    """The emailed code: typed by the user on the status page, or read from the inbox.

    The task shows as "Code needed" meanwhile, with a box for the code. The
    inbox is only a shortcut when mail credentials are set; the dashboard
    works without them.
    """
    from app.apply.inbox import credentials, wait_for_security_code

    if task_id is not None:
        _set_waiting(task_id, "That code was not accepted. Enter the newest code the site emailed you."
                     if retry else "The application site emailed you a security code. Enter it here.")
    has_inbox = _INBOX_ALLOWED.get() and credentials() is not None and not retry
    deadline = time.monotonic() + CODE_WAIT_S
    next_inbox = time.monotonic()
    try:
        while time.monotonic() < deadline:
            if task_id is not None:
                with session_scope() as session:
                    code = take_code(session, task_id)
                if code:
                    return code
            if has_inbox and time.monotonic() >= next_inbox:
                code = wait_for_security_code(since=submitted_at, timeout_s=1, poll_s=1)
                if code:
                    return code
                next_inbox = time.monotonic() + 20
            if task_id is None and not has_inbox:
                return None
            time.sleep(CODE_POLL_S)
        return None
    finally:
        if task_id is not None:
            _set_waiting(task_id, None)


def _set_waiting(task_id: int, detail: str | None) -> None:
    """Show (detail given) or clear the "Code needed" state on the task."""
    with session_scope() as session:
        task = session.get(ApplyTask, task_id)
        if detail is not None:
            task.status, task.detail = WAITING_CODE, detail
        elif task.status == WAITING_CODE:
            task.status, task.detail = RUNNING, None


def _visible(page, selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible()
    except Exception:
        return False


def _errors(page) -> list[str]:
    out = []
    try:
        for el in page.locator(ERRORS).all()[:8]:
            if not el.is_visible():
                continue
            text = (el.inner_text(timeout=1000) or "").strip()
            if not text:
                label = el.get_attribute("aria-label") or el.get_attribute("id") or ""
                text = f"field {label} is invalid" if label else ""
            if text and text not in out:
                out.append(text[:120])
    except Exception:
        pass
    return out


def _enter_code(page, code: str) -> None:
    """Type the emailed code: one box per character, or a single field."""
    boxes = page.locator('input[id^="security-input"], input[maxlength="1"]')
    if boxes.count() >= len(code):
        for i, char in enumerate(code):
            boxes.nth(i).fill(char)
    else:
        single = page.locator('input[id*="security" i], input[name*="code" i], input[autocomplete="one-time-code"]').first
        single.fill(code)
    page.wait_for_timeout(500)
    button = page.locator(SUBMIT).first
    if button.count():
        button.click()


def _shot(page, *, full: bool = True) -> bytes | None:
    try:
        # Quality 30 keeps a long form readable at about a third of the size:
        # these live in the database, and Neon's free tier is 0.5 GB.
        return page.screenshot(type="jpeg", quality=30, full_page=full)
    except Exception:
        return None


# ------------------------------------------------------------- bookkeeping


def _store_answers(task_id: int, draft: Draft) -> None:
    with session_scope() as session:
        task = session.get(ApplyTask, task_id)
        task.left_for_you = []
        task.answers = [
            {
                "label": a.label,
                "value": (Path(str(a.value)).name if a.type == "file" and a.value else a.value),
                "source": a.source,
                "required": a.required,
                "type": a.type,
                **({"options": a.options[:100]} if a.options else {}),
            }
            for a in draft.answers
            if a.answered
        ]


def _store_left(task_id: int, labels: list) -> None:
    with session_scope() as session:
        session.get(ApplyTask, task_id).left_for_you = labels


def _finish(task_id: int, outcome: Outcome, *, dry_run: bool) -> None:
    from app.services.actions import add_note, set_status

    now = utcnow()
    with session_scope() as session:
        task = session.get(ApplyTask, task_id)
        task.status, task.detail = outcome.status, outcome.detail
        task.finished_at = now
        if outcome.shot:
            task.screenshot = outcome.shot
        if outcome.status == SUBMITTED and not dry_run:
            task.submitted_at = now
            job, user = session.get(Job, task.job_id), session.get(User, task.user_id)
            set_status(session, job, JobStatus.APPLIED, user, now=now)
            add_note(session, job, "Submitted automatically after a right swipe.", user)
