"""Read job postings with a language model, where the vocabulary can't.

The deterministic skill extractor matches against a fixed list in
``pipeline/normalize.py``. It is fast, auditable and free, and it finds nothing
in roughly two thirds of crawled postings, because a hand-written vocabulary
cannot anticipate how every employer writes. Skill overlap is a quarter of the
relevance score, so that gap is the single largest hole in the ranking.

This module closes it with the cheapest possible model use: one call per
posting, structured output, no tools, no conversation. Three rules keep it
honest and affordable.

**Never invent.** The prompt forbids inference and the parser enforces a closed
vocabulary, so a model that ignores its instructions contributes nothing rather
than something false.

**Spend where it matters.** Postings are selected by how well their title
already matches a target role, so a corpus of five thousand costs a few hundred
calls rather than five thousand.

**Never block the product.** Enrichment is additive. With no model configured
the pipeline behaves exactly as before, and a model that times out mid-batch
leaves the jobs it already read enriched and the rest untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Job
from app.models.base import utcnow
from app.pipeline.llm import LlmClient
from app.pipeline.textutil import role_affinity
from app.schemas.job import normalized_from_job_row
from app.schemas.preferences import SearchPreferences

log = get_logger("enrichment")

#: Below this much body text there is nothing for a model to read, and asking
#: it anyway is how you get invented facts. Matches the scorer's own threshold.
MIN_BODY_CHARS = 40

#: A posting whose title matches no target role at all is not worth a call.
#: This is the whole cost-control story: it turns "read the corpus" into "read
#: the part of the corpus that could plausibly matter".
MIN_ROLE_AFFINITY = 0.35


@dataclass
class EnrichmentReport:
    considered: int = 0
    eligible: int = 0
    attempted: int = 0
    enriched: int = 0
    failed: int = 0
    skills_added: int = 0
    model: str = ""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.enriched}/{self.attempted} postings read "
            f"({self.skills_added} new skills) using {self.model or 'no model'}"
        )


def needs_enrichment(job: Job) -> bool:
    """Whether this posting has prose we have not yet read.

    Re-read when the content hash has moved: a reposted job with a rewritten
    description is new text, and stale facts about it are worse than none.
    """
    body = (job.description or "") + (job.requirements or "")
    if len(body.strip()) < MIN_BODY_CHARS:
        return False
    # Keyed off the timestamp, not the JSON column: a JSON column set to None
    # stores the JSON literal `null`, which is not SQL NULL, so `enrichment IS
    # NULL` silently matches nothing.
    if job.enriched_at is None:
        return True
    return bool(job.content_hash) and job.enrichment_hash != job.content_hash


def candidates(
    session: Session, prefs: SearchPreferences, *, limit: int = 200
) -> list[Job]:
    """Postings worth spending a model call on, best first.

    Ordered by role affinity rather than by stored score, because the stored
    score is partly *caused* by the missing skills this is meant to supply --
    ranking by it would systematically skip the postings that need reading
    most.
    """
    roles = [r.name for r in prefs.enabled_roles()]
    if not roles:
        return []

    rows = session.scalars(
        select(Job)
        .where(
            Job.is_active.is_(True),
            or_(
                Job.enriched_at.is_(None),
                Job.enrichment_hash.is_(None),
                Job.enrichment_hash != Job.content_hash,
            ),
        )
        .limit(5000)
    ).all()

    scored: list[tuple[float, Job]] = []
    for job in rows:
        if not needs_enrichment(job):
            continue
        affinity = max((role_affinity(job.title, role) for role in roles), default=0.0)
        if affinity < MIN_ROLE_AFFINITY:
            continue
        scored.append((affinity, job))

    scored.sort(key=lambda pair: -pair[0])
    return [job for _, job in scored[:limit]]


def apply_facts(job: Job, facts: dict, *, model: str, now: datetime | None = None) -> int:
    """Store what the model read, and merge its skills into the job's own.

    Returns how many skills were genuinely new. The model's list is merged
    rather than replacing the vocabulary's: the deterministic hits are precise
    and worth keeping, and the union is what the scorer should see. The raw
    reading is kept in ``enrichment`` either way, so the two provenances stay
    distinguishable.
    """
    now = now or utcnow()
    existing = {str(s).strip().lower() for s in (job.skills or [])}
    added = [s for s in facts.get("skills", []) if s and s not in existing]

    if added:
        job.skills = list(job.skills or []) + added

    # Only fill facts the deterministic pass left unknown. An extractor that
    # matched an explicit sponsorship sentence has evidence; the model has a
    # reading, and evidence wins.
    if not job.terms and facts.get("terms"):
        job.terms = list(facts["terms"])
    if job.experience_required_years is None and facts.get("min_years_experience") is not None:
        job.experience_required_years = float(facts["min_years_experience"])

    job.enrichment = facts
    job.enrichment_model = model
    job.enrichment_hash = job.content_hash
    job.enriched_at = now
    return len(added)


def enrich_jobs(
    session: Session,
    prefs: SearchPreferences,
    *,
    limit: int = 200,
    client: LlmClient | None = None,
    now: datetime | None = None,
) -> EnrichmentReport:
    """Read up to ``limit`` postings and store the facts.

    Commits nothing; the caller owns the transaction.
    """
    report = EnrichmentReport()
    owned = client is None
    client = client or LlmClient(max_calls=limit)
    report.model = client.model if client.enabled else ""

    try:
        if not client.enabled:
            report.errors.append(
                "No model configured. Set LLM_ENABLED=true and LLM_BASE_URL "
                "(a local Ollama needs no key)."
            )
            return report

        from sqlalchemy import func

        targets = candidates(session, prefs, limit=limit)
        report.considered = (
            session.scalar(select(func.count(Job.id)).where(Job.is_active.is_(True))) or 0
        )
        report.eligible = len(targets)

        for job in targets:
            if client.budget_left <= 0:
                break
            report.attempted += 1
            try:
                facts = client.extract_facts(normalized_from_job_row(job))
            except Exception as exc:  # pragma: no cover - defensive
                facts = None
                report.errors.append(f"{job.id}: {type(exc).__name__}")
            if not facts:
                report.failed += 1
                continue
            report.skills_added += apply_facts(job, facts, model=client.model, now=now)
            report.enriched += 1

        session.flush()
        log.info(
            "enrichment.done",
            enriched=report.enriched,
            attempted=report.attempted,
            skills_added=report.skills_added,
            model=report.model,
        )
        return report
    finally:
        if owned:
            client.close()
