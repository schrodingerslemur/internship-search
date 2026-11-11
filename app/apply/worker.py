"""The cloud worker: drain the swipe queue, one Greenhouse form at a time.

Runs on GitHub Actions (see .github/workflows/autoapply.yml). For each task:

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

import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.apply import greenhouse
from app.apply.answers import Draft
from app.apply.cloud import (
    claim_next,
    profile_from_record,
    prune_screenshots,
    readiness,
    submitted_since,
)
from app.apply.run import draft_for
from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import ApplicantRecord, ApplyTask, Job, User
from app.models.autoapply import FAILED, NEEDS_YOU, QUEUED, SUBMITTED
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
    """Process queued tasks until the queue is empty or a limit is reached."""
    from app.apply.agent_llm import apply_answerer

    settings = get_settings()
    tally: dict[str, int] = {}
    seen: set[int] = set()
    with session_scope() as session:
        prune_screenshots(session)
    answerer = apply_answerer()
    say(f"answer writers: {', '.join(answerer.names) or 'none (rules and standing answers only)'}")
    try:
        for _ in range(max_tasks):
            with session_scope() as session:
                day_start = utcnow() - timedelta(hours=24)
                if not dry_run and submitted_since(session, day_start) >= settings.autoapply_daily_limit:
                    say(f"daily limit of {settings.autoapply_daily_limit} reached; the rest wait")
                    break
                task = claim_next(session, skip=seen)
                if task is None:
                    break
                task_id = task.id
                seen.add(task_id)
            say(f"task {task_id}: started")
            try:
                outcome = _process(task_id, answerer, dry_run=dry_run)
            except Exception as exc:  # one broken form must not stop the queue
                log.exception("autoapply.task_crashed", task_id=task_id)
                outcome = Outcome(NEEDS_YOU, f"The worker hit an error ({type(exc).__name__}).")
            _finish(task_id, outcome, dry_run=dry_run)
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
            say(f"task {task_id}: {outcome.status}")
    finally:
        answerer.close()
    return tally


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
        _, token, job_id = task.form_key.split(":", 2)
        form = greenhouse.GreenhouseJob(token, job_id)
        company, title, canonical = job.company_name, job.title, job.canonical_job_id
        resume_name = Path(record.resume_filename or "resume.pdf").name
        resume_bytes = record.resume_bytes
        profile = profile_from_record(record)

    with tempfile.TemporaryDirectory() as tmp:
        # The original filename is what the recruiter sees attached.
        resume = Path(tmp) / resume_name
        resume.write_bytes(resume_bytes)
        profile.resume_path = str(resume)
        try:
            draft = draft_for(
                form, profile, llm=answerer, company=company, title=title,
                canonical_job_id=canonical,
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
                {"label": a.label, "type": a.type, "options": a.options[:100]}
                for a in draft.unresolved
            ])
            return Outcome(
                NEEDS_YOU, f"{len(labels)} required question(s) it would not answer for you."
            )
        return _fill_and_submit(draft, profile, dry_run=dry_run, task_id=task_id)

def _fill_and_submit(draft: Draft, profile, *, dry_run: bool, task_id: int) -> Outcome:
    from playwright.sync_api import sync_playwright

    from app.apply.browser import fill

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
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
            return _await_result(page, profile, submitted_at)
        finally:
            browser.close()


def _await_result(page, profile, submitted_at: datetime, timeout_s: float = 60) -> Outcome:
    deadline = time.monotonic() + timeout_s
    code_tried = False
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
        if SECURITY_CODE.search(text) and not code_tried:
            code_tried = True
            from app.apply.inbox import wait_for_security_code

            code = wait_for_security_code(since=submitted_at)
            if not code:
                return Outcome(
                    NEEDS_YOU,
                    "Greenhouse emailed you a security code and the worker has no inbox access "
                    "(set APPLY_IMAP_* or SMTP_* secrets), or the email did not arrive in time.",
                    _shot(page),
                )
            _enter_code(page, code)
            deadline = time.monotonic() + timeout_s
            continue
        errors = _errors(page)
        if errors:
            return Outcome(NEEDS_YOU, "The form rejected it: " + "; ".join(errors[:3]), _shot(page))
    return Outcome(NEEDS_YOU, "No confirmation appeared after clicking Submit.", _shot(page))


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
        task.answers = [
            {
                "label": a.label,
                "value": (Path(str(a.value)).name if a.type == "file" and a.value else a.value),
                "source": a.source,
                "required": a.required,
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
