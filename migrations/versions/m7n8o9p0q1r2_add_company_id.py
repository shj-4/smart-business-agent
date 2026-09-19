"""add company_id to 10 tables

Revision ID: m7n8o9p0q1r2
Revises: l6m7n8o9p0q1
"""
import sqlalchemy as sa
from alembic import op

revision = "m7n8o9p0q1r2"
down_revision = "l6m7n8o9p0q1"
branch_labels = None
depends_on = None

TABLES = [
    "transactions",
    "notes",
    "tasks",
    "budgets",
    "credit_limits",
    "invoices",
    "bonus_events",
    "employee_bonus_plans",
    "loyalty_accounts",
    "loyalty_configs",
]


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("company_id", sa.Integer(), nullable=True))
            batch.create_index(f"ix_{table}_company_id", ["company_id"])


def downgrade() -> None:
    for table in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_company_id")
            batch.drop_column("company_id")
