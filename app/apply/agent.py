"""The general agent: apply on a form nobody wrote code for.

Greenhouse has its own filler because its questions come from a public API.
Every other format -- Lever, Ashby, Workday, a company's own careers page --
goes through this loop instead:

1. Read the page (and every iframe on it) into a numbered list of the
   fields, buttons and links a person could use, plus the visible text.
2. Ask the model what to do next, given the applicant's facts and resume,
   what it has done so far, and the how-to written on earlier forms of this
   same format (``ApplyRecipe``).
3. Do it with Playwright, note what worked and what failed, and repeat until
   the site confirms the application, or the model says it cannot go on.

Every attempt ends with the model rewriting that format's how-to, so the
second Workday form starts from what the first one taught it.

The same honesty rules as the Greenhouse path apply: facts come only from the
profile and resume, and a fact it does not have comes back to the applicant
as a question instead of a guess.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.apply.answers import looks_like_placeholder
from app.logging_setup import get_logger

log = get_logger("autoapply.agent")

#: Model turns per application. A long multi-page form is about fifteen.
MAX_STEPS = 32
#: Actions the model may batch into one turn.
MAX_ACTIONS = 30
#: Steps of history shown back to the model.
HISTORY = 14

CONFIRMED = re.compile(
    r"thank you for (?:applying|your application|your interest|submitting)|application (?:has been |was )?"
    r"(?:submitted|received|complete)|we.?ve received your application|successfully (?:submitted|applied)|"
    r"you(?:'ve| have) (?:successfully )?applied",
    re.IGNORECASE,
)
CAPTCHA_FRAME = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare|captcha-delivery|datadome", re.I)

AGENT_SYSTEM = """\
You are applying for a job on behalf of a candidate, inside a real web browser,
on an application site you may not have seen before. The candidate approved
this specific application and asked you to complete and submit it.

Each turn you get the page as a list of elements, each with an id like e12 or
f1.e7 (an element inside an iframe), and the page's visible text. Reply with
the next actions. Batch as many as you safely can: fill every visible field in
one turn, then click Next/Submit in the next turn once you have seen they took.

Actions (JSON objects):
  {"do": "fill", "id": "e3", "value": "text"}        plain text field / textarea
  {"do": "type", "id": "e3", "value": "Pitts"}       autocomplete/combobox: types slowly so
                                                     suggestions appear; pick one next turn
  {"do": "select", "id": "e4", "value": "Yes"}       native <select>: option text
  {"do": "check", "id": "e5"} / {"do": "uncheck", "id": "e5"}   checkbox, radio, switch
  {"do": "click", "id": "e6"}                        buttons, links, options, custom radios
  {"do": "upload", "id": "e7", "file": "resume"}     file input or its upload button; file is
                                                     one of the files listed as available
  {"do": "press", "key": "Enter"}                    keyboard
  {"do": "goto", "url": "https://..."}               only for an Apply link on the same site
  {"do": "wait", "ms": 2000}

Rules:
- FACTS (contact details, education, dates, GPA, grades, work history,
  authorization, sponsorship, test scores, anything about the candidate's
  life) come ONLY from the candidate facts, standing answers and resume.
  Never invent one. If a REQUIRED field needs a fact you do not have, stop
  with status "needs_you" and list it in "questions" (exact label, type,
  options if any).
