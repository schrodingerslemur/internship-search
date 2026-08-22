"""Text normalisation helpers shared by matching and deduplication."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from selectolax.parser import HTMLParser

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9+#. ]+")
_HTML_HINT = re.compile(r"<[a-zA-Z/][^>]*>")

#: Corporate suffixes stripped when slugifying a company name.
_COMPANY_SUFFIXES = (
    "incorporated", "corporation", "technologies", "technology", "holdings",
    "solutions", "systems", "laboratories", "labs", "group", "limited",
    "company", "corp", "inc", "llc", "ltd", "plc", "gmbh", "co", "sa", "ag",
    "nv", "bv", "pte", "pvt", "the",
    # Finance and trading houses advertise under both the bare name and the
    # full one -- "IMC" and "IMC Trading", "Jump Trading" and "Jump Trading
    # Group". Treated as distinct employers they split the preferred-company
    # boost and show the same posting twice.
    #
    # Deliberately no geographic words here. "America" would reduce "Bank of
    # America" to "bank of", which collides with every other "Bank of ...".
    "trading", "capital", "partners", "management", "securities", "markets",
    "ventures", "associates",
)

#: Well-known aliases so the same employer collapses to one company record.
COMPANY_ALIASES: dict[str, str] = {
    "advanced micro devices": "amd",
    "alphabet": "google",
    "google llc": "google",
    "meta platforms": "meta",
    "facebook": "meta",
    "international business machines": "ibm",
    "amazon web services": "amazon",
    "aws": "amazon",
    "amazon com": "amazon",
    "nvidia corporation": "nvidia",
    "apple inc": "apple",
    "microsoft corporation": "microsoft",
    "alphabet inc": "google",
    "x formerly twitter": "x",
    "twitter": "x",
}

#: Noise removed from titles before comparison.
_TITLE_NOISE = re.compile(
    r"""(?ix)
    \b(
      20\d{2}|                                  # years
      summer|fall|autumn|winter|spring|         # seasons (kept as discriminators)
      req\s*id|requisition|job\s*id|
      remote|hybrid|onsite|on\s*site|
      us|usa|united\s+states|
      full[\s-]*time|part[\s-]*time|
      new\s+grad(uate)?|
      university|college|student|
      program|programme
    )\b
    """
)
_TITLE_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_TITLE_REQID = re.compile(r"\b[A-Z]{1,4}[-_]?\d{4,}\b")

#: Tokens that distinguish otherwise similar postings. Two listings may only
#: merge when their discriminator sets agree -- this is the primary guard
#: against over-deduplication (verification vs design, summer vs fall, ...).
DISCRIMINATOR_GROUPS: dict[str, tuple[str, ...]] = {
    "verification": ("verification", "dv", "uvm", "validation"),
    "design": ("design", "rtl", "logic design", "micro-architecture", "microarchitecture"),
    "physical": ("physical design", "pnr", "place and route", "backend", "back-end", "sta"),
    "analog": ("analog", "mixed signal", "mixed-signal", "rf"),
    "software": ("software", "swe", "full stack", "fullstack", "backend engineer", "frontend"),
    "hardware": ("hardware", "asic", "fpga", "silicon", "soc"),
    "firmware": ("firmware", "embedded", "bsp", "driver", "kernel"),
    "ml": ("machine learning", "deep learning", "ml ", "ai ", "mlops"),
    "security": ("security", "cryptography", "crypto", "pentest"),
    "intern": ("intern", "internship"),
    "coop": ("co-op", "coop", "co op"),
    "phd": ("phd", "doctoral"),
    "masters": ("masters", "master's", "ms "),
    "summer": ("summer",),
    "fall": ("fall", "autumn"),
    "spring": ("spring",),
    "winter": ("winter",),
}


def strip_html(text: str | None) -> str:
    """Convert an HTML fragment to readable plain text."""
    if not text:
        return ""
    if not _HTML_HINT.search(text):
        # Plain text already: collapse runs of spaces/tabs but keep newlines.
        # Section detection depends on headings staying on their own lines.
        out = re.sub(r"[ \t]+", " ", text)
        return re.sub(r"\n\s*\n+", "\n\n", out).strip()
    try:
        tree = HTMLParser(text)
        for tag in tree.css("script, style"):
            tag.decompose()
        # Preserve list/paragraph breaks so requirement bullets stay separable.
        for tag in tree.css("li"):
            if tag.text():
                tag.replace_with("\n* " + tag.text(strip=True) + "\n")
        for sel in ("br", "p", "div", "h1", "h2", "h3", "h4"):
            for tag in tree.css(sel):
                if tag.text():
                    tag.replace_with("\n" + tag.text(strip=True) + "\n")
        out = tree.text(separator=" ")
    except Exception:  # pragma: no cover - parser edge cases
        out = re.sub(r"<[^>]+>", " ", text)
    out = unicodedata.normalize("NFKC", out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n\s*\n+", "\n\n", out)
    return out.strip()


def normalize_text(text: str | None) -> str:
    """Lowercase, de-accent, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _WS.sub(" ", text.lower()).strip()


