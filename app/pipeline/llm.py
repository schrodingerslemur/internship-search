"""Optional LLM stages: fact extraction, duplicate adjudication, relevance.

Every stage is strictly additive. With the model disabled or unreachable, the
pipeline runs entirely on deterministic logic and loses no functionality -- it
simply reads fewer facts out of prose, resolves fewer ambiguous duplicate
pairs, and skips the semantic second opinion.

The transport is the OpenAI chat-completions shape, which is what Ollama,
Groq, OpenRouter, Together and the paid providers all speak. That means the
free options are first-class rather than an afterthought: a local Ollama needs
no key at all, and switching provider is a base URL and a model name.

The hard rule for every prompt: **never invent facts.** If the posting does
not state something, the answer is ``unknown`` or an empty list. A model that
guesses "sponsorship available" is worse than no model at all, so the prompts
forbid inference and the parsers reject anything outside the permitted
vocabulary.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

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


EXTRACT_SYSTEM = (
    "You read one job posting and report only what it actually says. "
    "Never infer, never generalise, never fill a field from world knowledge "
    "about the company. If the posting does not state something, leave the "
    "field empty or \"unknown\". "
    "Reply with JSON only, using exactly this shape: "
    '{"skills": ["..."], "domain": "hardware|software|ml|firmware|analog|'
    'research|other|unknown", "seniority": "intern|new_grad|junior|mid|senior|'
    'unknown", "is_internship": true|false|null, '
    '"sponsorship": "offered|not_offered|citizenship_required|unknown", '
    '"min_years_experience": number|null, "terms": ["Summer 2027"], '
    '"summary": "one sentence, under 25 words"}. '
    "skills: concrete technologies, tools and techniques named in the posting "
    "-- lowercase, at most 20, no soft skills, no duties, no company names."
)


class LlmClient:
    """OpenAI-compatible chat client with a per-run call budget.

    Deliberately not an SDK. The chat-completions request is a single JSON POST,
    and depending on one vendor's client library is what tied the previous
    implementation to one vendor. Speaking the wire format directly means
    Ollama, Groq, OpenRouter and the paid providers are all the same code, and
    the free ones need no extra install.
    """

    def __init__(self, *, max_calls: int | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self.enabled = settings.llm_available
        self.model = settings.llm_model
        self.base_url = (settings.llm_base_url or "").rstrip("/")
        self.max_calls = max_calls if max_calls is not None else settings.llm_max_calls_per_run
        self.timeout = timeout if timeout is not None else settings.llm_timeout_seconds
        self.calls = 0
        self._key = settings.llm_key
        self._client: httpx.Client | None = None

    @property
    def budget_left(self) -> int:
        return max(0, self.max_calls - self.calls)

    def _http(self) -> httpx.Client:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self._key:
                headers["Authorization"] = f"Bearer {self._key}"
            self._client = httpx.Client(
                base_url=self.base_url, headers=headers, timeout=self.timeout
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> LlmClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _complete(self, system: str, prompt: str, *, max_tokens: int = 400) -> dict | None:
        if not self.enabled or self.budget_left <= 0:
            return None
        self.calls += 1
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # Some gateways only honour the newer name; sending both is
            # harmless and saves a per-provider branch.
            "max_completion_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            # Honoured by Ollama, Groq and OpenAI; ignored elsewhere, which is
            # why the response is still parsed defensively below.
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._http().post("/chat/completions", json=body)
            if response.status_code >= 400:
                log.warning(
                    "llm.http_error",
                    status=response.status_code,
                    body=response.text[:200],
                    model=self.model,
                )
                return None
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
        except Exception as exc:
            # A model failure must never break a crawl. The deterministic
            # pipeline is the product; this is a second opinion on top of it.
            log.warning("llm.call_failed", error=f"{type(exc).__name__}: {exc}"[:200])
            return None
        return _parse_json(text or "")

    def health(self) -> tuple[bool, str]:
        """Whether the configured endpoint answers, and what it said.

        Used by the CLI and the coverage page so a misconfigured model reports
        itself instead of silently doing nothing.
        """
        if not self.enabled:
            return False, "LLM_ENABLED is off, or no key for a remote endpoint"
        try:
            models = self._http().get("/models")
            if models.status_code >= 400:
                return False, f"{self.base_url} returned HTTP {models.status_code}"
        except Exception as exc:
            return False, f"cannot reach {self.base_url}: {type(exc).__name__}"
        probe = self._complete(
            'Reply with JSON only: {"ok": true}', "Say ok.", max_tokens=32
        )
        if probe is None:
            return False, f"{self.model} did not return usable JSON"
        return True, f"{self.model} at {self.base_url}"

    # -- fact extraction: the stage that pays for itself --

    def extract_facts(self, job: NormalizedJob) -> dict | None:
        """Read a posting's prose into structured facts.

        This is the highest-value use of a model here. The deterministic
        extractor matches against a fixed vocabulary of about 120 strings, so
        it finds nothing in roughly two thirds of postings -- and the skills
        component is a quarter of the score. A model reads the same text
        without needing the vocabulary to have anticipated the words.
        """
        payload = self._complete(EXTRACT_SYSTEM, _extract_prompt(job), max_tokens=700)
        if not payload:
            return None
        return _sanitize_facts(payload)

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


#: Vocabularies the extractor is allowed to answer from. Anything outside them
#: collapses to the safe value, so a model that ignores the prompt cannot widen
#: the system's beliefs -- the parser is the contract, not the instructions.
_DOMAINS = frozenset(
    {"hardware", "software", "ml", "firmware", "analog", "research", "other", "unknown"}
)
_SENIORITY = frozenset({"intern", "new_grad", "junior", "mid", "senior", "unknown"})
_SPONSORSHIP = frozenset({"offered", "not_offered", "citizenship_required", "unknown"})

#: Words a model reaches for when it has nothing concrete to report. Letting
#: them through would turn "no skills stated" into a confident-looking list.
_NON_SKILLS = frozenset(
    {
        "communication", "teamwork", "leadership", "problem solving",
        "problem-solving", "collaboration", "time management", "attention to detail",
        "self-starter", "motivated", "passionate", "detail oriented",
        "written communication", "verbal communication", "interpersonal skills",
        "critical thinking", "adaptability", "creativity", "work ethic",
        "unknown", "none", "n/a", "not stated",
    }
)


def _sanitize_facts(payload: dict) -> dict:
    """Clamp extraction output into the documented vocabulary.

    A local 7B model will occasionally answer with a synonym, a sentence, or a
    hallucinated skill list. Every field here is either a member of a closed
    set or is discarded, so the worst a bad response can do is contribute
    nothing.
    """
    raw_skills = payload.get("skills")
    skills: list[str] = []
    if isinstance(raw_skills, list):
        for item in raw_skills:
            name = " ".join(str(item).strip().lower().split())
            if not name or len(name) > 40 or name in _NON_SKILLS:
                continue
            if name not in skills:
                skills.append(name)
    skills = skills[:20]

    def member(key: str, allowed: frozenset[str]) -> str:
        value = str(payload.get(key, "unknown")).strip().lower().replace(" ", "_")
        return value if value in allowed else "unknown"

    years = payload.get("min_years_experience")
    try:
        years_value = float(years) if years is not None else None
        if years_value is not None and not (0 <= years_value <= 30):
            years_value = None
    except (TypeError, ValueError):
        years_value = None

    is_internship = payload.get("is_internship")
    if not isinstance(is_internship, bool):
        is_internship = None

    raw_terms = payload.get("terms")
    terms = (
        [str(t).strip()[:24] for t in raw_terms if str(t).strip()][:6]
        if isinstance(raw_terms, list)
        else []
    )

    return {
        "skills": skills,
        "domain": member("domain", _DOMAINS),
        "seniority": member("seniority", _SENIORITY),
        "is_internship": is_internship,
        "sponsorship": member("sponsorship", _SPONSORSHIP),
        "min_years_experience": years_value,
        "terms": terms,
        "summary": " ".join(str(payload.get("summary", "")).split())[:200],
    }


def _extract_prompt(job: NormalizedJob) -> str:
    body = "\n\n".join(
        part
        for part in (
            truncate(job.description, 3500),
            truncate(job.requirements, 1500),
            truncate(job.preferred_qualifications, 800),
        )
        if part
    )
    return (
        f"Company: {job.company}\n"
        f"Title: {job.title}\n"
        f"Location: {job.location_raw or 'not stated'}\n\n"
        f"Posting:\n{body or '(no description available)'}\n\n"
        "Report only what this posting states."
    )


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
