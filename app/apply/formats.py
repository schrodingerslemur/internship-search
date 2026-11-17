"""Which application format a posting uses, so what is learned on one form helps the next.

Greenhouse is filled by its own code (app/apply/greenhouse.py). Every other
format -- Lever, Ashby, Workday, a company's own careers site -- is filled by
the general agent (app/apply/agent.py), which keeps a written how-to per
format in ``ApplyRecipe`` and reads it before the next form of that format.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

#: (host pattern, key, name). First match wins; the key names the recipe.
KNOWN: tuple[tuple[str, str, str], ...] = (
    (r"greenhouse\.io", "greenhouse", "Greenhouse"),
    (r"lever\.co", "lever", "Lever"),
    (r"ashbyhq\.com", "ashby", "Ashby"),
    (r"myworkdayjobs\.com|myworkdaysite\.com|workday\.com", "workday", "Workday"),
    (r"smartrecruiters\.com", "smartrecruiters", "SmartRecruiters"),
    (r"icims\.com", "icims", "iCIMS"),
    (r"taleo\.net", "taleo", "Taleo"),
    (r"successfactors\.|sapsf\.", "successfactors", "SuccessFactors"),
    (r"eightfold\.ai", "eightfold", "Eightfold"),
    (r"jobvite\.com", "jobvite", "Jobvite"),
    (r"bamboohr\.com", "bamboohr", "BambooHR"),
    (r"workable\.com", "workable", "Workable"),
    (r"rippling\.com", "rippling", "Rippling"),
    (r"recruitee\.com", "recruitee", "Recruitee"),
    (r"breezy\.hr", "breezy", "Breezy"),
    (r"applytojob\.com", "jazzhr", "JazzHR"),
    (r"paylocity\.com", "paylocity", "Paylocity"),
    (r"ultipro\.com|ukg\.", "ukg", "UKG"),
    (r"oraclecloud\.com", "oracle", "Oracle Cloud"),
    (r"dayforcehcm\.com", "dayforce", "Dayforce"),
    (r"adp\.com", "adp", "ADP"),
    (r"phenompeople\.com", "phenom", "Phenom"),
    (r"wellfound\.com|angel\.co", "wellfound", "Wellfound"),
    (r"linkedin\.com", "linkedin", "LinkedIn"),
    (r"indeed\.com", "indeed", "Indeed"),
    (r"handshake", "handshake", "Handshake"),
)
_KNOWN = [(re.compile(p, re.I), k, n) for p, k, n in KNOWN]
_SECOND_LEVEL = {"co", "com", "ac", "org", "net", "gov", "edu"}


def host_of(url: str | None) -> str:
    try:
        host = urlparse(url or "").hostname or ""
    except ValueError:
        return ""
    return host.lower()


def site_of(host: str) -> str:
    """The registrable domain: careers.nvidia.com -> nvidia.com."""
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def format_of(url: str | None) -> tuple[str, str] | None:
    """``(key, name)`` for an application URL, or None when there is no URL.

    A known applicant-tracking system is one format wherever it is hosted; any
    other site is its own format, named by its domain.
    """
    host = host_of(url)
    if not host:
        return None
    for rx, key, name in _KNOWN:
        if rx.search(host):
            return key, name
    site = site_of(host)
    return f"site:{site}"[:120], site
