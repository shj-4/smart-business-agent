"""add parties and period_closes tables (missing from prior migrations)

Revision ID: p9q0r1s2t3u4
Revises: 208c11c4f276
"""
import sqlalchemy as sa
from alembic import op

revision = "p9q0r1s2t3u4"
down_revision = "208c11c4f276"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "parties" not in tables:
        op.create_table(
            "parties",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("party_type", sa.String(16), nullable=True),
            sa.Column("phone", sa.String(64), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("telegram_user_id", "name", name="uq_party_user_name"),
        )
        with op.batch_alter_table("parties") as batch:
            batch.create_index("ix_parties_id", ["id"])
            batch.create_index("ix_parties_telegram_user_id", ["telegram_user_id"])
            batch.create_index("ix_parties_company_id", ["company_id"])

    if "period_closes" not in tables:
        op.create_table(
            "period_closes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True),
            sa.Column("month_key", sa.String(7), nullable=False),
            sa.Column("closed_by", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("company_id", "month_key", name="uq_period_close_company_month"),
        )
        with op.batch_alter_table("period_closes") as batch:
            batch.create_index("ix_period_closes_id", ["id"])
            batch.create_index("ix_period_closes_telegram_user_id", ["telegram_user_id"])
            batch.create_index("ix_period_closes_company_id", ["company_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "period_closes" in tables:
        op.drop_table("period_closes")
    if "parties" in tables:
        op.drop_table("parties")
