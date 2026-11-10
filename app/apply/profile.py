"""The applicant: who is applying, and the standing answers they have given.

Kept in ``data/apply/profile.json`` rather than the database. The database is
shared with the hosted dashboard and the public CI runner; a phone number and
a resume have no business in either.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import app.config as config


def apply_dir() -> Path:
    # Read through the module so tests that repoint DATA_DIR are honoured.
    return config.DATA_DIR / "apply"


def profile_path() -> Path:
    return apply_dir() / "profile.json"


@dataclass
class StandingAnswer:
    """A reusable answer to a question the profile cannot otherwise settle.

    ``match`` is a case-insensitive regular expression tested against the
    question's label. ``answer`` is the text to enter, or for a dropdown the
    option to pick (matched loosely, so "No" picks "No, I do not").
    """

    match: str
    answer: str

    def applies_to(self, label: str) -> bool:
        try:
            return re.search(self.match, label, re.IGNORECASE) is not None
        except re.error:
            return False


@dataclass
class ApplicantProfile:
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    country: str = "United States"
    #: City as typed into a location autocomplete, e.g. "Pittsburgh, PA".
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""

    school: str = ""
    degree: str = "Bachelor's"
    discipline: str = ""
    #: Human-readable, e.g. "December 2027".
    graduation: str = ""
    graduation_month: int | None = None
    graduation_year: int | None = None

    us_citizen: bool | None = None
    work_authorized_us: bool | None = None
    requires_sponsorship: bool | None = None
    willing_to_relocate: bool | None = None

    terms: list[str] = field(default_factory=list)
    earliest_start: str = ""

    #: "decline" answers every demographic question with its decline option.
    demographics: str = "decline"

    resume_path: str = ""
    daily_cap: int = 5
    #: Company names (case-insensitive substrings) never to prepare.
    never_apply_to: list[str] = field(default_factory=list)
    standing_answers: list[StandingAnswer] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def resume_file(self) -> Path | None:
        if not self.resume_path:
            return None
        path = Path(self.resume_path)
        if not path.is_absolute():
            path = config.PROJECT_ROOT / path
        return path if path.exists() else None

    def excluded(self, company: str | None) -> bool:
        name = (company or "").lower()
        return any(block.lower() in name for block in self.never_apply_to if block.strip())

    def facts_text(self) -> str:
        """The profile as plain statements, for a model to answer from."""
        yes_no = {True: "yes", False: "no", None: "unknown"}
        lines = [
            f"Name: {self.full_name}",
            f"Email: {self.email}",
            f"Phone: {self.phone}",
            f"Current location: {self.location}",
            f"LinkedIn: {self.linkedin}",
            f"GitHub: {self.github}",
            f"School: {self.school}",
            f"Degree currently pursuing: {self.degree} in {self.discipline}",
            f"Expected graduation: {self.graduation}",
            f"US citizen: {yes_no[self.us_citizen]}",
            f"Legally authorized to work in the US: {yes_no[self.work_authorized_us]}",
            f"Will require visa sponsorship now or in future: {yes_no[self.requires_sponsorship]}",
            f"Willing to relocate: {yes_no[self.willing_to_relocate]}",
            f"Internship terms sought: {', '.join(self.terms) or 'unknown'}",
            f"Earliest start date: {self.earliest_start or 'unknown'}",
        ]
        for standing in self.standing_answers:
            lines.append(f"Standing answer for questions like /{standing.match}/: {standing.answer}")
        return "\n".join(lines)

    def problems(self) -> list[str]:
        """What is missing before an application can be filled at all."""
        out = []
        for name in ("first_name", "last_name", "email", "phone"):
            if not getattr(self, name):
                out.append(f"{name} is empty")
        if self.resume_file() is None:
            out.append(f"resume not found at {self.resume_path or '(unset)'}")
        return out


def load_profile(path: Path | None = None) -> ApplicantProfile:
    path = path or profile_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No applicant profile at {path}. Copy app/apply/profile.example.json "
            "there and fill it in."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    standing = [StandingAnswer(**item) for item in raw.pop("standing_answers", []) or []]
    known = set(ApplicantProfile.__dataclass_fields__)
    profile = ApplicantProfile(**{k: v for k, v in raw.items() if k in known})
    profile.standing_answers = standing
    return profile


def save_profile(profile: ApplicantProfile, path: Path | None = None) -> Path:
    path = path or profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(profile), indent=2), encoding="utf-8")
    return path


def resume_text(profile: ApplicantProfile) -> str:
    """Plain text of the resume, cached beside the profile.

    PDF text layers come out one word per line; collapsed to single spaces it
    reads fine to a model, which is the only consumer.
    """
    resume = profile.resume_file()
    if resume is None:
        return ""
    cache = apply_dir() / "resume.txt"
    if cache.exists() and cache.stat().st_mtime >= resume.stat().st_mtime:
        return cache.read_text(encoding="utf-8")

    if resume.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        text = " ".join(page.extract_text() or "" for page in PdfReader(str(resume)).pages)
    else:
        from app.services.resumes import extract_text

        text = extract_text(resume)
    text = re.sub(r"\s+", " ", text).strip()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text
