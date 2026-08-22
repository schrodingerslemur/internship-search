"""Capture decision signal: dismissal reasons, score snapshots, impressions.

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0

Three additions, all in service of one thing: making decisions learnable later.

``dismiss_reason`` turns a "no" into a reason for it. Six one-tap options that
imply opposite corrections to the ranking, so a dismissal without one is nearly
unlearnable.

``score_at_save`` / ``score_at_dismiss`` snapshot the score at the moment of the
decision, matching what ``applications.score_at_apply`` already does. Re-scoring
rewrites ``relevance_score`` in place, so without these every past decision
silently re-attributes itself to whatever the ranker believes today.

``feed_impressions`` records what was actually shown. It is the denominator:
precision@k is undefined without it, and it is the only honest measure of
whether the ranking is improving.

None of this is retroactive -- signal that was not captured is gone -- which is
why it lands before the layers that consume it rather than alongside them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a6b7c8d9e0f1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None

STATE_COLUMNS = (
    ("dismiss_reason", sa.String(length=40)),
    ("score_at_save", sa.Float()),
    ("score_at_dismiss", sa.Float()),
)


def _columns(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # Adding a column that is already there aborts the whole migration, and a
    # database that has been through a manual fix-up is the normal case here.
    present = _columns(bind, "user_job_state")
    for name, type_ in STATE_COLUMNS:
        if name not in present:
            op.add_column("user_job_state", sa.Column(name, type_, nullable=True))

    if "dismiss_reason" not in present:
        op.create_index(
            "ix_user_job_state_dismiss_reason", "user_job_state", ["dismiss_reason"]
        )

    if "feed_impressions" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "feed_impressions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("view", sa.String(length=30), nullable=False, server_default="review"),
            sa.Column("shown_at", sa.DateTime(), nullable=False),
            sa.Column("job_ids", sa.JSON(), nullable=True),
            sa.Column("scores", sa.JSON(), nullable=True),
            sa.Column("total_available", sa.Integer(), nullable=True, server_default="0"),
        )
        op.create_index("ix_feed_impressions_user_id", "feed_impressions", ["user_id"])
        op.create_index("ix_feed_impressions_shown_at", "feed_impressions", ["shown_at"])
        op.create_index(
            "ix_feed_impressions_user_time", "feed_impressions", ["user_id", "shown_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()

    if "feed_impressions" in sa.inspect(bind).get_table_names():
        op.drop_table("feed_impressions")

    present = _columns(bind, "user_job_state")
    if "dismiss_reason" in present:
        op.drop_index("ix_user_job_state_dismiss_reason", table_name="user_job_state")
    for name, _ in reversed(STATE_COLUMNS):
        if name in present:
            op.drop_column("user_job_state", name)
