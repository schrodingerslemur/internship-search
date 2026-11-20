"""Check applications before they are sent.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "review_applications" not in _columns("users"):
        op.add_column(
            "users",
            sa.Column("review_applications", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    have = _columns("apply_tasks")
    if "overrides" not in have:
        op.add_column("apply_tasks", sa.Column("overrides", sa.JSON(), nullable=True))
    if "feedback" not in have:
        op.add_column("apply_tasks", sa.Column("feedback", sa.Text(), nullable=True))
    if "approved" not in have:
        op.add_column(
            "apply_tasks",
            sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    op.drop_column("apply_tasks", "approved")
    op.drop_column("apply_tasks", "feedback")
    op.drop_column("apply_tasks", "overrides")
    op.drop_column("users", "review_applications")
