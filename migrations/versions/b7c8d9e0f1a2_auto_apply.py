"""Auto-apply: the swipe queue and the applicant the cloud worker applies as.

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "apply_tasks" not in tables:
        op.create_table(
            "apply_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("form_key", sa.String(length=200), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("detail", sa.Text()),
            sa.Column("left_for_you", sa.JSON()),
            sa.Column("answers", sa.JSON()),
            sa.Column("screenshot", sa.LargeBinary()),
            sa.Column("queued_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime()),
            sa.Column("finished_at", sa.DateTime()),
            sa.Column("submitted_at", sa.DateTime()),
            *_timestamps(),
            sa.UniqueConstraint("user_id", "job_id", name="uq_apply_task_user_job"),
        )
        op.create_index("ix_apply_tasks_user_id", "apply_tasks", ["user_id"])
        op.create_index("ix_apply_tasks_job_id", "apply_tasks", ["job_id"])
        op.create_index("ix_apply_tasks_status", "apply_tasks", ["status"])
        op.create_index("ix_apply_tasks_submitted_at", "apply_tasks", ["submitted_at"])

    if "applicant_records" not in tables:
        op.create_table(
            "applicant_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("data", sa.JSON()),
            sa.Column("resume_filename", sa.String(length=300)),
            sa.Column("resume_bytes", sa.LargeBinary()),
            *_timestamps(),
        )


def downgrade() -> None:
    op.drop_table("applicant_records")
    op.drop_table("apply_tasks")
