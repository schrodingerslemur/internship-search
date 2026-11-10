"""Answer an application form's questions, honestly or not at all.

Order of authority, highest first:

1. Field identity -- first_name, email, resume -- straight from the profile.
2. The applicant's standing answers, which override every rule below.
3. Rules for the questions every form asks in its own words: sponsorship,
   work authorization, school, degree, graduation, demographics.
4. A language model, for whatever is left, given only the profile and the
   resume and told to decline rather than guess.

Anything still unanswered and required is left for the applicant and listed,
because a wrong answer sent under their name is worse than a blank they fill
in themselves.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.apply.greenhouse import Question
from app.apply.profile import ApplicantProfile

DECLINE = re.compile(
    r"decline|prefer not|don.?t wish|do not wish|not (?:want|wish) to|choose not|"
    r"rather not|i don.?t want|not to (?:answer|disclose|say)",
    re.IGNORECASE,
)
ACKNOWLEDGE = re.compile(r"acknowledg|confirm|agree|accept|i understand|i have read|^yes\b", re.I)
DEMOGRAPHIC = re.compile(
    r"gender|\brace\b|ethnic|hispanic|latin[oax]|veteran|disabilit|sexual orientation|"
    r"transgender|lgbt|pronoun",
    re.IGNORECASE,
)
MONTHS = (
    "january february march april may june july august september october november december"
).split()


@dataclass
class Answer:
    name: str
    label: str
    type: str
    required: bool
    #: Text, a file path, or for a dropdown the chosen option label(s).
    value: str | list[str] | None = None
    #: profile, standing, rule, ai -- or "" when unanswered.
    source: str = ""
    note: str = ""
    options: list[str] = field(default_factory=list)
    #: Greenhouse option ids by label, which the hosted form uses as checkbox ids.
    option_values: dict[str, str] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.value not in (None, "", [])


@dataclass
class Draft:
    key: str
    canonical_job_id: str | None
    company: str
    title: str
    form_url: str
    prepared_at: str
    answers: list[Answer]

    @property
    def unresolved(self) -> list[Answer]:
        return [a for a in self.answers if a.required and not a.answered]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Draft:
        answers = [Answer(**a) for a in data.pop("answers", [])]
        return cls(**data, answers=answers)


# ---------------------------------------------------------------- options


def norm(text: str) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'").replace("�", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def pick_option(options: list[str], *patterns: str) -> str | None:
    """The first option matching the earliest pattern that matches any.

    Patterns are tried in priority order, so ``pick_option(o, "fall 2027",
    "2027")`` prefers an exact season over any option that merely names the
    year.
    """
    for pattern in patterns:
        rx = re.compile(pattern, re.IGNORECASE)
        for option in options:
            if rx.search(norm(option)):
                return option
    return None


def loose_option(options: list[str], wanted: str) -> str | None:
    """Map a free-text answer onto one of a dropdown's options."""
    target = norm(wanted)
    if not target:
        return None
    for option in options:
        if norm(option) == target:
            return option
    for option in options:
        if norm(option).startswith(target) or target.startswith(norm(option)):
            return option
    for option in options:
        if target in norm(option):
            return option
    return None


def yes_no(options: list[str], value: bool | None) -> str | None:
    if value is None:
        return None
    return pick_option(options, r"^yes\b" if value else r"^no\b")


def graduation_option(options: list[str], profile: ApplicantProfile) -> str | None:
    year, month = profile.graduation_year, profile.graduation_month
    if not year:
        return None
    patterns: list[str] = []
    if month:
        name = MONTHS[month - 1]
        patterns += [rf"{name[:3]}\w*\.? *(?:\d+, *)?{year}", rf"\b{month:02d}/{year}"]
        season = (
            ["spring", "winter"] if month <= 5 else ["summer"] if month <= 8 else ["fall", "autumn", "winter"]
        )
        patterns += [rf"\b{s}\b.*{year}" for s in season]
    patterns.append(rf"^{year}$")
    return pick_option(options, *patterns)


# ---------------------------------------------------------------- rules


_FILLER = {"and", "of", "in", "the", "with"}


