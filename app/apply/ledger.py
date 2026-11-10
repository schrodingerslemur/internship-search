"""What has been prepared, submitted or skipped, kept on this machine.

Keyed by the Greenhouse identity (``greenhouse:{board}:{id}``) rather than a
database row id, so the record means the same thing whichever database the
jobs were read from.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from app.apply.answers import Draft
from app.apply.profile import apply_dir

PREPARED = "prepared"
SUBMITTED = "submitted"
SKIPPED = "skipped"
FAILED = "failed"


def _ledger_path() -> Path:
    return apply_dir() / "ledger.json"


def _draft_path(key: str) -> Path:
    return apply_dir() / "drafts" / (re.sub(r"[^\w.-]", "_", key) + ".json")


def load() -> dict[str, dict]:
    path = _ledger_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(entries: dict[str, dict]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_draft(draft: Draft) -> None:
    path = _draft_path(draft.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft.to_dict(), indent=2), encoding="utf-8")
    entries = load()
    entries[draft.key] = {
        "status": PREPARED,
        "company": draft.company,
        "title": draft.title,
        "canonical_job_id": draft.canonical_job_id,
        "prepared_at": draft.prepared_at,
        "updated_at": draft.prepared_at,
    }
    _save(entries)


def set_status(key: str, status: str, detail: str | None = None) -> None:
    entries = load()
    entry = entries.setdefault(key, {})
    entry["status"] = status
    entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if detail:
        entry["detail"] = detail
    _save(entries)


def load_draft(key: str) -> Draft | None:
    path = _draft_path(key)
    if not path.exists():
        return None
    return Draft.from_dict(json.loads(path.read_text(encoding="utf-8")))


def waiting() -> list[Draft]:
    """Prepared drafts not yet submitted or skipped, oldest first."""
    drafts = []
    for key, entry in sorted(load().items(), key=lambda kv: kv[1].get("prepared_at", "")):
        if entry.get("status") == PREPARED:
            draft = load_draft(key)
            if draft is not None:
                drafts.append(draft)
    return drafts


def prepared_on(day: date) -> int:
    """How many drafts were prepared on a given day, for the daily cap."""
    stamp = day.isoformat()
    return sum(1 for e in load().values() if str(e.get("prepared_at", "")).startswith(stamp))


def known_keys() -> set[str]:
    return set(load())
