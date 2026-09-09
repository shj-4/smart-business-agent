"""add updated_at and reminder_sent columns

Revision ID: 543d7abac058
Revises: e1f2a3b4c5d6
Create Date: 2026-09-07 19:42:47.276055

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "543d7abac058"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """إضافة أعمدة التعديل (updated_at) وتذكير المهام (reminder_sent)."""
    # تنظيف جدول مؤقت قديم تركه batch_alter_table في ترحيل سابق (فارغ)
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_tasks")

    op.add_column("notes", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.add_column("tasks", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.add_column("transactions", sa.Column("updated_at", sa.DateTime(), nullable=True))
    # reminder_sent غير nullable مع default 0 (لصفوف موجودة مسبقًا في SQLite)
    op.add_column(
        "tasks",
        sa.Column("reminder_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """esحذف الأعمدة المضافة (عكوس)."""
    op.drop_column("tasks", "reminder_sent")
    op.drop_column("tasks", "updated_at")
    op.drop_column("notes", "updated_at")
    op.drop_column("transactions", "updated_at")