def closest_option(options: list[str], wanted: str, floor: float = 0.5) -> str | None:
    """The option sharing the most words with ``wanted`` (Jaccard), if close enough.

    For long institutional lists that never carry the applicant's exact
    wording: "Electrical and Computer Engineering" lands on "Computer
    Engineering" rather than nothing.
    """

    def words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]+", norm(text).lower()) if w not in _FILLER}

    target = words(wanted)
    if not target:
        return None
    best, best_score = None, 0.0
    for option in options:
        have = words(option)
        if not have:
            continue
        score = len(target & have) / len(target | have)
        if score > best_score:
            best, best_score = option, score
    return best if best_score >= floor else None


def _text_or_option(q: Question, text: str | None) -> str | list[str] | None:
    if text in (None, ""):
        return None
    if q.type in ("select", "multiselect"):
        chosen = loose_option(q.option_labels, text)
        if chosen is None:
            return None
        return [chosen] if q.type == "multiselect" else chosen
    return text


def _choice(q: Question, option: str | None) -> str | list[str] | None:
    if option is None:
        return None
    return [option] if q.type == "multiselect" else option


def rule_answer(q: Question, p: ApplicantProfile, company: str = "") -> tuple[Any, str]:
    """(value, note) for a question the rules know, else (None, "")."""
    label = norm(q.label)
    options = q.option_labels
    is_choice = q.type in ("select", "multiselect")

    if q.group == "compliance" or DEMOGRAPHIC.search(label):
        if p.demographics == "decline" and is_choice:
            return _choice(q, pick_option(options, DECLINE.pattern)), "declined"
        return None, ""

    if "sponsor" in label:
        return (_choice(q, yes_no(options, p.requires_sponsorship)) if is_choice
                else ("Yes" if p.requires_sponsorship else "No")), ""
    if re.search(r"authori[sz]ed|eligible to work|legally (?:able|permitted|allowed)|right to work", label):
        return (_choice(q, yes_no(options, p.work_authorized_us)) if is_choice
                else ("Yes" if p.work_authorized_us else "No")), ""
    if re.search(r"citizen|permanent resident|green card|u\.?s\.? person", label) and is_choice:
        return _choice(q, yes_no(options, p.us_citizen)), ""
    if "relocat" in label and p.willing_to_relocate is not None:
        return (_choice(q, yes_no(options, p.willing_to_relocate)) if is_choice
                else ("Yes" if p.willing_to_relocate else "No")), ""
    # "This role is onsite 5 days a week in San Jose -- can you meet that?"
    if is_choice and p.willing_to_relocate and re.search(
        r"\bon-?site\b|\bin[- ](?:the )?office\b|\bin[- ]person\b", label
    ):
        return _choice(q, yes_no(options, True)), "onsite"

    if "linkedin" in label:
        return _text_or_option(q, p.linkedin), ""
    if "github" in label:
        return _text_or_option(q, p.github), ""
    if re.search(r"website|portfolio|personal (?:site|url|page)", label):
        return _text_or_option(q, p.website or p.github), ""

    if re.search(r"legal first name|first name \(legal\)", label) and not is_choice:
        return p.first_name, ""
    if re.search(r"legal last name|last name \(legal\)|legal surname", label) and not is_choice:
        return p.last_name, ""
    if re.search(r"full (?:legal )?name|signature|type your name", label) and not is_choice:
        return p.full_name, ""
    if re.search(r"preferred (?:first )?name", label) and not is_choice:
        return p.first_name, ""

    if re.search(r"school|universit|college|institution", label) and "high school" not in label:
        if is_choice:
            return _choice(q, pick_option(options, re.escape(norm(p.school)))), ""
        return p.school, ""
    if re.search(r"\bdegree\b", label) and not re.search(r"graduat|complet", label):
        if is_choice:
            word = norm(p.degree).split("'")[0].split()[0] if p.degree else ""
            return _choice(q, pick_option(options, rf"^{re.escape(word)}")), ""
        return p.degree, ""
    if re.search(r"graduat", label):
        if is_choice:
            return _choice(q, graduation_option(options, p)), "check the term"
        return p.graduation, ""
    if re.search(r"\bmajor\b|discipline|field of study|area of study|concentration", label):
        value = _text_or_option(q, p.discipline)
        if value is None and is_choice:
            closest = closest_option(options, p.discipline)
            if closest is not None:
                return _choice(q, closest), "closest option to your major"
        return value, ""

    if re.search(r"start date|available to start|earliest.*start|when can you start", label) and not is_choice:
        return p.earliest_start or None, ""

    if re.search(r"privacy|notice at collection|data protection|gdpr|acknowledg|consent|certif|attest", label):
        if is_choice:
            return _choice(q, pick_option(options, ACKNOWLEDGE.pattern)), "acknowledgement"
        return None, ""

    if re.search(r"how did you (?:hear|find|learn)|where did you (?:hear|find|learn|see)|hear about", label):
        if is_choice:
            return _choice(q, pick_option(
                options, r"career|company (?:web)?site|website", r"job board|online|internet", r"^other"
            )), ""
        return f"{company} careers page" if company else "Company careers page", ""

    return None, ""


