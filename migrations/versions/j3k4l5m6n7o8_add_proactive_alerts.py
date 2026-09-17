"""add notif_proactive column to user_prefs

Revision ID: j3k4l5m6n7o8
Revises: i1j2k3l4m5n6
Create Date: 2026-09-17 09:00:00.000000

مفتاح النشرة الاستباقية اليومية (إشعار مجمّع: مهام/فواتير تستحق قريبًا +
ميزانيات على السقف) — تُفعَّل افتراضيًا لأي صف قائم أو جديد.
"""

import sqlalchemy as sa
from alembic import op

revision = "j3k4l5m6n7o8"
down_revision = "i1j2k3l4m5n6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_prefs",
        sa.Column("notif_proactive", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("user_prefs", "notif_proactive")
