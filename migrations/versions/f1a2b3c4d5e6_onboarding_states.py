"""A new account's onboarding chat and the draft it builds.

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "onboarding_states" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "onboarding_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("messages", sa.JSON()),
        sa.Column("draft", sa.JSON()),
        sa.Column("step", sa.String(length=20)),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("onboarding_states")
