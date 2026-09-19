"""add companies, company_members, invite_links

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
"""
import sqlalchemy as sa
from alembic import op

revision = "l6m7n8o9p0q1"
down_revision = "k5l6m7n8o9p0"
branch_labels = None
depends_on = None

_MYSQL = {"mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("business_type", sa.String(32), nullable=True),
        sa.Column("base_currency", sa.String(16), nullable=True),
        sa.Column("owner_telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        **_MYSQL,
    )
    op.create_index("ix_companies_id", "companies", ["id"])
    op.create_index("ix_companies_owner", "companies", ["owner_telegram_user_id"])

    op.create_table(
        "company_members",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="staff"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column("invited_by", sa.BigInteger(), nullable=True),
        sa.Column("joined_at", sa.DateTime(), nullable=True),
        **_MYSQL,
    )
    op.create_index("ix_company_members_company_id", "company_members", ["company_id"])

    op.create_table(
        "invite_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False, server_default="link"),
        sa.Column("role", sa.String(16), nullable=False, server_default="staff"),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("token", name="uq_invite_token"),
        **_MYSQL,
    )
    op.create_index("ix_invite_links_company_id", "invite_links", ["company_id"])


def downgrade() -> None:
    op.drop_table("invite_links")
    op.drop_table("company_members")
    op.drop_table("companies")
