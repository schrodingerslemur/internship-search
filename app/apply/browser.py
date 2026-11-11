"""Fill a Greenhouse form in a real browser, then hand it to the applicant.

This module never clicks Submit. The form is protected by reCAPTCHA
Enterprise, which exists to tell people from programs; the applicant reviews
the filled form and submits it themselves, and the browser only watches for
the confirmation page so the tracker can record the application.

The hosted form is a React app: text fields are plain inputs addressed by the
question's field name, but every dropdown is a combobox that has to be opened,
optionally typed into, and have an option clicked.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.apply.answers import DECLINE, DEMOGRAPHIC, Answer, Draft, closest_option, norm, pick_option
from app.apply.profile import ApplicantProfile, apply_dir

CONFIRMED = re.compile(
    r"thank you for (?:applying|your application)|application (?:has been |was )?"
    r"(?:submitted|received)|we.?ve received your application",
    re.IGNORECASE,
)
#: Anything that looks like a menu option. Too broad to use alone: the phone
#: field's hidden country list is a couple of hundred of these.
OPTION = '[role="option"]'


def _options_for(element_id: str) -> str:
    """The open menu's options for one combobox.

    react-select ids each option after the input it belongs to, which keeps
    other widgets' hidden lists (the phone country picker) out of the match.
    """
    return f'[id^="react-select-{element_id}-option"], .select__menu {OPTION}'


@dataclass
class FillReport:
    filled: list[str] = field(default_factory=list)
    #: Answers prepared but not placed on the page.
    failed: list[str] = field(default_factory=list)
    #: Required questions with no prepared answer.
    left_for_you: list[str] = field(default_factory=list)


def launch(playwright, *, headless: bool = False):
    """A persistent browser profile, so sign-ins and cookies carry over.

    Prefers the installed Chrome over the bundled Chromium: it is the browser
    the applicant actually uses, and the one a bot check expects to see.
    """
    profile_dir = apply_dir() / "browser"
    profile_dir.mkdir(parents=True, exist_ok=True)
    options = {"headless": headless, "viewport": {"width": 1280, "height": 900}}
    try:
        return playwright.chromium.launch_persistent_context(
            str(profile_dir), channel="chrome", **options
        )
    except Exception:
        return playwright.chromium.launch_persistent_context(str(profile_dir), **options)


def _by_id(page, element_id: str, tag: str = "*"):
    return page.locator(f'{tag}[id="{element_id}"]').first


def _settle(page, ms: int = 250) -> None:
    page.wait_for_timeout(ms)


def _prefix(label: str) -> str:
    return " ".join(re.findall(r"[A-Za-z0-9]+", label)[:2])


def choose(page, element_id: str, wanted, *, pattern: re.Pattern | None = None) -> bool:
    """Open a combobox and click the option matching ``wanted`` (or ``pattern``)."""
    box = _by_id(page, element_id, "input")
    if box.count() == 0:
        return False
    box.scroll_into_view_if_needed()
    target = norm(str(wanted or ""))
    attempts = [""] if pattern is not None else ["", _prefix(str(wanted))]
    for typed in attempts:
        box.click()
        if typed:
            box.fill(typed)
        scoped = _options_for(element_id)
        try:
            page.locator(scoped).first.wait_for(state="visible", timeout=4000)
        except Exception:
            continue
        _settle(page)
        options = page.locator(scoped)
        texts = [norm(t) for t in options.all_inner_texts()]
        index = None
        if pattern is not None:
            index = next((i for i, t in enumerate(texts) if pattern.search(t)), None)
        else:
            index = next((i for i, t in enumerate(texts) if t == target), None)
            if index is None:
                index = next((i for i, t in enumerate(texts) if t.startswith(target)), None)
        if index is not None:
            options.nth(index).click()
            _settle(page)
            return True
    box.press("Escape")
    return False


def _choose_location(page, text: str) -> bool:
    element_id = "candidate-location"
    box = _by_id(page, element_id, "input")
    if box.count() == 0:
        element_id = "location"
        box = _by_id(page, element_id, "input")
    if box.count() == 0:
        return False
    city = text.split(",")[0].strip()
    box.scroll_into_view_if_needed()
    box.click()
    box.fill(city)
    scoped = _options_for(element_id)
    try:
        page.locator(scoped).first.wait_for(state="visible", timeout=6000)
    except Exception:
        return False
    _settle(page, 400)
    options = page.locator(scoped)
    texts = [norm(t) for t in options.all_inner_texts()]
    index = next((i for i, t in enumerate(texts) if norm(city) in t), 0 if texts else None)
    if index is None:
        return False
    options.nth(index).click()
    return True


def _fill_one(page, answer: Answer) -> bool:
    value = answer.value
    if answer.name == "location":
        return _choose_location(page, str(value))
    if answer.type == "file":
        target = page.locator(f'input[type="file"][id="{answer.name}"]').first
        if target.count() == 0 or not Path(str(value)).exists():
            return False
        target.set_input_files(str(value))
        return True
    if answer.type in ("text", "textarea"):
        target = _by_id(page, answer.name)
        if target.count() == 0:
            return False
        target.scroll_into_view_if_needed()
        target.fill(str(value))
        return True
    if answer.type == "select":
        return choose(page, answer.name, value)
    if answer.type == "multiselect":
        picks = value if isinstance(value, list) else [value]
        boxes = page.locator(f'input[type="checkbox"][name="{answer.name}"]')
        if boxes.count():
            ok = True
            for label in picks:
                option_id = answer.option_values.get(label)
                box = _by_id(page, f"{answer.name}_{option_id}", "input")
                if option_id is None or box.count() == 0:
                    ok = False
                    continue
                box.set_checked(True, force=True)
            return ok
        return all(choose(page, answer.name, label) for label in picks)
    return False


def _menu_texts(page, element_id: str) -> list[str]:
    box = _by_id(page, element_id, "input")
    box.scroll_into_view_if_needed()
    box.click()
    scoped = _options_for(element_id)
    try:
        page.locator(scoped).first.wait_for(state="visible", timeout=4000)
    except Exception:
        return []
    _settle(page)
    texts = page.locator(scoped).all_inner_texts()
    box.press("Escape")
    return [t.strip() for t in texts]


def _search_and_pick(page, element_id: str, text: str, timeout_s: float = 8) -> bool:
    """Type into a server-searched combobox and click the exact match.

    The school list is fetched per keystroke, so the menu first shows stale
    options; poll until the wanted one arrives rather than reading once.
    """
    box = _by_id(page, element_id, "input")
    if box.count() == 0:
        return False
    box.scroll_into_view_if_needed()
    box.click()
    box.fill(text)
    target = norm(text)
    options = page.locator(_options_for(element_id))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        texts = [norm(t) for t in options.all_inner_texts()]
        if target in texts:
            options.nth(texts.index(target)).click()
            _settle(page)
            return True
        page.wait_for_timeout(300)
    box.press("Escape")
    return False


def _discipline_option(options: list[str], discipline: str) -> str | None:
    """The fixed-list discipline nearest the applicant's major.

    Greenhouse's list has "Engineering" and "Computer Science" but no
    "Electrical and Computer Engineering": an option the major contains wins,
    then word overlap, then the list's own catch-all.
    """
    want = norm(discipline)
    exact = next((o for o in options if norm(o) == want), None)
    if exact:
        return exact
    inside = [o for o in options if re.search(rf"\b{re.escape(norm(o))}\b", want)]
    if inside:
        return max(inside, key=len)
    return closest_option(options, discipline, floor=0.3) or pick_option(options, r"^other$", r"unknown")


def fill_education(page, profile: ApplicantProfile) -> list[str]:
    """The Education block some boards add outside the question list.

    It is not in the API's questions, but when present its School, Degree and
    Discipline are required: comboboxes over Greenhouse's fixed lists, plus
    free year fields. Returns the fields it could not set.
    """
    if _by_id(page, "school--0", "input").count() == 0:
        return []
    missed = []
    if profile.school and not _search_and_pick(page, "school--0", profile.school):
        if not _search_and_pick(page, "school--0", "Other"):
            missed.append("School")
    if profile.degree:
        word = norm(profile.degree).split("'")[0].split()[0]
        if not choose(page, "degree--0", None, pattern=re.compile(rf"^{re.escape(word)}")):
            missed.append("Degree")
    if profile.discipline and _by_id(page, "discipline--0", "input").count():
        pick = _discipline_option(_menu_texts(page, "discipline--0"), profile.discipline)
        if not (pick and choose(page, "discipline--0", pick)):
            missed.append("Discipline")
    end_year = _by_id(page, "end-year--0", "input")
    if profile.graduation_year and end_year.count():
        end_year.fill(str(profile.graduation_year))
    start_year = _by_id(page, "start-year--0", "input")
    first = profile.education_start_year or (profile.graduation_year and profile.graduation_year - 4)
    if first and start_year.count():
        start_year.fill(str(first))
    return missed


def _decline_remaining_demographics(page) -> list[str]:
    """Decline any demographic dropdown the question list did not describe.

    Newer boards render voluntary self-identification outside the documented
    question list, so the page itself is the only complete source.
    """
    declined = []
    boxes = page.locator('form input[role="combobox"]')
    for i in range(boxes.count()):
        box = boxes.nth(i)
        label = box.evaluate(
            """e => (e.labels && e.labels[0] && e.labels[0].innerText) ||
                    (document.getElementById(e.getAttribute('aria-labelledby') || '') || {}).innerText || ''"""
        )
        element_id = box.get_attribute("id") or ""
        if element_id and DEMOGRAPHIC.search(label or ""):
            if choose(page, element_id, None, pattern=DECLINE):
                declined.append(label.strip())
    return declined


def fill(page, draft: Draft, profile: ApplicantProfile) -> FillReport:
    report = FillReport()
    # One stuck field must cost seconds, not the default thirty per action.
    page.set_default_timeout(10000)
    page.goto(draft.form_url, wait_until="domcontentloaded")
    page.wait_for_selector("form input", timeout=30000)
    _settle(page, 1500)

    # Phone fields carry their own country picker; set it first or the
    # number is validated against the wrong country.
    if profile.country:
        choose(page, "country", profile.country)

    try:
        report.failed.extend(fill_education(page, profile))
    except Exception:
        report.failed.append("Education")

    for answer in draft.answers:
        if not answer.answered:
            if answer.required:
                report.left_for_you.append(answer.label)
            continue
        try:
            ok = _fill_one(page, answer)
        except Exception:
            ok = False
        (report.filled if ok else report.failed).append(answer.label)

    if profile.demographics == "decline":
        try:
            _decline_remaining_demographics(page)
        except Exception:
            pass
    page.evaluate("window.scrollTo(0, 0)")
    return report


def wait_for_submission(page, timeout_s: float = 1800) -> str:
    """'submitted', 'closed' (the applicant skipped it), or 'timeout'."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if page.is_closed():
                return "closed"
            if "confirmation" in page.url.lower():
                return "submitted"
            if CONFIRMED.search(page.locator("body").inner_text(timeout=3000)):
                return "submitted"
            page.wait_for_timeout(1500)
        except Exception:
            if page.is_closed():
                return "closed"
            time.sleep(1.5)
    return "timeout"
