"""Greenhouse application forms: where they are and what they ask.

Greenhouse publishes every form's questions -- labels, field names, required
flags and the exact dropdown options -- through the same public board API the
crawler already uses, so answers can be prepared without opening a browser:

``https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
#: The bare form, without the company's careers-site wrapper around it. Many
#: companies embed Greenhouse in an iframe on their own site; the embed URL is
#: the same form with nothing in front of it.
FORM = "https://job-boards.greenhouse.io/embed/job_app?for={token}&token={job_id}"

_PATH = re.compile(r"greenhouse\.io/(?P<token>[\w.-]+)/jobs/(?P<id>\d+)")


@dataclass(frozen=True)
class GreenhouseJob:
    token: str
    job_id: str

    @property
    def form_url(self) -> str:
        return FORM.format(token=self.token, job_id=self.job_id)

    @property
    def key(self) -> str:
        return f"greenhouse:{self.token}:{self.job_id}"


def identify(url: str | None) -> GreenhouseJob | None:
    """The board token and job id behind a Greenhouse URL, if it is one.

    A company careers page carrying only ``?gh_jid=`` is not enough: the job
    id is there but the board token is not, and the form cannot be addressed
    without both.
    """
    if not url or "greenhouse.io" not in url:
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "embed/job_app" in parsed.path and query.get("for") and query.get("token"):
        token, job_id = query["for"][0], query["token"][0]
        return GreenhouseJob(token, job_id) if job_id.isdigit() else None
    match = _PATH.search(f"{parsed.netloc}{parsed.path}")
    if match and match.group("token") not in {"embed", "v1"}:
        return GreenhouseJob(match.group("token"), match.group("id"))
    return None


def from_listing(source: str, source_job_id: str | None, urls: list[str | None]) -> GreenhouseJob | None:
    """Identify from a stored listing: the crawler's own id first, then URLs."""
    if source == "greenhouse" and source_job_id and ":" in source_job_id:
        token, _, job_id = source_job_id.rpartition(":")
        if token and job_id.isdigit():
            return GreenhouseJob(token, job_id)
    for url in urls:
        found = identify(url)
        if found:
            return found
    return None


@dataclass
class Option:
    label: str
    value: str


@dataclass
class Question:
    #: The form field's name, which is also its DOM id on the hosted form.
    name: str
    label: str
    #: text, textarea, file, select, multiselect
    type: str
    required: bool
    options: list[Option] = field(default_factory=list)
    #: question, location, compliance
    group: str = "question"

    @property
    def option_labels(self) -> list[str]:
        return [o.label for o in self.options]


_TYPES = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
}


def _field_questions(item: dict, group: str) -> list[Question]:
    """One API question, which may carry several alternative fields.

    Resume and cover letter each offer a file upload *and* a paste-it-in
    textarea for the same answer. Only the first field is the real question;
    the alternatives are skipped so they are never filled twice.
    """
    fields = [f for f in item.get("fields") or [] if f.get("type") != "input_hidden"]
    if not fields:
        return []
    first = fields[0]
    kind = _TYPES.get(first.get("type", ""))
    if kind is None:
        return []
    return [
        Question(
            name=str(first.get("name")),
            label=str(item.get("label") or "").strip(),
            type=kind,
            required=bool(item.get("required")),
            options=[
                Option(label=str(v.get("label", "")).strip(), value=str(v.get("value", "")))
                for v in first.get("values") or []
            ],
            group=group,
        )
    ]


def parse_questions(payload: dict) -> list[Question]:
    out: list[Question] = []
    for item in payload.get("questions") or []:
        out.extend(_field_questions(item, "question"))
    for item in payload.get("location_questions") or []:
        out.extend(_field_questions(item, "location"))
    for block in payload.get("compliance") or []:
        for item in block.get("questions") or []:
            out.extend(_field_questions(item, "compliance"))
    return out


def fetch(job: GreenhouseJob, client: httpx.Client | None = None) -> tuple[dict, list[Question]]:
    """The posting and its questions. Raises on a closed or missing posting."""
    own = client is None
    client = client or httpx.Client(timeout=25)
    try:
        response = client.get(
            API.format(token=job.token, job_id=job.job_id), params={"questions": "true"}
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if own:
            client.close()
    return payload, parse_questions(payload)
