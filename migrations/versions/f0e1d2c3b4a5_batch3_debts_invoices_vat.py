"""batch3: debts, invoices, credit limits, order status, VAT fields

Revision ID: f0e1d2c3b4a5
Revises: a6b7c8d9e1f2
Create Date: 2026-09-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0e1d2c3b4a5"
down_revision: str | Sequence[str] | None = "a6b7c8d9e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """إضافة حقول الضريبة، حالة الطلبية، نطاق تصنيف للميزانيات، وجدولَي
    الفواتير الآجلة والحدود الائتمانية."""
    op.add_column("transactions", sa.Column("vat_rate", sa.Numeric(6, 3), nullable=True))
    op.add_column("transactions", sa.Column("vat_amount", sa.Numeric(12, 2), nullable=True))

    op.add_column("notes", sa.Column("status", sa.String(length=16), nullable=True))

    # scope=category للميزانيات: عمود + قيد فريد (batch يدعم SQLite عبر إعادة إنشاء)
    with op.batch_alter_table("budgets") as batch_op:
        batch_op.add_column(sa.Column("category", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint(
            "uq_budget_user_scope_category", ["telegram_user_id", "scope", "category"]
        )

    op.create_table(
        "credit_limits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("person", sa.String(length=255), nullable=False),
        sa.Column("limit_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("alerted_status", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("telegram_user_id", "person", name="uq_credit_user_person"),
    )
    op.create_index("ix_credit_limits_user_id", "credit_limits", ["telegram_user_id"])

    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("person", sa.String(length=255), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_date", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("alerted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_invoices_user_id", "invoices", ["telegram_user_id"])


def downgrade() -> None:
    """حذف إضافات الدفعة الثالثة (عكوس)."""
    op.drop_index("ix_invoices_user_id", table_name="invoices")
    op.drop_table("invoices")
    op.drop_index("ix_credit_limits_user_id", table_name="credit_limits")
    op.drop_table("credit_limits")

    with op.batch_alter_table("budgets") as batch_op:
        batch_op.drop_constraint("uq_budget_user_scope_category", type_="unique")
        batch_op.drop_column("category")

    op.drop_column("notes", "status")
    op.drop_column("transactions", "vat_amount")
    op.drop_column("transactions", "vat_rate")
