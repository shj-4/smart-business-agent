"""add workspace_members.status (pending/active consent gate)

Revision ID: g2a1b3c4d5e6
Revises: f0e1d2c3b4a5
Create Date: 2026-09-12 10:00:00.000000

بوابة موافقة: كل دعوة جديدة تُنشأ بحالة "pending" ولا تمنح أي وصول حتى يقبلها
الطرف المدعو (accept_workspace_invite). الصفوف القائمة (قبل هذا الحقل) تصبح
"active" — الترتيبات الحالية تُحافَظ كما هي.
"""

import sqlalchemy as sa
from alembic import op

revision = "g2a1b3c4d5e6"
down_revision = "f0e1d2c3b4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_members",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )


def downgrade() -> None:
    op.drop_column("workspace_members", "status")
