"""Optional LLM stages: duplicate adjudication and semantic relevance.

Both stages are strictly additive. With no API key, or with ``LLM_ENABLED``
false, the pipeline runs entirely on deterministic logic and loses no
functionality -- it simply resolves fewer ambiguous duplicate pairs and skips
the semantic second opinion.

The hard rule for both prompts: **never invent facts.** If the posting does not
state something, the answer is ``unknown``. A model that guesses "sponsorship
available" is worse than no model at all, so the prompts forbid inference and
the parsers reject anything outside the permitted vocabulary.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger
from app.pipeline.textutil import truncate
from app.schemas.job import NormalizedJob

log = get_logger("llm")

DEDUP_SYSTEM = (
    "You compare two job postings and decide whether they describe the SAME "
    "underlying open position. Different locations, different requisition IDs, "
    "different teams, or different specialisations (for example verification "
    "versus design) mean DIFFERENT. The same role advertised on two websites "
    "means SAME. If you cannot tell, answer UNCERTAIN. Never guess. "
    'Reply with JSON only: {"verdict": "SAME|DIFFERENT|UNCERTAIN", '
    '"confidence": 0.0-1.0, "reason": "one short sentence"}'
)

CLASSIFY_SYSTEM = (
    "You assess how well a job posting fits a candidate. Use ONLY facts stated "
    "in the posting. If the posting does not mention something, report it as "
    "unknown -- never infer it. In particular, if visa sponsorship is not "
    "mentioned, sponsorship must be \"unknown\", never \"available\". "
    "Reply with JSON only using this shape: "
    '{"relevance": 0.0-1.0, "role_match": true|false, "skills_match": 0.0-1.0, '
    '"experience_match": 0.0-1.0, "location_match": 0.0-1.0, '
    '"sponsorship": "offered|not_offered|unknown", '
    '"recommendation": "APPLY|CONSIDER|SKIP", "reason": "one or two sentences"}'
)


class LlmClient:
    """Thin Anthropic wrapper with a per-run call budget."""

    def __init__(self, *, max_calls: int | None = None) -> None:
        settings = get_settings()
        self.enabled = settings.llm_available
        self.model = settings.llm_model
        self.max_calls = max_calls if max_calls is not None else settings.llm_max_calls_per_run
        self.calls = 0
        self._client: Any = None
        if self.enabled:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            except Exception as exc:  # pragma: no cover - optional dependency
                log.warning("llm.init_failed", error=str(exc)[:200])
                self.enabled = False

    @property
    def budget_left(self) -> int:
        return max(0, self.max_calls - self.calls)

    def _complete(self, system: str, prompt: str, *, max_tokens: int = 400) -> dict | None:
        if not self.enabled or self._client is None or self.budget_left <= 0:
            return None
        self.calls += 1
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            ).strip()
        except Exception as exc:
            log.warning("llm.call_failed", error=str(exc)[:200])
            return None
        return _parse_json(text)

    # -- stage 5: duplicate adjudication --

    def adjudicate_duplicate(
        self, a: NormalizedJob, b: NormalizedJob
    ) -> tuple[str, float]:
        payload = self._complete(DEDUP_SYSTEM, _dedup_prompt(a, b), max_tokens=250)
        if not payload:
            return "UNCERTAIN", 0.0
        verdict = str(payload.get("verdict", "UNCERTAIN")).upper()
        if verdict not in {"SAME", "DIFFERENT", "UNCERTAIN"}:
            verdict = "UNCERTAIN"
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return verdict, max(0.0, min(1.0, confidence))

    # -- semantic relevance --

    def classify_relevance(self, job: NormalizedJob, profile_summary: str) -> dict | None:
        payload = self._complete(
            CLASSIFY_SYSTEM, _classify_prompt(job, profile_summary), max_tokens=500
        )
        if not payload:
            return None
        return _sanitize_assessment(payload)


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    # Models sometimes wrap JSON in code fences.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _sanitize_assessment(payload: dict) -> dict:
    """Clamp the model's output into the documented vocabulary.

    Anything unrecognised collapses to the safe value, which for sponsorship is
    always ``unknown``.
    """

    def num(key: str, default: float = 0.0) -> float:
        try:
            return max(0.0, min(1.0, float(payload.get(key, default))))
        except (TypeError, ValueError):
            return default

    sponsorship = str(payload.get("sponsorship", "unknown")).lower()
    if sponsorship not in {"offered", "not_offered", "unknown"}:
        sponsorship = "unknown"
    recommendation = str(payload.get("recommendation", "SKIP")).upper()
    if recommendation not in {"APPLY", "CONSIDER", "SKIP"}:
        recommendation = "SKIP"

    return {
        "relevance": num("relevance"),
        "role_match": bool(payload.get("role_match", False)),
        "skills_match": num("skills_match"),
        "experience_match": num("experience_match"),
        "location_match": num("location_match"),
        "sponsorship": sponsorship,
        "recommendation": recommendation,
        "reason": str(payload.get("reason", ""))[:400],
    }


def _dedup_prompt(a: NormalizedJob, b: NormalizedJob) -> str:
    def describe(job: NormalizedJob) -> str:
        return "\n".join(
            [
                f"Company: {job.company}",
                f"Title: {job.title}",
                f"Location: {job.location_raw or 'unknown'}",
                f"Requisition ID: {job.requisition_id or 'unknown'}",
                f"Employment type: {job.employment_type}",
                f"Posted: {job.date_posted.date() if job.date_posted else 'unknown'}",
                f"Source: {job.source}",
                f"Description: {truncate(job.description, 900)}",
            ]
        )

    return f"Job A:\n{describe(a)}\n\nJob B:\n{describe(b)}\n\nAre these the same underlying position?"


def _classify_prompt(job: NormalizedJob, profile_summary: str) -> str:
    return (
        f"Candidate profile:\n{profile_summary}\n\n"
        f"Job posting:\n"
        f"Company: {job.company}\n"
        f"Title: {job.title}\n"
        f"Location: {job.location_raw or 'unknown'}\n"
        f"Employment type: {job.employment_type}\n"
        f"Detected skills: {', '.join(job.skills[:20]) or 'none detected'}\n"
        f"Stated experience requirement: "
        f"{job.experience_required_years if job.experience_required_years is not None else 'not stated'}\n"
        f"Description: {truncate(job.description, 2500)}\n"
        f"Requirements: {truncate(job.requirements, 1200)}\n\n"
        "Assess the fit."
    )


def profile_summary_text(profile: Any) -> str:
    """Compact profile description for prompts."""
    parts = [
        f"School: {getattr(profile, 'school', None) or 'unknown'}",
        f"Degree: {getattr(profile, 'degree', None) or 'unknown'} in "
        f"{getattr(profile, 'major', None) or 'unknown'}",
        f"Graduation: {getattr(profile, 'graduation_year', None) or 'unknown'}",
        f"Skills: {', '.join(profile.all_skills()[:25]) if hasattr(profile, 'all_skills') else ''}",
    ]
    work_auth = getattr(profile, "work_authorization", None)
    if work_auth:
        parts.append(f"Work authorization: {work_auth}")
    return "\n".join(parts)
