"""Widen identity columns.

SQLite ignores VARCHAR limits, so these columns were sized from the values seen
in local development. PostgreSQL enforces them, and a Workday requisition id --
which is the whole job slug, not a short number -- overflows ``requisition_id``
at 120 characters and aborts the entire persistence flush.

These columns cannot be truncated: the deduplicator compares them for equality
to decide whether two listings are the same opening, and two distinct Workday
slugs frequently share a long prefix. So they are widened instead. Free-text
columns are handled the other way round, by the clamp guard in models/base.py.

Revision ID: b1f2c3d4e5a6
Revises: 093cae67bbb3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b1f2c3d4e5a6"
down_revision = "093cae67bbb3"
branch_labels = None
depends_on = None

#: (table, column, new length, old length, nullable)
WIDENED: tuple[tuple[str, str, int, int, bool], ...] = (
    ("jobs", "requisition_id", 500, 120, True),
    ("jobs", "ats_identity", 500, 300, True),
    ("job_listings", "ats_identity", 500, 300, True),
    ("job_listings", "source_job_id", 500, 300, False),
    ("dedup_decisions", "left_key", 500, 300, False),
    ("dedup_decisions", "right_key", 500, 300, False),
    ("ats_boards", "board_token", 400, 200, False),
)


def _is_sqlite() -> bool:
    # SQLite stores no length metadata at all, so altering these types is both
    # unnecessary and expensive there (it forces a full table rebuild).
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if _is_sqlite():
        return
    for table, column, new_length, _old, nullable in WIDENED:
        op.alter_column(
            table,
            column,
            existing_type=sa.String(length=_old),
            type_=sa.String(length=new_length),
            existing_nullable=nullable,
        )


def downgrade() -> None:
    if _is_sqlite():
        return
    # Values longer than the old limit would be rejected on the way back down;
    # trim them first so the downgrade cannot fail halfway through.
    for table, column, new_length, old_length, nullable in WIDENED:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = LEFT({column}, {old_length}) "
                f"WHERE LENGTH({column}) > {old_length}"
            )
        )
        op.alter_column(
            table,
            column,
            existing_type=sa.String(length=new_length),
            type_=sa.String(length=old_length),
            existing_nullable=nullable,
        )
