"""make telegram_message_id unique only per user (compound index)

message_id في تيليجرام تسلسلي لكل محادثة وليس فريدًا عالميًا.
القيد الفريد السابق على العمود وحده يسبب IntegrityError عند مطابقة
message_id بين مستخدمين مختلفين. نستبدله بقيد مركّب (telegram_user_id, telegram_message_id).

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a7b8
Create Date: 2026-09-06 19:20:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("transactions", "notes", "tasks"):
        op.drop_index(f"ix_{table}_telegram_message_id", table_name=table)
        op.create_index(
            f"ix_{table}_user_message_id",
            table,
            ["telegram_user_id", "telegram_message_id"],
            unique=True,
        )


def downgrade() -> None:
    for table in ("transactions", "notes", "tasks"):
        op.drop_index(f"ix_{table}_user_message_id", table_name=table)
        op.create_index(
            f"ix_{table}_telegram_message_id",
            table,
            ["telegram_message_id"],
            unique=True,
        )
