"""Accounts and per-user job state.

Adds password-based login, and moves "what have I done about this job" off the
shared ``jobs`` row into ``user_job_state``. Status was never a fact about a
posting: one person applying to a job must not silence it for everyone else on
the instance.

The existing single user's tracker is copied into the new table before anything
else, so no saved, applied or dismissed decision is lost. The old columns on
``jobs`` are deliberately left in place -- dropping them is a separate step,
after every reader has moved over, so this migration stays reversible.

Revision ID: c2d3e4f5a6b7
Revises: b1f2c3d4e5a6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1f2c3d4e5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_config")),
        sa.UniqueConstraint("key", name=op.f("uq_app_config_key")),
    )
    op.create_index(op.f("ix_app_config_key"), "app_config", ["key"], unique=True)

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))
        batch.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(sa.Column("digest_email", sa.String(length=320), nullable=True))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))

    op.create_table(
        "user_job_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="new"),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("saved_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_job_state_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_user_job_state_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_job_state")),
        sa.UniqueConstraint("user_id", "job_id", name="uq_user_job_state_user_id_job_id"),
    )
    op.create_index(
        op.f("ix_user_job_state_user_id"), "user_job_state", ["user_id"], unique=False
    )
    op.create_index(op.f("ix_user_job_state_job_id"), "user_job_state", ["job_id"], unique=False)
    op.create_index(
        op.f("ix_user_job_state_status"), "user_job_state", ["status"], unique=False
    )
    op.create_index(
        "ix_user_job_state_user_status", "user_job_state", ["user_id", "status"], unique=False
    )

    # The Kanban card is per user for the same reason status is.
    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_applications_user_id_users"),
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.drop_constraint("uq_application_job", type_="unique")
        batch.create_unique_constraint("uq_application_user_job", ["user_id", "job_id"])
    op.create_index(op.f("ix_applications_user_id"), "applications", ["user_id"], unique=False)

    _backfill()


def _backfill() -> None:
    """Copy the existing user's tracker into the new per-user table.

    Only rows that carry a decision or a notification are copied: an untouched
    job needs no state row, which keeps the table proportional to decisions
    made rather than to the thousands of jobs crawled.
    """
    bind = op.get_bind()
    user_id = bind.execute(sa.text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar()
    if user_id is None:
        return  # Fresh install; nothing to carry over.

    bind.execute(
        sa.text(
            """
            INSERT INTO user_job_state
                (user_id, job_id, status, notified, notified_at, created_at, updated_at)
            SELECT
                :user_id,
                j.id,
                COALESCE(j.status, 'new'),
                COALESCE(j.notified, FALSE),
                j.notified_at,
                COALESCE(j.created_at, CURRENT_TIMESTAMP),
                CURRENT_TIMESTAMP
            FROM jobs j
            WHERE COALESCE(j.status, 'new') <> 'new' OR COALESCE(j.notified, FALSE)
            """
        ),
        {"user_id": user_id},
    )

    # Existing cards belong to the only account that could have made them.
    bind.execute(
        sa.text("UPDATE applications SET user_id = :user_id WHERE user_id IS NULL"),
        {"user_id": user_id},
    )


def downgrade() -> None:
    # Push per-user state back onto the shared row for the oldest account, so a
    # downgrade does not silently discard the tracker.
    bind = op.get_bind()
    user_id = bind.execute(sa.text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar()
    if user_id is not None:
        bind.execute(
            sa.text(
                """
                UPDATE jobs SET status = s.status,
                                notified = s.notified,
                                notified_at = s.notified_at
                FROM user_job_state s
                WHERE s.job_id = jobs.id AND s.user_id = :user_id
                """
            )
            if bind.dialect.name != "sqlite"
            else sa.text(
                """
                UPDATE jobs SET
                    status = (SELECT s.status FROM user_job_state s
                              WHERE s.job_id = jobs.id AND s.user_id = :user_id),
                    notified = COALESCE((SELECT s.notified FROM user_job_state s
                              WHERE s.job_id = jobs.id AND s.user_id = :user_id), 0)
                WHERE EXISTS (SELECT 1 FROM user_job_state s
                              WHERE s.job_id = jobs.id AND s.user_id = :user_id)
                """
            ),
            {"user_id": user_id},
        )

    op.drop_index(op.f("ix_applications_user_id"), table_name="applications")
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("uq_application_user_job", type_="unique")
        batch.create_unique_constraint("uq_application_job", ["job_id"])
        batch.drop_column("user_id")

    op.drop_index("ix_user_job_state_user_status", table_name="user_job_state")
    op.drop_index(op.f("ix_user_job_state_status"), table_name="user_job_state")
    op.drop_index(op.f("ix_user_job_state_job_id"), table_name="user_job_state")
    op.drop_index(op.f("ix_user_job_state_user_id"), table_name="user_job_state")
    op.drop_table("user_job_state")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_login_at")
        batch.drop_column("digest_email")
        batch.drop_column("is_active")
        batch.drop_column("password_hash")

    op.drop_index(op.f("ix_app_config_key"), table_name="app_config")
    op.drop_table("app_config")
