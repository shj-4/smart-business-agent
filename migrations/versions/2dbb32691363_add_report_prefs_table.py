"""add report_prefs table

Revision ID: 2dbb32691363
Revises: 91a6bfa9f488
Create Date: 2026-09-07 20:04:22.502506

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2dbb32691363"
down_revision: str | Sequence[str] | None = "91a6bfa9f488"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """إضافة جدول تفضيلات التقارير الدورية."""
    op.create_table(
        "report_prefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("frequency", sa.String(length=10), nullable=False, server_default="off"),
        sa.Column("deliver_time", sa.String(length=5), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("telegram_user_id", name="uq_report_pref_user"),
    )


def downgrade() -> None:
    """حذف جدول تفضيلات التقارير (عكوس)."""
    op.drop_table("report_prefs")
