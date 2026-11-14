"""Auto-apply: documents beyond the resume (transcript, cover letter, ...).

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "applicant_documents" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "applicant_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("labels", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "kind", name="uq_applicant_document_kind"),
    )
    op.create_index("ix_applicant_documents_user_id", "applicant_documents", ["user_id"])


def downgrade() -> None:
    op.drop_table("applicant_documents")
