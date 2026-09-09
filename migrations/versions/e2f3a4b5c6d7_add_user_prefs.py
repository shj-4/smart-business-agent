"""add user_prefs table

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-09 12:00:00.000000

تفضيلات الواجهة لكل مستخدم (صف واحد لكل معرّف): لغة الواجهة ar/en.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_prefs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("lang", sa.String(length=2), nullable=False, server_default="ar"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id", name="uq_user_pref_user"),
    )
    op.create_index("ix_user_prefs_telegram_user_id", "user_prefs", ["telegram_user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_prefs_telegram_user_id", table_name="user_prefs")
    op.drop_table("user_prefs")
