"""Store facts a language model read out of a posting's prose.

The deterministic extractor matches a fixed vocabulary of roughly 120 strings,
so it finds no skills at all in about two thirds of crawled postings -- and
skill overlap is a quarter of the relevance score. A model reads the same text
without needing the vocabulary to have anticipated the words.

Kept in its own column rather than merged into ``skills``. The two have
different provenance: one is a regex hit on a curated list, the other is a
model's reading, and a user deciding how much to trust a score is entitled to
know which produced it. ``enrichment_hash`` records the content the facts were
read from, so a posting that changes becomes eligible for re-reading instead of
carrying stale facts forever.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None

NEW_COLUMNS = (
    ("enrichment", sa.JSON()),
    ("enriched_at", sa.DateTime()),
    ("enrichment_model", sa.String(length=120)),
    ("enrichment_hash", sa.String(length=64)),
)


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns("jobs")}


def upgrade() -> None:
    # Adding a column that is already there aborts the whole migration, and a
    # database that has been through a manual fix-up is the normal case here.
    present = _existing(op.get_bind())
    for name, type_ in NEW_COLUMNS:
        if name not in present:
            op.add_column("jobs", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    present = _existing(op.get_bind())
    for name, _ in reversed(NEW_COLUMNS):
        if name in present:
            op.drop_column("jobs", name)
