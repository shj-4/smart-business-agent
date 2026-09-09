"""add category columns and budgets table

Revision ID: 91a6bfa9f488
Revises: 543d7abac058
Create Date: 2026-09-07 19:57:21.571008

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "91a6bfa9f488"
down_revision: str | Sequence[str] | None = "543d7abac058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """إضافة عمود التصنيف للمعاملات والملاحظات وجدول الميزانيات الشهرية."""
    op.add_column("transactions", sa.Column("category", sa.String(length=80), nullable=True))
    op.add_column("notes", sa.Column("category", sa.String(length=80), nullable=True))
    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=True),
        sa.Column("scope", sa.String(length=10), nullable=False),  # currency | person
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("person", sa.String(length=80), nullable=True),
        sa.Column("monthly_limit", sa.Numeric(12, 2), nullable=False),
        sa.Column("alerted_status", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("month_key", sa.String(length=7), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "telegram_user_id",
            "scope",
            "currency",
            name="uq_budget_user_scope_currency",
        ),
        sa.UniqueConstraint(
            "telegram_user_id",
            "scope",
            "person",
            name="uq_budget_user_scope_person",
        ),
    )


def downgrade() -> None:
    """حذف جدول الميزانيات وأعمدة التصنيف (عكوس)."""
    op.drop_table("budgets")
    op.drop_column("notes", "category")
    op.drop_column("transactions", "category")
