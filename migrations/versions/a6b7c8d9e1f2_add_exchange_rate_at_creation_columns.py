"""add exchange rate at creation columns

Revision ID: a6b7c8d9e1f2
Revises: e2f3a4b5c6d7
Create Date: 2026-09-09 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6b7c8d9e1f2"
down_revision: str | Sequence[str] | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """إضافة أعمدة سعر الصرف المثبَّت وقت التسجيل على جدول المعاملات.

    - amount_in_base_currency: المبلغ محوّلًا بعملة الأساس لحظة إنشاء العملية
      (يفضَّل على الحساب اللحظي بسعر اليوم في التقارير التاريخية).
    - base_currency_at_creation: العملة الأساس المعتمدة وقت الحساب.
    nullable=True — عملية قديمة أو فشل تحويل في لحظة التسجيل تعني إعادة الحساب الحي.
    """
    op.add_column(
        "transactions",
        sa.Column("amount_in_base_currency", sa.Text(), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("base_currency_at_creation", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    """حذف أعمدة سعر الصرف المثبَّت (عكوس)."""
    op.drop_column("transactions", "base_currency_at_creation")
    op.drop_column("transactions", "amount_in_base_currency")
