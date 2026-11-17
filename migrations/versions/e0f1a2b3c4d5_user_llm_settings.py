"""Bring your own model: each user's provider, model and sealed API key.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "user_llm_settings" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "user_llm_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("base_url", sa.Text()),
        sa.Column("key_sealed", sa.Text(), nullable=False),
        sa.Column("key_hint", sa.String(length=12)),
        sa.Column("checked_ok", sa.Boolean()),
        sa.Column("check_detail", sa.String(length=300)),
        sa.Column("checked_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_llm_settings")
