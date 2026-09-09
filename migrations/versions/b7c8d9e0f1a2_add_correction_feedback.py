"""add correction_feedback table

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-09-07 12:00:00.000000

سجل "التحليل الخاطئ": تُخزَّن الرسائل التي ألغاها المستخدم (/cancel) أو رفض
تأكيدها — لمراجعة يدوية دورية وتحسين الـ prompt.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c8d9e0f1a2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "correction_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("raw_message", sa.Text(), nullable=True),
        sa.Column("data_type", sa.String(length=16), nullable=True),
        sa.Column("reviewed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_correction_feedback_id", "correction_feedback", ["id"], unique=False)
    op.create_index(
        "ix_correction_feedback_telegram_user_id",
        "correction_feedback",
        ["telegram_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_correction_feedback_telegram_user_id", table_name="correction_feedback")
    op.drop_index("ix_correction_feedback_id", table_name="correction_feedback")
    op.drop_table("correction_feedback")
