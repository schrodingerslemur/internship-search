"""Emails the owner allows to sign up, edited on /admin.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "allowed_emails" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "allowed_emails",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry", sa.String(length=320), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("allowed_emails")
