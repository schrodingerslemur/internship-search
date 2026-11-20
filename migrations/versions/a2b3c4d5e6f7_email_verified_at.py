"""Accounts confirm their email before they can sign in.

Every account that exists already is treated as verified: they were created
before the check existed, and locking the owner out on deploy would be absurd.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "email_verified_at" in columns:
        return
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE users SET email_verified_at = CURRENT_TIMESTAMP")


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
