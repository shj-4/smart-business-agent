"""add workspace_members table

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-09-07 14:00:00.000000

حساب مشترك: جدول خفيف يربط كل معرّف Telegram بمساحة عمل (مرتكزها معرّف
المالك). القراءات تُنطّق بمجموعة الأعضاء؛ لا يلزم أي عمود جديد في جداول البيانات.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c9d0e1f2a3b4"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_members",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("joined_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("telegram_user_id"),
    )
    op.create_index(
        "ix_workspace_members_workspace_id", "workspace_members", ["workspace_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_members_workspace_id", table_name="workspace_members")
    op.drop_table("workspace_members")
