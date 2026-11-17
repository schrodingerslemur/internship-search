"""Auto-apply on any format: the agent's start URL, and its how-to per format.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "apply_url" not in {c["name"] for c in inspector.get_columns("apply_tasks")}:
        op.add_column("apply_tasks", sa.Column("apply_url", sa.Text(), nullable=True))
    if "apply_recipes" in set(inspector.get_table_names()):
        return
    op.create_table(
        "apply_recipes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("format_key", sa.String(length=120), nullable=False, unique=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("successes", sa.Integer()),
        sa.Column("failures", sa.Integer()),
        sa.Column("last_outcome", sa.String(length=20)),
        sa.Column("last_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("apply_recipes")
    op.drop_column("apply_tasks", "apply_url")
