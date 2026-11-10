"""Prepare today's applications, then walk the applicant through submitting them."""

from __future__ import annotations

import html
from collections.abc import Callable
from datetime import date

import httpx

from app.apply import greenhouse, ledger
from app.apply.answers import Draft, build_draft, prepare_answers
from app.apply.greenhouse import GreenhouseJob
from app.apply.profile import ApplicantProfile, resume_text
from app.pipeline.textutil import strip_html

Say = Callable[[str], None]


def draft_for(
    form: GreenhouseJob,
    profile: ApplicantProfile,
    *,
    llm=None,
    company: str | None = None,
    title: str | None = None,
    canonical_job_id: str | None = None,
    client: httpx.Client | None = None,
) -> Draft:
    payload, questions = greenhouse.fetch(form, client)
    company = company or payload.get("company_name") or form.token
    posting = strip_html(html.unescape(payload.get("content") or ""))
    answers = prepare_answers(
        questions,
        profile,
        company=company,
        resume=resume_text(profile),
        posting=f"{payload.get('title', '')}\n{posting}",
        llm=llm,
    )
    return build_draft(
        key=form.key,
        canonical_job_id=canonical_job_id,
        company=company,
        title=title or payload.get("title") or "",
        form_url=form.form_url,
        answers=answers,
    )


def prepare(session, user, profile: ApplicantProfile, *, llm=None, say: Say = print) -> list[Draft]:
    """Draft up to the day's remaining cap from the best Greenhouse matches."""
    from app.apply.picker import pick

    remaining = profile.daily_cap - ledger.prepared_on(date.today())
    if remaining <= 0:
        say(f"Already prepared {profile.daily_cap} today (the daily cap).")
        return []

    candidates = pick(session, user, profile, limit=remaining, skip_keys=ledger.known_keys())
    if not candidates:
        say("No new strong matches with a Greenhouse form right now.")
        return []

    drafts = []
    with httpx.Client(timeout=25) as client:
        for c in candidates:
            label = f"{c.job.company_name} - {c.job.title}"
            try:
                draft = draft_for(
                    c.form,
                    profile,
                    llm=llm,
                    company=c.job.company_name,
                    title=c.job.title,
                    canonical_job_id=c.job.canonical_job_id,
                    client=client,
                )
            except httpx.HTTPStatusError as exc:
                ledger.set_status(c.form.key, ledger.FAILED, f"HTTP {exc.response.status_code}")
                say(f"  skipped {label}: the posting is gone ({exc.response.status_code})")
                continue
            except Exception as exc:
                say(f"  skipped {label}: {type(exc).__name__}: {exc}"[:200])
                continue
            ledger.record_draft(draft)
            drafts.append(draft)
            todo = len(draft.unresolved)
            say(f"  prepared {label}" + (f"  ({todo} question(s) for you)" if todo else ""))
    return drafts


def describe(draft: Draft, report=None) -> list[str]:
    lines = [f"{draft.company} - {draft.title}", f"  {draft.form_url}"]
    written = [a for a in draft.answers if a.source == "ai"]
    if written:
        lines.append("  Written by AI -- read these before submitting:")
        lines += [f"    * {a.label[:80]}" for a in written]
    if report is not None:
        todo = report.left_for_you + report.failed
    else:
        todo = [a.label for a in draft.unresolved]
    if todo:
        lines.append("  Fill these in yourself:")
        lines += [f"    * {label[:80]}" for label in todo]
    return lines


def resolve_user(session, email: str | None = None, fallback_email: str | None = None):
    """The account applications are recorded against.

    An explicit email wins; a single-account database needs no choice; a
    shared one falls back to the applicant's own email (``fallback_email``,
    normally the profile's), then to the account the digest is addressed to.
    """
    from app.config import get_settings
    from app.services.auth import find_by_email
    from app.services.preferences import all_active_users

    if email:
        user = find_by_email(session, email)
        if user is None:
            raise LookupError(f"no account with email {email}")
        return user
    users = all_active_users(session)
    if len(users) == 1:
        return users[0]
    own = (fallback_email or "").lower()
    for user in users:
        if own and own in ((user.email or "").lower(), (user.notification_email or "").lower()):
            return user
    target = (get_settings().email_to or "").lower()
    for user in users:
        if target and (user.notification_email or "").lower() == target:
            return user
    known = ", ".join(u.email or str(u.id) for u in users) or "none"
    raise LookupError(f"several accounts ({known}); pass --email to pick one")


def mark_applied(
    draft: Draft, email: str | None = None, fallback_email: str | None = None
) -> bool:
    """Record the application in the tracker, if the job is in this database.

    Opens its own short session: the browser can sit on one form for half an
    hour, and a hosted database drops connections held idle that long.
    """
    if not draft.canonical_job_id:
        return False
    from app.db import session_scope
    from app.models.base import JobStatus
    from app.services.actions import add_note, get_job, set_status

    with session_scope() as session:
        job = get_job(session, draft.canonical_job_id)
        if job is None:
            return False
        user = resolve_user(session, email, fallback_email)
        set_status(session, job, JobStatus.APPLIED, user)
        add_note(session, job, "Submitted with the application assistant.", user)
    return True


def submit_session(
    drafts: list[Draft],
    profile: ApplicantProfile,
    *,
    on_submitted: Callable[[Draft], bool] | None = None,
    say: Say = print,
) -> dict[str, int]:
    """Open each draft filled in; the applicant submits or closes the tab."""
    from playwright.sync_api import sync_playwright

    from app.apply.browser import fill, launch, wait_for_submission

    tally = {"submitted": 0, "skipped": 0, "unfinished": 0}
    with sync_playwright() as p:
        context = launch(p)
        try:
            for n, draft in enumerate(drafts, 1):
                try:
                    page = context.new_page()
                except Exception:
                    say("The browser was closed; the rest stay queued for next time.")
                    break
                say("")
                say(f"[{n}/{len(drafts)}] filling...")
                try:
                    report = fill(page, draft, profile)
                except Exception as exc:
                    say(f"  could not fill the form ({type(exc).__name__}); fill it by hand")
                    report = None
                for line in describe(draft, report):
                    say(line)
                say("  -> Review it, then click Submit. Close the tab to skip this job.")

                outcome = wait_for_submission(page)
                if outcome == "submitted":
                    ledger.set_status(draft.key, ledger.SUBMITTED)
                    try:
                        tracked = bool(on_submitted and on_submitted(draft))
                    except Exception as exc:
                        tracked = False
                        say(f"  (could not update the tracker: {type(exc).__name__})")
                    say("  submitted" + (" and marked applied in the tracker" if tracked else ""))
                    tally["submitted"] += 1
                    if not page.is_closed():
                        page.close()
                elif outcome == "closed":
                    ledger.set_status(draft.key, ledger.SKIPPED)
                    say("  skipped")
                    tally["skipped"] += 1
                else:
                    say("  no submission seen; it stays in the queue for next time")
                    tally["unfinished"] += 1
                    if not page.is_closed():
                        page.close()
        finally:
            context.close()
    return tally


def check(draft: Draft, profile: ApplicantProfile, screenshot) -> list[str]:
    """Fill a form headlessly and screenshot it. Never submits, never waits."""
    from playwright.sync_api import sync_playwright

    from app.apply.browser import fill, launch

    with sync_playwright() as p:
        context = launch(p, headless=True)
        try:
            page = context.new_page()
            report = fill(page, draft, profile)
            page.screenshot(path=str(screenshot), full_page=True)
        finally:
            context.close()
    return describe(draft, report) + [f"  filled {len(report.filled)} field(s)"]