def slugify_company(name: str | None) -> str:
    """Normalise a company name into a stable matching slug."""
    base = normalize_text(name)
    if not base:
        return ""
    base = base.replace("&", " and ")
    base = _NON_ALNUM.sub(" ", base)
    base = _WS.sub(" ", base).strip()
    if base in COMPANY_ALIASES:
        return COMPANY_ALIASES[base]
    words = [w for w in base.split() if w]
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    while words and words[0] in ("the",):
        words.pop(0)
    slug = " ".join(words).strip()
    slug = COMPANY_ALIASES.get(slug, slug)
    return slug or base


def normalize_title(title: str | None) -> str:
    """Reduce a title to its comparable core."""
    if not title:
        return ""
    text = _TITLE_BRACKETS.sub(" ", title)
    text = _TITLE_REQID.sub(" ", text)
    text = normalize_text(text)
    text = text.replace("/", " ").replace("-", " ").replace(",", " ")
    text = _TITLE_NOISE.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def title_tokens(title: str | None) -> frozenset[str]:
    return frozenset(t for t in normalize_title(title).split() if len(t) > 1)


# --------------------------------------------------------------------------
# Role affinity
#
# Dedup asks "are these the same posting?", which is a symmetric question and
# is answered by `token_set_ratio` below. Ranking asks a different, directional
# one: "does this title contain the role I am looking for?" A title may carry
# the team, the term and the requisition alongside the role and still be a
# perfect match, so the two questions need two functions.
# --------------------------------------------------------------------------

#: Morphological variants collapsed before comparison. Job titles use these
#: interchangeably -- "Hardware Engineering Intern" and "Hardware Engineer
#: Intern" are the same posting written by two different recruiters.
ROLE_STEMS: dict[str, str] = {
    "engineering": "engineer", "engineers": "engineer", "engineer": "engineer",
    "designer": "design", "designs": "design", "designing": "design",
    "development": "develop", "developer": "develop", "developing": "develop",
    "verification": "verify", "validation": "verify", "verifying": "verify",
    "architecture": "architect", "architectures": "architect",
    "systems": "system", "programming": "program", "programmer": "program",
    "internship": "intern", "internships": "intern", "interns": "intern",
    "coop": "intern", "researcher": "research", "analytics": "analyst",
}

#: Tokens present in nearly every internship title, which therefore separate
#: nothing. Left in, they inflate both sides of the comparison and let two
#: unrelated roles look similar because both end in "Intern".
ROLE_STOPWORDS: frozenset[str] = frozenset(
    {
        "intern", "co", "op", "the", "of", "and", "for", "new", "at", "in", "to",
        "undergraduate", "undergrad", "graduate", "grad", "campus", "student",
        "summer", "fall", "autumn", "spring", "winter", "year", "years", "level",
        "phd", "masters", "master", "ms", "bs", "ba", "part", "full", "time",
        "opportunity", "opportunities", "position", "role", "job", "career",
        "i", "ii", "iii", "temporary", "seasonal", "paid", "unpaid",
    }
)

#: Domain equivalences. An employer advertising "Silicon Design Engineering"
#: and one advertising "Hardware Engineering" are hiring for the same thing;
#: only the house style differs.
ROLE_SYNONYMS: dict[str, str] = {
    "silicon": "hardware", "asic": "hardware", "soc": "hardware",
    "vlsi": "hardware", "chip": "hardware", "ic": "hardware",
    "semiconductor": "hardware", "circuit": "hardware", "circuits": "hardware",
    "firmware": "embedded", "microcontroller": "embedded",
    "swe": "software", "sde": "software",
    "ai": "ml", "machine": "ml", "learning": "ml",
    "fpgas": "fpga", "gpu": "hardware", "cpu": "hardware",
}

#: Words in a role phrase that describe *seniority or function* rather than
#: *domain*. A role must match on something more specific than these, or
#: "Project Engineer Intern" would satisfy "Hardware Engineer Intern".
GENERIC_ROLE_WORDS: frozenset[str] = frozenset(
    {"engineer", "design", "develop", "system", "technical", "technology", "program"}
)

