"""add notification preference columns to user_prefs

Revision ID: i1j2k3l4m5n6
Revises: h7i8j9k0l1m2
Create Date: 2026-09-16 12:30:00.000000

إعدادات إشعارات لكل مستخدم (كل عمود يُفعَّل افتراضيًا لأي صف قائم أو جديد):
  notif_task_reminder   تذكيرات المهام المتأخرة.
  notif_budget_alert    تنبيهات الميزانيات.
  notif_credit_alert    تنبيهات الحدود الائتمانية.
  notif_invoice_alert   تنبيهات الفواتير الآجلة.
  notif_morning_summary الملخص الصباحي.
  notif_deviation       تنبيهات الانحراف.
  notif_periodic_report التقارير الدورية.
"""

import sqlalchemy as sa
from alembic import op

revision = "i1j2k3l4m5n6"
down_revision = "h7i8j9k0l1m2"
branch_labels = None
depends_on = None

NOTIFICATION_COLUMNS = [
    "notif_task_reminder",
    "notif_budget_alert",
    "notif_credit_alert",
    "notif_invoice_alert",
    "notif_morning_summary",
    "notif_deviation",
    "notif_periodic_report",
]


def upgrade() -> None:
    for col in NOTIFICATION_COLUMNS:
        op.add_column(
            "user_prefs",
            sa.Column(col, sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    for col in NOTIFICATION_COLUMNS:
        op.drop_column("user_prefs", col)
