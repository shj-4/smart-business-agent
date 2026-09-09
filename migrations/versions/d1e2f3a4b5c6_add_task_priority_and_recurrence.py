"""add task priority and recurrence_rule columns

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-09-09 10:00:00.000000

أولويات المهام (high|normal|low) + تكرار المهام (daily|weekly|monthly) لتمكين
المهام المتكررة ("ذكرني كل أسبوع اتصل بالمورد"). الأعمدة نصية بلا تشفير
(تسمّيات رموز قصيرة غير حساسة) — نفس نمط بقية أعمدة metadata.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("priority", sa.String(length=8), nullable=False, server_default="normal"),
    )
    op.add_column("tasks", sa.Column("recurrence_rule", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "recurrence_rule")
    op.drop_column("tasks", "priority")