#: A title carrying one of these is in a different discipline entirely, however
#: many words it happens to share. Without this guard, recall-based matching
#: scores "Mechanical Engineer, Robotics Hardware" as a hardware role.
FOREIGN_DISCIPLINES: frozenset[str] = frozenset(
    {
        "mechanical", "mechanic", "technician", "civil", "chemical", "biomedical",
        "industrial", "manufacturing", "environmental", "structural", "aerospace",
        "sales", "marketing", "recruiting", "recruiter", "hr", "finance",
        "accounting", "accountant", "audit", "legal", "compliance", "paralegal",
        "nursing", "nurse", "clinical", "biology", "chemistry", "pharmacy",
        "journalism", "news", "editorial", "photographer", "teaching", "teacher",
        "construction", "logistics", "warehouse", "retail", "hospitality",
        "communications", "publicity", "advertising", "merchandising",
    }
)


def role_tokens(text: str | None) -> frozenset[str]:
    """Tokens of a title or role phrase, reduced to comparable meaning."""
    out: set[str] = set()
    for word in normalize_title(text).split():
        if len(word) < 2:
            continue
        word = ROLE_STEMS.get(word, word)
        if word in ROLE_STOPWORDS:
            continue
        out.add(ROLE_SYNONYMS.get(word, word))
    return frozenset(out)


def role_anchors(role: str | None) -> frozenset[str]:
    """The domain words a title must contain to count as this role at all."""
    tokens = role_tokens(role)
    specific = tokens - GENERIC_ROLE_WORDS
    return specific or tokens


def role_affinity(title: str | None, role: str | None) -> float:
    """How well ``title`` matches the target ``role``, 0..1.

    Recall-led on purpose: the question is how much of the role the title
    covers, not how similar the two strings are. Dividing by the longer string
    -- which is what a symmetric ratio does -- punishes a title for naming the
    team, the term and the requisition alongside the role, so
    "Silicon Design Engineering Intern/Co-Op" scored 0.33 against
    "Hardware Engineer Intern" despite being exactly that job.

    Two guards keep recall from over-firing. The role's domain word must
    actually appear, and a title belonging to another discipline scores zero
    however many generic words it shares.
    """
    title_set, role_set = role_tokens(title), role_tokens(role)
    if not title_set or not role_set:
        return 0.0
    if title_set & FOREIGN_DISCIPLINES:
        return 0.0
    anchors = role_anchors(role)
    if anchors and not (title_set & anchors):
        return 0.0

    hit = title_set & role_set
    # Recall is weighted by how much each role word actually identifies the
    # role. Counting words equally made "FPGA Design Intern" score 0.44
    # against "FPGA Engineer Intern" -- half marks for missing the word
    # "engineer", which distinguishes nothing, while matching "FPGA", which
    # distinguishes everything.
    def weight(token: str) -> float:
        return 0.35 if token in GENERIC_ROLE_WORDS else 1.0

    role_mass = sum(weight(t) for t in role_set)
    recall = sum(weight(t) for t in hit) / role_mass if role_mass else 0.0
    precision = len(hit) / len(title_set)
    # Recall decides; precision only softens a title that is mostly about
    # something else, so a long but on-topic title is not penalised for length.
    return recall * (0.75 + 0.25 * precision)


def discriminators(*texts: str | None) -> frozenset[str]:
    """Extract the discriminator groups present in the given text."""
    hay = " ".join(normalize_text(t) for t in texts if t)
    if not hay:
        return frozenset()
    hay = f" {hay} "
    found: set[str] = set()
    for group, needles in DISCRIMINATOR_GROUPS.items():
        for needle in needles:
            if needle in hay:
                found.add(group)
                break
    return frozenset(found)


def shingles(text: str | None, size: int = 5, limit: int = 150) -> frozenset[str]:
    """Word-level shingle set used for Jaccard description similarity."""
    words = normalize_text(text).split()
    if len(words) < size:
        return frozenset([" ".join(words)]) if words else frozenset()
    grams = [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]
    if len(grams) <= limit:
        return frozenset(grams)
    # Deterministic down-sample: keep the `limit` smallest hashes (MinHash-style),
    # so two long descriptions still compare on the same subspace.
    hashed = sorted(grams, key=lambda g: hashlib.md5(g.encode()).hexdigest())
    return frozenset(hashed[:limit])


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def token_set_ratio(a: str, b: str) -> float:
    """Order-independent token overlap, 0..1."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def sha256_of(*parts: str | None) -> str:
    joined = "|".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def fingerprint(company_slug: str, title_core: str, location_key: str, employment: str) -> str:
    """Deterministic dedup blocking key (dedup stage 3)."""
    return sha256_of(company_slug, title_core, location_key, employment)[:32]


def truncate(text: str | None, limit: int = 280) -> str:
    if not text:
        return ""
    text = _WS.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"
