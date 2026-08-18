"""Normalisation: ``RawJob`` -> ``NormalizedJob``.

Every source, however different its payload, converges here. This stage owns
all inference (employment type, sponsorship, salary, deadlines) so that sources
stay thin adapters and inference rules live in exactly one place.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.models.base import RemoteStatus, SourceKind, utcnow
from app.pipeline import extract as ex
from app.pipeline.identity import canonicalize_url, extract_ats_identity, url_hash
from app.pipeline.textutil import (
    discriminators,
    fingerprint,
    normalize_title,
    sha256_of,
    shingles,
    slugify_company,
    strip_html,
)
from app.schemas.job import NormalizedJob, RawJob

#: Skill vocabulary used to tag postings. Extend freely -- it only affects the
#: `skills` list surfaced in the UI and the skill-overlap score, never filtering.
SKILL_VOCAB: tuple[str, ...] = (
    "fpga", "rtl", "systemverilog", "verilog", "vhdl", "uvm", "asic", "soc",
    "verification", "validation", "synthesis", "timing analysis", "sta",
    "place and route", "physical design", "dft", "scan", "jtag", "axi", "ahb",
    "pcie", "ddr", "hbm", "ethernet", "usb", "i2c", "spi", "uart", "can bus",
    "vivado", "vitis", "quartus", "questasim", "modelsim", "vcs", "verdi",
    "synopsys", "cadence", "xilinx", "altera", "intel quartus", "yosys",
    "cocotb", "chisel", "hls", "opencl", "cuda", "tensorrt", "verilator",
    "computer architecture", "microarchitecture", "cpu", "gpu", "tpu", "npu",
    "riscv", "risc-v", "arm", "x86", "mips",
    "embedded", "firmware", "rtos", "freertos", "bare metal", "device driver",
    "linux kernel", "yocto", "zephyr", "microcontroller", "stm32", "arduino",
    "c", "c++", "python", "rust", "go", "java", "javascript", "typescript",
    "matlab", "scala", "haskell", "ocaml", "perl", "tcl", "bash", "assembly",
    "pytorch", "tensorflow", "jax", "numpy", "pandas", "scikit-learn",
    "machine learning", "deep learning", "reinforcement learning", "nlp",
    "computer vision", "llm", "transformers",
    "docker", "kubernetes", "aws", "gcp", "azure", "terraform", "ci/cd",
    "git", "jenkins", "sql", "postgresql", "redis", "kafka", "spark",
    "react", "node.js", "django", "flask", "fastapi", "graphql",
    "signal processing", "dsp", "rf", "analog", "mixed signal", "power",
    "hardware security", "cryptography", "side channel", "fault injection",
    "secure boot", "tpm", "trustzone", "formal verification", "model checking",
    "low latency", "high frequency trading", "networking", "tcp/ip", "rdma",
    "distributed systems", "operating systems", "compilers", "llvm",
)

#: Sources sometimes stuff the whole posting into one blob. These headings let
#: us split requirements/responsibilities back out for the UI.
_REQ_HEADING = re.compile(
    r"(?im)^\s*(?:#+\s*)?(?:minimum\s+)?(?:qualifications?|requirements?|what\s+you'?ll?\s+need|"
    r"what\s+we'?re\s+looking\s+for|basic\s+qualifications?|who\s+you\s+are|skills?\s+(?:and|&)\s+experience)\s*:?\s*$"
)
_PREF_HEADING = re.compile(
    r"(?im)^\s*(?:#+\s*)?(?:preferred\s+qualifications?|nice\s+to\s+have|bonus\s+points?|"
    r"preferred\s+skills?|desired\s+qualifications?|pluses?)\s*:?\s*$"
)
_RESP_HEADING = re.compile(
    r"(?im)^\s*(?:#+\s*)?(?:responsibilities|what\s+you'?ll?\s+do|the\s+role|your\s+impact|"
    r"job\s+description|duties|about\s+the\s+role)\s*:?\s*$"
)


def _split_sections(text: str) -> dict[str, str]:
    """Split a description into responsibilities / requirements / preferred."""
    if not text:
        return {}
    marks: list[tuple[int, str]] = []
    for name, pattern in (
        ("requirements", _REQ_HEADING),
        ("preferred", _PREF_HEADING),
        ("responsibilities", _RESP_HEADING),
    ):
        for m in pattern.finditer(text):
            marks.append((m.start(), name))
    if not marks:
        return {}
    marks.sort()
    out: dict[str, str] = {}
    for idx, (pos, name) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(text)
        body = text[pos:end]
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.strip()
        if body and name not in out:
            out[name] = body
    return out


#: Skills whose names are ordinary words, unit symbols, or legal shorthand.
#: A word-boundary match is not enough for these -- "8 U.S.C. 1324b" must not
#: register the C programming language, and "heat to 300 C" must not either --
#: so each requires an explicit technical context.
AMBIGUOUS_SKILL_PATTERNS: dict[str, str] = {
    "c": (
        r"\bc/c\+\+|\bc\+\+/c\b"
        r"|\b(?:program\w*|cod\w+|develop\w*|written|writing|proficien\w+|experience|fluent|skills?)"
        r"[^.]{0,30}?\bin\s+c\b"
        r"|\b(?:embedded|ansi|bare[\s-]?metal)\s+c\b"
        r"|\bc\s+(?:programming|language|compiler)\b"
    ),
    "go": r"\bgolang\b|\bgo\s+(?:programming|language)\b|\b(?:in|using|with)\s+go\b",
    "r": r"\br\s+(?:programming|language|studio)\b|\brstudio\b|\b(?:in|using|with)\s+r\b",
    "rf": r"\brf\s+(?:design|engineer\w*|circuit\w*|front[\s-]?end|system\w*|test\w*)\b|\bradio\s+frequency\b",
    "arm": r"\barm\s+(?:cortex|architecture|processor|core|assembly|v\d)\b|\bcortex[\s-]?[amr]\b|\barm64\b",
    "power": r"\bpower\s+(?:electronics|management|integrity|supply|converter|rail|delivery)\b|\blow[\s-]?power\s+design\b",
    "swift": r"\bswift\s+(?:ui|programming|language)\b|\bswiftui\b",
}


def extract_skills(text: str, title: str = "") -> list[str]:
    """Tag a posting with skills from the vocabulary.

    Matching is boundary-aware, and genuinely ambiguous names additionally
    require a technical context, so boilerplate and unit symbols cannot
    masquerade as programming languages.
    """
    hay = f" {(title + ' ' + text).lower()} "
    hay = re.sub(r"[\n\r\t]+", " ", hay)
    found: list[str] = []
    for skill in SKILL_VOCAB:
        needle = skill.lower()
        pattern = AMBIGUOUS_SKILL_PATTERNS.get(needle)
        if pattern is not None:
            if re.search(pattern, hay):
                found.append(skill)
        elif len(needle) <= 3:
            # Short tokens still need boundaries to avoid matching inside words.
            if re.search(rf"(?<![a-z0-9+#]){re.escape(needle)}(?![a-z0-9+#])", hay):
                found.append(skill)
        elif needle in hay:
            found.append(skill)
    return found


def normalize_job(raw: RawJob, *, now: datetime | None = None) -> NormalizedJob | None:
    """Convert a raw listing into the canonical schema.

    Returns ``None`` for listings too malformed to be useful (no company or no
    title), which are counted as dropped rather than silently mangled.
    """
    now = now or utcnow()

    company = (raw.company or "").strip()
    title = (raw.title or "").strip()
    if not company or not title:
        return None

    description = strip_html(raw.description)
    requirements = strip_html(raw.requirements)
    responsibilities = strip_html(raw.responsibilities)
    preferred = strip_html(raw.preferred_qualifications)

    # Recover sections when the source gave us one undifferentiated blob.
    if description and not (requirements or responsibilities or preferred):
        sections = _split_sections(description)
        requirements = requirements or sections.get("requirements", "")
        responsibilities = responsibilities or sections.get("responsibilities", "")
        preferred = preferred or sections.get("preferred", "")

    full_text = "\n".join(p for p in (description, requirements, responsibilities, preferred) if p)

    # --- location ---
    location_raw = raw.location or (raw.locations[0] if raw.locations else None)
    parsed = ex.parse_location(location_raw)
    all_locations = list(dict.fromkeys([loc for loc in ([location_raw] + list(raw.locations)) if loc]))

    remote_status = RemoteStatus.UNKNOWN
    if raw.remote_status:
        declared = raw.remote_status.strip().lower()
        mapping = {
            "remote": RemoteStatus.REMOTE, "hybrid": RemoteStatus.HYBRID,
            "onsite": RemoteStatus.ONSITE, "on-site": RemoteStatus.ONSITE,
            "on_site": RemoteStatus.ONSITE, "in office": RemoteStatus.ONSITE,
        }
        remote_status = mapping.get(declared, RemoteStatus.UNKNOWN)
    if remote_status is RemoteStatus.UNKNOWN:
        remote_status = ex.infer_remote_status(location_raw, title)
    if remote_status is RemoteStatus.UNKNOWN and parsed.is_remote:
        remote_status = RemoteStatus.REMOTE

    employment = ex.infer_employment_type(title, full_text, raw.employment_type)

    # --- salary ---
    salary_min, salary_max = raw.salary_min, raw.salary_max
    currency, period, salary_raw_text = raw.salary_currency, raw.salary_period, raw.salary_raw
    if salary_min is None:
        parsed_salary = ex.extract_salary(raw.salary_raw or full_text[:4000])
        salary_min = parsed_salary["min"]  # type: ignore[assignment]
        salary_max = parsed_salary["max"]  # type: ignore[assignment]
        currency = currency or parsed_salary["currency"]  # type: ignore[assignment]
        period = period or parsed_salary["period"]  # type: ignore[assignment]
        salary_raw_text = salary_raw_text or parsed_salary["raw"]  # type: ignore[assignment]

    sponsorship, evidence = ex.extract_sponsorship(full_text, raw.sponsorship_hint)
    deadline, deadline_explicit = (raw.deadline, True) if raw.deadline else ex.extract_deadline(full_text, now)

    # --- identity ---
    identity = extract_ats_identity(raw.apply_url, raw.url, raw.company_url)
    canonical = canonicalize_url(raw.url or raw.apply_url)
    company_slug = slugify_company(company)
    title_core = normalize_title(title)
    requisition = raw.requisition_id or (identity.job_id if identity else None)

    return NormalizedJob(
        source=raw.source,
        source_kind=raw.source_kind or SourceKind.UNKNOWN,
        source_job_id=str(raw.source_job_id),
        canonical_url=canonical,
        canonical_url_hash=url_hash(raw.url or raw.apply_url),
        ats_identity=identity.job_key if identity else None,
        fingerprint=fingerprint(company_slug, title_core, parsed.key, employment.value),
        content_hash=sha256_of(title_core, company_slug, parsed.key, full_text[:6000])[:32],
        company=company,
        company_slug=company_slug,
        title=title,
        title_core=title_core,
        discriminators=discriminators(title, requirements or description or ""),
        location_raw=location_raw,
        locations=all_locations,
        city=parsed.city,
        state=parsed.state,
        country=parsed.country,
        location_key=parsed.key,
        remote_status=remote_status,
        employment_type=employment,
        description=description or None,
        requirements=requirements or None,
        responsibilities=responsibilities or None,
        preferred_qualifications=preferred or None,
        description_shingles=shingles(full_text or title),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        salary_period=period,
        salary_raw=salary_raw_text,
        url=raw.url,
        apply_url=raw.apply_url or raw.url,
        date_posted=raw.date_posted,
        date_updated=raw.date_updated,
        deadline=deadline,
        deadline_is_explicit=deadline_explicit,
        requisition_id=requisition,
        terms=raw.terms or ex.extract_terms(title, full_text[:3000]),
        skills=extract_skills(full_text, title),
        degree_requirements=ex.extract_degrees(full_text),
        experience_required_years=ex.extract_experience_years(full_text),
        sponsorship=sponsorship,
        sponsorship_evidence=evidence,
        company_url=raw.company_url,
        raw=raw.raw or {},
    )


def normalize_all(
    raws: list[RawJob], *, now: datetime | None = None
) -> tuple[list[NormalizedJob], int]:
    """Normalise a batch. Returns (normalized, dropped_count)."""
    out: list[NormalizedJob] = []
    dropped = 0
    for raw in raws:
        try:
            job = normalize_job(raw, now=now)
        except Exception:
            dropped += 1
            continue
        if job is None:
            dropped += 1
        else:
            out.append(job)
    return out, dropped