FIELD_IDENTITY = {
    "first_name": lambda p: p.first_name,
    "last_name": lambda p: p.last_name,
    "email": lambda p: p.email,
    "phone": lambda p: p.phone,
    "location": lambda p: p.location,
}

#: Optional questions worth a model's effort: the "why us" prompts that a
#: reader actually weighs. Everything else optional is left blank.
WORTH_WRITING = re.compile(r"\bwhy\b|interest|excite|motivat|what draws", re.IGNORECASE)


def prepare_answers(
    questions: list[Question],
    profile: ApplicantProfile,
    *,
    company: str = "",
    resume: str = "",
    posting: str = "",
    llm: Any = None,
) -> list[Answer]:
    answers: list[Answer] = []
    for q in questions:
        a = Answer(
            name=q.name,
            label=q.label,
            type=q.type,
            required=q.required,
            options=q.option_labels,
            option_values={o.label: o.value for o in q.options},
        )
        answers.append(a)

        if q.name in FIELD_IDENTITY:
            a.value, a.source = FIELD_IDENTITY[q.name](profile) or None, "profile"
            continue
        if q.name == "resume":
            path = profile.resume_file()
            a.value, a.source = (str(path) if path else None), "profile"
            continue
        if q.name == "cover_letter":
            continue  # optional almost everywhere; never invented

        standing = next((s for s in profile.standing_answers if s.applies_to(q.label)), None)
        if standing is not None:
            value = _text_or_option(q, standing.answer)
            if value not in (None, "", []):
                a.value, a.source = value, "standing"
                continue

        value, note = rule_answer(q, profile, company)
        if value not in (None, "", []):
            a.value, a.source, a.note = value, "rule", note

    pending = [
        a
        for a in answers
        if not a.answered
        and a.type != "file"
        and a.name not in FIELD_IDENTITY
        and (a.required or (a.type in ("text", "textarea") and WORTH_WRITING.search(a.label)))
    ]
    if pending and llm is not None:
        _ask_model(pending, profile, company, resume, posting, llm)
    return answers


def _ask_model(
    pending: list[Answer],
    profile: ApplicantProfile,
    company: str,
    resume: str,
    posting: str,
    llm: Any,
) -> None:
    replies = llm.answer_application_questions(
        facts=profile.facts_text(),
        resume=resume,
        posting=f"{company}\n{posting}",
        questions=[
            {"name": a.name, "label": a.label, "type": a.type, "options": a.options}
            for a in pending
        ],
    )
    for a in pending:
        reply = (replies or {}).get(a.name) or {}
        if not reply.get("confident"):
            continue
        raw = reply.get("answer")
        if a.type == "select":
            value = loose_option(a.options, str(raw or ""))
        elif a.type == "multiselect":
            picks = raw if isinstance(raw, list) else [raw]
            value = [o for o in (loose_option(a.options, str(x or "")) for x in picks) if o] or None
        else:
            value = str(raw or "").strip() or None
        if value:
            a.value, a.source = value, "ai"


def build_draft(
    *,
    key: str,
    canonical_job_id: str | None,
    company: str,
    title: str,
    form_url: str,
    answers: list[Answer],
    now: datetime | None = None,
) -> Draft:
    return Draft(
        key=key,
        canonical_job_id=canonical_job_id,
        company=company,
        title=title,
        form_url=form_url,
        prepared_at=(now or datetime.now()).isoformat(timespec="seconds"),
        answers=answers,
    )
