"""add bonus events, employee bonus plans, loyalty accounts/configs

Revision ID: h7i8j9k0l1m2
Revises: g2a1b3c4d5e6
Create Date: 2026-09-16 12:00:00.000000

ميزة «حدث البون» — أربعة جداول جديدة:
  bonus_events        فعاليات ترويجية (فترة + ميزانية بونس).
  employee_bonus_plans خطط مكافآت موظفين دورية/ثابتة مع موعد استحقاق وسقف شهري.
  loyalty_accounts    محافظ نقاط ولاء للعملاء (رصيد/مكتسب/مستهلَك).
  loyalty_configs     إعدادات نقاط الولاء لكل مستخدم (معدّل النقاط وقيمتها).
"""

import sqlalchemy as sa
from alembic import op

revision = "h7i8j9k0l1m2"
down_revision = "g2a1b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bonus_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("start_at", sa.DateTime(), nullable=True),
        sa.Column("end_at", sa.DateTime(), nullable=True),
        sa.Column("budget", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_bonus_events_user_id", "bonus_events", ["telegram_user_id"])

    op.create_table(
        "employee_bonus_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("person", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("frequency", sa.String(length=16), nullable=False, server_default="monthly"),
        sa.Column("next_due_at", sa.DateTime(), nullable=True),
        sa.Column("monthly_cap", sa.Numeric(12, 2), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_employee_bonus_plans_user", "employee_bonus_plans", ["telegram_user_id"])

    op.create_table(
        "loyalty_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("person", sa.String(length=255), nullable=False),
        sa.Column("points_balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_earned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_redeemed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("telegram_user_id", "person", name="uq_loyalty_user_person"),
    )
    op.create_index("ix_loyalty_accounts_user_id", "loyalty_accounts", ["telegram_user_id"])

    op.create_table(
        "loyalty_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("points_rate", sa.Numeric(12, 4), nullable=False, server_default="1"),
        sa.Column("points_value", sa.Numeric(12, 6), nullable=False, server_default="0.01"),
        sa.Column("min_redeem_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("telegram_user_id", name="uq_loyalty_user"),
    )
    op.create_index("ix_loyalty_configs_user_id", "loyalty_configs", ["telegram_user_id"])


def downgrade() -> None:
    op.drop_index("ix_loyalty_configs_user_id", table_name="loyalty_configs")
    op.drop_table("loyalty_configs")
    op.drop_index("ix_loyalty_accounts_user_id", table_name="loyalty_accounts")
    op.drop_table("loyalty_accounts")
    op.drop_index("ix_employee_bonus_plans_user", table_name="employee_bonus_plans")
    op.drop_table("employee_bonus_plans")
    op.drop_index("ix_bonus_events_user_id", table_name="bonus_events")
    op.drop_table("bonus_events")
