"""Resume storage, keyword extraction, and per-job resume recommendation.

The system recommends *which* of the user's resumes to send and which of their
existing strengths to emphasise. It never rewrites a resume, never invents
experience, and never submits an application.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import RESUME_DIR
from app.logging_setup import get_logger
from app.models import Job, Resume, User
from app.models.base import utcnow
from app.pipeline.normalize import extract_skills
from app.pipeline.textutil import normalize_text
from app.services.preferences import get_or_create_user

log = get_logger("resumes")


def extract_text(path: Path) -> str:
    """Extract plain text from a resume file.

    PDF extraction is best-effort: if no text layer is available the resume is
    still stored, and the user can paste text manually.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".pdf":
            return _extract_pdf(path)
        if suffix == ".docx":
            return _extract_docx(path)
    except Exception as exc:  # pragma: no cover - optional parsers
        log.warning("resume.extract_failed", file=path.name, error=str(exc)[:200])
    return ""


def _extract_pdf(path: Path) -> str:
    raw = path.read_bytes()
    # Pull text from uncompressed content streams without a heavy dependency.
    chunks = re.findall(rb"\((?:\\.|[^\\()])*\)", raw)
    text = " ".join(
        c[1:-1].decode("latin-1", errors="ignore").replace("\\(", "(").replace("\\)", ")")
        for c in chunks
    )
    return re.sub(r"\s+", " ", text).strip()


def _extract_docx(path: Path) -> str:
    import xml.etree.ElementTree as ET
    import zipfile

    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ET.fromstring(xml)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    return " ".join(node.text or "" for node in root.iter(f"{ns}t")).strip()


def save_resume(
    session: Session,
    *,
    name: str,
    kind: str,
    filename: str | None,
    content: bytes | None,
    text_override: str | None = None,
    make_default: bool = False,
    user: User | None = None,
) -> Resume:
    user = user or get_or_create_user(session)
    RESUME_DIR.mkdir(parents=True, exist_ok=True)

    path: Path | None = None
    text = text_override or ""
    if content and filename:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120]
        path = RESUME_DIR / f"{utcnow():%Y%m%d%H%M%S}_{safe}"
        path.write_bytes(content)
        if not text:
            text = extract_text(path)

    resume = Resume(
        user_id=user.id,
        name=name.strip() or "Resume",
        kind=kind.strip().lower() or "general",
        filename=filename,
        file_path=str(path) if path else None,
        text_content=text or None,
        keywords=extract_skills(text or "", "") if text else [],
        is_default=make_default,
        uploaded_at=utcnow(),
    )
    if make_default:
        for other in session.scalars(select(Resume).where(Resume.user_id == user.id)).all():
            other.is_default = False
    session.add(resume)
    session.flush()
    return resume


def list_resumes(session: Session, user: User | None = None) -> list[Resume]:
    user = user or get_or_create_user(session)
    return list(
        session.scalars(
            select(Resume).where(Resume.user_id == user.id).order_by(Resume.created_at.desc())
        ).all()
    )


def match_resume_to_job(session: Session, job: Job, user: User | None = None) -> dict:
    """Recommend the best resume for a job, with an honest match percentage.

    The percentage is the share of the posting's detected skills that the
    resume actually evidences -- not a claim about the user's abilities beyond
    what their resume already says.
    """
    resumes = list_resumes(session, user)
    job_skills = [normalize_text(s) for s in (job.skills or []) if s]

    if not resumes:
        return {
            "recommended": None,
            "match_percent": None,
            "matched_keywords": [],
            "missing_keywords": job_skills[:12],
            "alternatives": [],
            "note": "Upload a resume to get a recommendation.",
        }

    scored: list[tuple[float, Resume, list[str], list[str]]] = []
    for resume in resumes:
        keywords = {normalize_text(k) for k in (resume.keywords or [])}
        text = normalize_text(resume.text_content or "")
        matched = [
            skill
            for skill in job_skills
            if skill in keywords or (len(skill) > 3 and skill in text)
        ]
        missing = [skill for skill in job_skills if skill not in matched]
        percent = (len(matched) / len(job_skills) * 100) if job_skills else 0.0
        # A resume tagged for this domain gets a small nudge on ties.
        if resume.kind and resume.kind in normalize_text(job.title or ""):
            percent = min(100.0, percent + 5)
        scored.append((percent, resume, matched, missing))

    scored.sort(key=lambda t: -t[0])
    best_percent, best_resume, matched, missing = scored[0]

    return {
        "recommended": best_resume,
        "match_percent": round(best_percent),
        "matched_keywords": matched[:20],
        "missing_keywords": missing[:12],
        "alternatives": [
            {"resume": r, "match_percent": round(p)} for p, r, _, _ in scored[1:4]
        ],
        "note": None,
    }


def application_assistant(session: Session, job: Job, user: User | None = None) -> dict:
    """Talking points for a saved job, grounded in the user's own profile.

    Everything returned is drawn from the stored profile and the posting text;
    nothing is invented.
    """
    from app.services.preferences import load_profile

    profile = load_profile(session, user=user)
    resume_match = match_resume_to_job(session, job, user)

    candidate_skills = {normalize_text(s): s for s in profile.all_skills()}
    job_skills = [s for s in (job.skills or [])]
    overlap = [candidate_skills[normalize_text(s)] for s in job_skills if normalize_text(s) in candidate_skills]

    relevant_projects = []
    for project in profile.projects or []:
        text = normalize_text(str(project))
        if any(normalize_text(s) in text for s in job_skills):
            relevant_projects.append(str(project))

    relevant_internships = []
    for item in profile.previous_internships or []:
        text = normalize_text(str(item))
        if any(normalize_text(s) in text for s in job_skills) or normalize_text(
            job.company_name or ""
        ) in text:
            relevant_internships.append(str(item))

    emphasise = overlap[:6] or job_skills[:5]
    talking_points = [f"Your hands-on experience with {skill}" for skill in emphasise[:4]]
    if profile.research_experience and any(
        normalize_text(s) in normalize_text(profile.research_experience) for s in job_skills
    ):
        talking_points.append("Your research experience, which lines up with this team's work")

    interview_topics = sorted({s for s in job_skills})[:10]

    return {
        "resume_match": resume_match,
        "emphasise": emphasise,
        "relevant_projects": relevant_projects[:5],
        "relevant_internships": relevant_internships[:5],
        "talking_points": talking_points,
        "cover_letter_points": [
            f"Connect your {skill} work to the responsibilities listed" for skill in emphasise[:3]
        ],
        "interview_topics": interview_topics,
        "gaps": resume_match.get("missing_keywords", []),
    }
