"""store active Telegram main menu references

Revision ID: s2t3u4v5w6x7
Revises: r1s2t3u4v5w6
"""
import sqlalchemy as sa
from alembic import op

revision = "s2t3u4v5w6x7"
down_revision = "r1s2t3u4v5w6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "active_menus",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("chat_id", "telegram_user_id", name="uq_active_menu_chat_user"),
    )
    op.create_index("ix_active_menus_id", "active_menus", ["id"])
    op.create_index("ix_active_menus_chat_id", "active_menus", ["chat_id"])
    op.create_index("ix_active_menus_telegram_user_id", "active_menus", ["telegram_user_id"])
    op.create_table(
        "obsolete_menus",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("chat_id", "telegram_user_id", "message_id", name="uq_obsolete_menu_ref"),
    )
    op.create_index("ix_obsolete_menus_id", "obsolete_menus", ["id"])
    op.create_index("ix_obsolete_menus_chat_id", "obsolete_menus", ["chat_id"])
    op.create_index("ix_obsolete_menus_telegram_user_id", "obsolete_menus", ["telegram_user_id"])


def downgrade() -> None:
    op.drop_index("ix_obsolete_menus_id", table_name="obsolete_menus")
    op.drop_index("ix_obsolete_menus_telegram_user_id", table_name="obsolete_menus")
    op.drop_index("ix_obsolete_menus_chat_id", table_name="obsolete_menus")
    op.drop_table("obsolete_menus")
    op.drop_index("ix_active_menus_id", table_name="active_menus")
    op.drop_index("ix_active_menus_telegram_user_id", table_name="active_menus")
    op.drop_index("ix_active_menus_chat_id", table_name="active_menus")
    op.drop_table("active_menus")
