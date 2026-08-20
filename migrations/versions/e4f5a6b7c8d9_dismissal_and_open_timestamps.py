"""Record *when* a job was dismissed, and when its application was opened.

Status alone cannot answer "what did I throw away yesterday?", so the Dismissed
view had nothing sensible to sort by. ``dismissed_at`` gives it one.

``opened_at`` exists to keep an honest distinction the UI depends on: opening an
application URL is not the same as having applied. The card records the open,
then asks; only the answer sets the status.

Existing dismissals are backfilled from ``updated_at``, which for a dismissed
row is the moment the dismissal was written.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None

NEW_COLUMNS = (
    ("dismissed_at", sa.DateTime()),
    ("opened_at", sa.DateTime()),
)


def upgrade() -> None:
    with op.batch_alter_table("user_job_state") as batch:
        for name, type_ in NEW_COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=True))

    # A row whose status is 'dismissed' was last written when it was dismissed,
    # so updated_at is the best available answer and beats leaving it null.
    op.get_bind().execute(
        sa.text(
            "UPDATE user_job_state SET dismissed_at = updated_at "
            "WHERE status = 'dismissed' AND dismissed_at IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("user_job_state") as batch:
        for name, _ in reversed(NEW_COLUMNS):
            batch.drop_column(name)
