"""Per-user relevance scoring.

A score is a claim about the fit between a job and a person, so it cannot be a
column on the job. Two people with different profiles must be able to look at
the same posting and see different numbers -- and different digests.

The existing single user's scores are copied across, so nobody's ranking resets
to zero on upgrade. ``jobs.relevance_score`` stays as the shared fallback for a
job nobody has been scored against yet.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None

NEW_COLUMNS = (
    ("relevance_score", sa.Float()),
    ("priority", sa.String(length=30)),
    ("match_reasons", sa.JSON()),
    ("concerns", sa.JSON()),
    ("missing_requirements", sa.JSON()),
    ("score_breakdown", sa.JSON()),
    ("scored_at", sa.DateTime()),
)


def upgrade() -> None:
    with op.batch_alter_table("user_job_state") as batch:
        for name, type_ in NEW_COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=True))
    op.create_index(
        op.f("ix_user_job_state_priority"), "user_job_state", ["priority"], unique=False
    )
    op.create_index(
        "ix_user_job_state_user_score", "user_job_state", ["user_id", "relevance_score"]
    )

    # Carry the existing user's rankings over, so the first run after the
    # upgrade is not a blank slate.
    bind = op.get_bind()
    user_id = bind.execute(sa.text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar()
    if user_id is None:
        return
    bind.execute(
        sa.text(
            """
            UPDATE user_job_state SET
                relevance_score = (SELECT j.relevance_score FROM jobs j WHERE j.id = job_id),
                priority        = (SELECT j.priority        FROM jobs j WHERE j.id = job_id)
            WHERE user_id = :user_id
            """
        ),
        {"user_id": user_id},
    )


def downgrade() -> None:
    op.drop_index("ix_user_job_state_user_score", table_name="user_job_state")
    op.drop_index(op.f("ix_user_job_state_priority"), table_name="user_job_state")
    with op.batch_alter_table("user_job_state") as batch:
        for name, _ in reversed(NEW_COLUMNS):
            batch.drop_column(name)
