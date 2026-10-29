"""add FK for company_id and retention indexes

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
"""
import sqlalchemy as sa
from alembic import op

revision = "n8o9p0q1r2s3"
down_revision = "m7n8o9p0q1r2"
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
            batch.create_foreign_key(
                f"fk_{table}_company_id",
                "companies",
                ["company_id"],
                ["id"],
                ondelete="SET NULL",
            )
    with op.batch_alter_table("invite_links") as batch:
        batch.add_column(sa.Column("token_hash", sa.String(64), nullable=True))
        batch.create_index("ix_invite_links_token_hash", ["token_hash"])
    with op.batch_alter_table("user_prefs") as batch:
        batch.add_column(sa.Column("ai_consent", sa.Boolean, nullable=False, server_default="1"))
        batch.add_column(sa.Column("feedback_consent", sa.Boolean, nullable=False, server_default="1"))
    with op.batch_alter_table("correction_feedback") as batch:
        batch.create_index("ix_correction_feedback_created", ["created_at"])


def downgrade() -> None:
    with op.batch_alter_table("correction_feedback") as batch:
        batch.drop_index("ix_correction_feedback_created")
    with op.batch_alter_table("user_prefs") as batch:
        batch.drop_column("feedback_consent")
        batch.drop_column("ai_consent")
    with op.batch_alter_table("invite_links") as batch:
        batch.drop_index("ix_invite_links_token_hash")
        batch.drop_column("token_hash")
    for table in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_company_id", type_="foreignkey")
