"""add transactions composite index (user, created_at)

Revision ID: a1b2c3d4e5f6
Revises: 2dbb32691363
Create Date: 2026-09-07 00:00:00.000000

تسريع استعلامات الفترات الزمنية المتكررة (المبالغ المشفّرة تُجمع في Python،
لكن انتقاء الصفوف نفسها يستفيد من الفهرس المركّب telegram_user_id+created_at).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "2dbb32691363"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_transactions_user_created",
        "transactions",
        ["telegram_user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_user_created", table_name="transactions")
