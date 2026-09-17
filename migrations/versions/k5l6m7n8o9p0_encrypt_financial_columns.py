"""encrypt non-transaction financial columns (5 tables)

Revision ID: k5l6m7n8o9p0
Revises: j3k4l5m6n7o8
Create Date: 2026-09-17 11:30:00.000000

تشفير الحقول المالية خارج جدول المعاملات:
- invoices.amount
- budgets.monthly_limit
- credit_limits.limit_amount
- employee_bonus_plans.amount
- bonus_events.budget

LoyaltyConfig.points_value مُستبعد لأن EncryptedNumeric يُكمّل إلى
0.01 (خسارة دقة الحقول العشرية 6 خانات) — يبقى نصًا واضحًا.

القيم المخزّنة حاليًا كأرقام في SQLite تبقى مقروءة بعد التحويل:
EncryptedNumeric يقرأ القيم القديمة كنص واضح (Decimal(str(value))) ويعيد
Decimal موحّدًا عند القراءة — لا فقدان بيانات.

⚠️ النسخة الاحتياطية من data/business.db يجب أن تتم قبل تنفيذ هذا المهاجر.
"""

import sqlalchemy as sa
from alembic import op

revision = "k5l6m7n8o9p0"
down_revision = "j3k4l5m6n7o8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # invoices.amount: NOT NULL
    # recreate="auto" (الافتراضي) يعيد بناء الجدول على SQLite عند تغيير النوع
    with op.batch_alter_table("invoices") as batch:
        batch.alter_column(
            "amount",
            existing_type=sa.Numeric(12, 2),
            type_=sa.Text(),
            existing_nullable=False,
        )

    # budgets.monthly_limit: NOT NULL
    with op.batch_alter_table("budgets") as batch:
        batch.alter_column(
            "monthly_limit",
            existing_type=sa.Numeric(12, 2),
            type_=sa.Text(),
            existing_nullable=False,
        )

    # credit_limits.limit_amount: NOT NULL
    with op.batch_alter_table("credit_limits") as batch:
        batch.alter_column(
            "limit_amount",
            existing_type=sa.Numeric(12, 2),
            type_=sa.Text(),
            existing_nullable=False,
        )

    # employee_bonus_plans.amount: NOT NULL
    with op.batch_alter_table("employee_bonus_plans") as batch:
        batch.alter_column(
            "amount",
            existing_type=sa.Numeric(12, 2),
            type_=sa.Text(),
            existing_nullable=False,
        )

    # bonus_events.budget: NULLABLE
    with op.batch_alter_table("bonus_events") as batch:
        batch.alter_column(
            "budget",
            existing_type=sa.Numeric(12, 2),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("bonus_events") as batch:
        batch.alter_column(
            "budget",
            existing_type=sa.Text(),
            type_=sa.Numeric(12, 2),
            existing_nullable=True,
        )
    with op.batch_alter_table("employee_bonus_plans") as batch:
        batch.alter_column(
            "amount",
            existing_type=sa.Text(),
            type_=sa.Numeric(12, 2),
            existing_nullable=False,
        )
    with op.batch_alter_table("credit_limits") as batch:
        batch.alter_column(
            "limit_amount",
            existing_type=sa.Text(),
            type_=sa.Numeric(12, 2),
            existing_nullable=False,
        )
    with op.batch_alter_table("budgets") as batch:
        batch.alter_column(
            "monthly_limit",
            existing_type=sa.Text(),
            type_=sa.Numeric(12, 2),
            existing_nullable=False,
        )
    with op.batch_alter_table("invoices") as batch:
        batch.alter_column(
            "amount",
            existing_type=sa.Text(),
            type_=sa.Numeric(12, 2),
            existing_nullable=False,
        )
