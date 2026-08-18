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