- CHOICES you make yourself: preferred location (US, nearest the candidate or
  the posting), team, internship length (a standard full summer), programming
  language (the resume's main one), how they heard (company careers site).
- AGREE to acknowledgements, privacy notices, consent and attestation boxes
  needed to apply. Answer No to prior employment there, relatives there,
  criminal record, clearance, non-competes, competing offers, unless facts say
  otherwise. Decline every voluntary demographic question (gender, race,
  veteran, disability) with its "decline / prefer not to say" option.
- "Why us / why this role" free text: 2-4 plain first-person sentences tying
  specific resume items to the posting. No cliches, nothing the resume lacks.
- Upload the resume wherever one is asked for (choose "upload a file" over
  "apply with LinkedIn/Indeed"). Upload the transcript wherever one is asked.
  A cover letter only where it is required and one is available.
- Prefer "Apply", "Apply manually", "Apply as guest", "Continue without
  account". Do not use "Apply with LinkedIn/Indeed/Google".
- Never pay, never subscribe to anything, never change site settings.
- If the site requires signing in or creating an account and offers no guest
  route, stop with "needs_you" and say so. Do not create accounts.
- If you see a CAPTCHA, a bot check, or "access denied", stop with "failed"
  (bot protection) unless it is a simple checkbox you can tick.
- If the site emails a verification / security code, stop with "need_code";
  the code will be given to you next turn and you then enter it.
- When you see the site's confirmation that the application was received,
  reply "submitted" with "evidence": the exact confirmation sentence from
  the page text. Never claim submitted without it.
- An action result starting with "error" means it did not happen; try
  another element or approach rather than repeating it.

Reply with JSON only:
{"thought": "one short sentence on where you are",
 "actions": [ ... ],
 "status": "continue" | "submitted" | "need_code" | "needs_you" | "failed" | "ready",
 "reason": "for any status but continue: one plain sentence for the candidate",
 "evidence": "confirmation sentence, when submitted",
 "questions": [{"label": "...", "type": "text|select|file", "options": ["..."]}],
 "filled": [{"label": "question", "value": "what you entered"}],
 "lessons": "for any status but continue: the COMPLETE updated how-to for applying on this
             site/format, as short numbered steps a future you can follow on a different
             job here -- page sequence, which buttons, widget quirks (how dropdowns or
             uploads behave), what blocked you. Nothing about this candidate or job."}
"filled" lists every answer you entered this turn (cumulative across turns is
fine). Status "ready" is only for a dry run, when the one remaining step is the
final submit.
"""


# --------------------------------------------------------------- page model

OBSERVE_JS = r"""
() => {
  window.__aaN = window.__aaN || 0;
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => {
    if (e.type === 'file') return true;
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const byIds = ids => ids.split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
  const labelOf = e => {
    let t = '';
    if (e.labels && e.labels.length) t = [...e.labels].map(l => l.innerText).join(' ');
    if (!txt(t) && e.getAttribute('aria-labelledby')) t = byIds(e.getAttribute('aria-labelledby'));
    if (!txt(t)) t = e.getAttribute('aria-label') || '';
    const tag = e.tagName;
    if (!txt(t) && (tag === 'BUTTON' || tag === 'A' || e.getAttribute('role'))) t = e.innerText || e.value || '';
    if (!txt(t)) t = e.getAttribute('placeholder') || e.getAttribute('title') || '';
    if (!txt(t)) { const f = e.closest('fieldset'); const lg = f && f.querySelector('legend'); if (lg) t = lg.innerText; }
    for (let p = e.parentElement, i = 0; !txt(t) && p && i < 3; p = p.parentElement, i++) t = (p.innerText || '').slice(0, 160);
    if (!txt(t)) t = e.name || e.id || '';
    return txt(t).slice(0, 160);
  };
  const describe = e => {
    if (!e.dataset.aa) e.dataset.aa = 'e' + (++window.__aaN);
    const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
    const it = {id: e.dataset.aa, tag, type, role: e.getAttribute('role') || '', label: labelOf(e)};
    if (e.required || e.getAttribute('aria-required') === 'true') it.required = true;
    if (tag === 'select') {
      it.options = [...e.options].map(o => txt(o.text)).filter(Boolean).slice(0, 80);
      it.value = txt(e.selectedOptions[0] && e.selectedOptions[0].text);
    } else if (type === 'checkbox' || type === 'radio') it.checked = e.checked;
    else if (type === 'file') it.files = [...(e.files || [])].map(f => f.name);
    else if (tag === 'input' || tag === 'textarea') it.value = (e.value || '').slice(0, 80);
    if (e.getAttribute('aria-checked')) it.checked = e.getAttribute('aria-checked') === 'true';
    if (e.getAttribute('aria-invalid') === 'true') it.invalid = true;
    if (tag === 'a') it.href = (e.getAttribute('href') || '').slice(0, 120);
    return it;
  };
  const controls = 'input:not([type=hidden]), textarea, select, button, [role=button], [role=combobox], ' +
    '[role=option], [role=checkbox], [role=radio], [role=switch], [role=menuitem], [role=tab], ' +
    '[contenteditable=true]';
  const items = [];
  for (const e of document.querySelectorAll(controls)) {
    if (items.length >= 200) break;
    if (vis(e) && !e.disabled) items.push(describe(e));
  }
  let links = 0;
  for (const e of document.querySelectorAll('a[href]')) {
    if (links >= 40) break;
    if (vis(e) && txt(e.innerText)) { items.push(describe(e)); links++; }
  }
  const errors = [...document.querySelectorAll('[role=alert], [aria-live=assertive], .error, .error-message, [class*="error" i]')]
    .filter(vis).map(e => txt(e.innerText)).filter(t => t && t.length < 200).slice(0, 8);
  return {title: document.title, url: location.href,
          text: txt(document.body ? document.body.innerText : '').slice(0, 4500), items, errors};
}
"""


@dataclass
class Observation:
    url: str
    title: str
    text: str
    items: list[dict]
    errors: list[str]
    captcha: bool = False

    def render(self) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if self.captcha:
            lines.append("NOTE: a CAPTCHA / bot-check frame is present on this page.")
        if self.errors:
            lines.append("Errors shown: " + " | ".join(self.errors))
        lines.append("Elements:")
        for it in self.items:
            kind = it["tag"] + (f"[{it['type']}]" if it.get("type") else "") + (
                f"(role={it['role']})" if it.get("role") else ""
            )
            bits = [it["id"], kind, json.dumps(it.get("label", ""), ensure_ascii=False)]
            if it.get("required"):
                bits.append("REQUIRED")
            if "value" in it and it["value"]:
                bits.append(f"value={json.dumps(it['value'], ensure_ascii=False)}")
            if "checked" in it:
                bits.append("checked" if it["checked"] else "unchecked")
            if it.get("files"):
                bits.append(f"files={it['files']}")
            if it.get("invalid"):
                bits.append("INVALID")
            if it.get("options"):
                bits.append("options=" + json.dumps(it["options"], ensure_ascii=False))
            if it.get("href"):
                bits.append(f"href={it['href']}")
            lines.append("  " + " ".join(bits))
        lines.append("Visible text:\n" + self.text)
        return "\n".join(lines)


@dataclass
class AgentResult:
    status: str  # submitted, ready, needs_you, failed
    detail: str
    lessons: str = ""
    questions: list[dict] = field(default_factory=list)
    filled: list[dict] = field(default_factory=list)
    steps: int = 0


class WebAgent:
    def __init__(
        self,
        page,
        brain,
        *,
        briefing: str,
        files: dict[str, str],
        recipe: str = "",
        on_code=None,
        dry_run: bool = False,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.page = page
        self.brain = brain
        self.briefing = briefing
        #: kind -> local path: resume, transcript, cover_letter, ...
        self.files = {k: v for k, v in files.items() if v and Path(v).exists()}
        self.recipe = recipe
        self.on_code = on_code
        self.dry_run = dry_run
        self.max_steps = max_steps
        self.history: list[str] = []
        self.frames: dict[str, object] = {}
        self.filled: dict[str, str] = {}
        self.extra: list[str] = []

    # ------------------------------------------------------------ observe

    def observe(self) -> Observation:
        page = self.page
        self.frames = {}
        main = page.main_frame
        obs = Observation(url=page.url, title="", text="", items=[], errors=[])
        n = 0
        for frame in page.frames:
            url = frame.url or ""
            if CAPTCHA_FRAME.search(url):
                obs.captcha = True
                continue
            if frame is not main and (not url or url == "about:blank"):
                continue
            try:
                data = frame.evaluate(OBSERVE_JS)
            except Exception:
                continue
            prefix = "" if frame is main else f"f{(n := n + 1)}."
            self.frames[prefix] = frame
            for it in data["items"]:
                it["id"] = prefix + it["id"]
            if frame is main:
                obs.title, obs.text = data["title"], data["text"]
            elif data["items"] or data["text"]:
                obs.text += f"\n[iframe {prefix[:-1]}: {url[:80]}] " + data["text"][:2500]
            obs.items.extend(data["items"])
            obs.errors.extend(data["errors"])
        return obs

    def _locate(self, element_id: str):
        prefix, _, local = element_id.rpartition(".")
        frame = self.frames.get(prefix + "." if prefix else "")
        if frame is None:
            raise LookupError(f"no frame for {element_id}")
        return frame.locator(f'[data-aa="{local}"]').first

    # ---------------------------------------------------------------- act

    def act(self, action: dict) -> str:
        do = str(action.get("do") or "")
        page = self.page
        try:
            if do == "press":
                page.keyboard.press(str(action.get("key") or "Enter"))
                return "ok"
            if do == "wait":
                page.wait_for_timeout(min(int(action.get("ms") or 1500), 8000))
                return "ok"
            if do == "goto":
                url = str(action.get("url") or "")
                if not url.startswith("http"):
                    return "error: goto needs an absolute URL"
                page.goto(url, wait_until="domcontentloaded")
                return "ok"
            if do in ("fill", "type") and looks_like_placeholder(action.get("value")):
                return (
                    "error: not entered -- that reads like an instruction or placeholder, not an "
                    "answer. Write the real answer from the facts, or if a fact is missing put the "
                    "question in \"questions\" for the candidate."
                )
            el = self._locate(str(action.get("id") or ""))
            if el.count() == 0:
                return "error: element no longer on the page"
            value = action.get("value")
            if do == "fill":
                try:
                    el.fill(str(value or ""))
                except Exception:
                    el.click()
                    page.keyboard.press("Control+A")
                    page.keyboard.type(str(value or ""))
                return "ok"
            if do == "type":
                el.click()
                el.fill("")
                el.press_sequentially(str(value or ""), delay=60)
                page.wait_for_timeout(1200)
                return "ok"
            if do == "select":
                return self._select(el, str(value or ""))
            if do in ("check", "uncheck"):
                try:
                    el.set_checked(do == "check", force=True)
                except Exception:
                    el.click(force=True)
                return "ok"
            if do == "click":
                try:
                    el.click(timeout=8000)
                except Exception:
                    el.click(force=True, timeout=5000)
                return "ok"
            if do == "upload":
                return self._upload(el, str(action.get("file") or "resume"))
            return f"error: unknown action {do!r}"
        except Exception as exc:
            return f"error: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}"

    def _select(self, el, value: str) -> str:
        for how in ({"label": value}, {"value": value}):
            try:
                el.select_option(**how, timeout=4000)
                return "ok"
            except Exception:
                pass
        options = el.evaluate("e => [...e.options].map(o => o.text.trim())")
        want = value.lower()
        pick = next((o for o in options if o.lower().startswith(want) or want in o.lower()), None)
        if pick is None:
            return f"error: no option like {value!r}"
        el.select_option(label=pick)
        return f"ok (picked {pick!r})"

    def _upload(self, el, kind: str) -> str:
        path = self.files.get(kind)
        if path is None:
            return f"error: no {kind} file available (have: {', '.join(self.files) or 'none'})"
        is_file = el.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'")
        if is_file:
            el.set_input_files(path)
        else:
            with self.page.expect_file_chooser(timeout=8000) as chooser:
                el.click()
            chooser.value.set_files(path)
        self.page.wait_for_timeout(1500)
        return f"ok (attached {Path(path).name})"

    # ---------------------------------------------------------------- run

    def _prompt(self, obs: Observation) -> str:
        files = ", ".join(f"{k} ({Path(v).name})" for k, v in self.files.items()) or "none"
        parts = [
            self.briefing,
            f"Files available to upload: {files}",
            "How-to for this site, written on earlier applications here:\n"
            + (self.recipe.strip() or "(none yet: this is the first application on this format)"),
        ]
        if self.dry_run:
            parts.append(
                "DRY RUN: fill everything, but do NOT click the final submit. When only the "
                "final submit remains, reply status \"ready\"."
            )
        if self.extra:
            parts.append("New information:\n" + "\n".join(self.extra))
        if self.history:
            parts.append("What you did so far (oldest first):\n" + "\n".join(self.history[-HISTORY:]))
        parts.append("Current page:\n" + obs.render())
        return "\n\n".join(parts)

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        self.page.wait_for_timeout(1200)

    def run(self, url: str) -> AgentResult:
        page = self.page
        page.set_default_timeout(10000)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self._settle()
        silent = unconfirmed = codes = 0
        last = None
        for step in range(1, self.max_steps + 1):
            obs = self.observe()
            reply = self.brain(AGENT_SYSTEM, self._prompt(obs))
            if not isinstance(reply, dict):
                silent += 1
                if silent >= 2:
                    return AgentResult("needs_you", "The agent's model did not answer.", steps=step)
                continue
            silent = 0
            last = reply
            for item in reply.get("filled") or []:
                if isinstance(item, dict) and item.get("label") and not looks_like_placeholder(item.get("value")):
                    self.filled[str(item["label"])[:200]] = str(item.get("value"))[:500]
            status = str(reply.get("status") or "continue")
            results = []
            for action in (reply.get("actions") or [])[:MAX_ACTIONS]:
                if not isinstance(action, dict):
                    continue
                if self.dry_run and status in ("ready",):
                    break
                outcome = self.act(action)
                results.append(f"{_brief(action)} -> {outcome}")
            if results:
                self._settle()
            thought = str(reply.get("thought") or "")[:200]
            self.history.append(f"[{step}] {thought}\n    " + "\n    ".join(results or ["(no actions)"]))
            log.info("agent.step", step=step, status=status, actions=len(results))

            if status == "continue":
                continue
            if status == "submitted":
                if self._confirmed(str(reply.get("evidence") or "")):
                    return self._result("submitted", reply, "Submitted; the site confirmed it.", step)
                unconfirmed += 1
                if unconfirmed >= 3:
                    return self._result("needs_you", reply, "It looked submitted, but no confirmation appeared.", step)
                self.extra.append("The page shows no confirmation yet; check for errors or a remaining step.")
                continue
            if status == "need_code":
                if codes >= 3:
                    return self._result("needs_you", reply, "The site did not accept the codes entered.", step)
                code = self.on_code(codes > 0) if self.on_code else None
                codes += 1
                if not code:
                    return self._result(
                        "needs_you", reply, "The site emailed a verification code and none was entered in time.", step
                    )
                self.extra.append(f"The verification code just emailed to the candidate is {code}. Enter it now.")
                continue
            if status == "ready":
                return self._result("ready", reply, "Dry run: filled in, not submitted.", step)
            if status in ("needs_you", "failed"):
                return self._result(status, reply, str(reply.get("reason") or "The agent stopped."), step)
            self.history.append(f"    (unknown status {status!r}; continuing)")
        return self._result("needs_you", last or {}, f"Gave up after {self.max_steps} steps.", self.max_steps)

    def _confirmed(self, evidence: str) -> bool:
        try:
            text = " ".join(
                f.locator("body").inner_text(timeout=3000) for f in self.frames.values()
            )
        except Exception:
            text = ""
        text = " ".join(text.split())
        if CONFIRMED.search(text) or "confirmation" in self.page.url.lower():
            return True
        evidence = " ".join(evidence.split())
        return len(evidence) >= 12 and evidence.lower() in text.lower()

    def _result(self, status: str, reply: dict, detail: str, steps: int) -> AgentResult:
        if status in ("needs_you", "failed") and reply.get("reason"):
            detail = str(reply["reason"])[:300]
        questions = [
            {
                "label": str(q.get("label"))[:300],
                "type": q.get("type") if q.get("type") in ("text", "select", "file", "textarea") else "text",
                "options": [str(o) for o in (q.get("options") or [])][:100],
            }
            for q in reply.get("questions") or []
            if isinstance(q, dict) and q.get("label")
        ] if status == "needs_you" else []
        return AgentResult(
            status=status,
            detail=detail,
            lessons=str(reply.get("lessons") or "").strip()[:6000],
            questions=questions,
            filled=[{"label": k, "value": v} for k, v in self.filled.items()],
            steps=steps,
        )


def _brief(action: dict) -> str:
    """An action as one short line for the model's history."""
    keep = {k: (str(v)[:60] if k == "value" else v) for k, v in action.items()}
    return json.dumps(keep, ensure_ascii=False)


def readable_standing(profile) -> str:
    """Standing answers as "question -> answer" lines a model reads easily."""
    lines = []
    for s in profile.standing_answers:
        q = re.sub(r"^\^\\s\*|\\s\*\$$", "", s.match)
        q = re.sub(r"\\(.)", r"\1", q)
        lines.append(f"- {q} -> {s.answer}")
    return "\n".join(lines)


def briefing(profile, *, resume: str, company: str, title: str, posting: str) -> str:
    """Everything about the candidate and the job, once per application."""
    facts = profile.facts_text().split("\nStanding answer")[0]
    standing = readable_standing(profile)
    return "\n\n".join(
        p
        for p in (
            f"Job: {title} at {company}",
            "Candidate facts:\n" + facts,
            ("Standing answers the candidate gave (use verbatim for these questions):\n" + standing)
            if standing else "",
            "Resume:\n" + (resume or "(no text)")[:7000],
            "Posting (excerpt):\n" + (posting or "(not available)")[:2500],
        )
        if p
    )
