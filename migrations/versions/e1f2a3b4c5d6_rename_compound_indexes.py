"""rename compound unique indexes to match model constraint names

نسق أسماء الفهارس المركبة الفريدة مع أسماء UniqueConstraint في النماذج
(uq_<table>_user_message) حتى يتطابق المخطط مع autogenerate في المستقبل.

Revision ID: e1f2a3b4c5d6
Revises: d5e6f7a8b9c0
Create Date: 2026-09-06 19:25:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("transactions", "notes", "tasks")


def upgrade() -> None:
    for table in TABLES:
        op.drop_index(f"ix_{table}_user_message_id", table_name=table)
        op.create_index(
            f"uq_{table}_user_message",
            table,
            ["telegram_user_id", "telegram_message_id"],
            unique=True,
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_index(f"uq_{table}_user_message", table_name=table)
        op.create_index(
            f"ix_{table}_user_message_id",
            table,
            ["telegram_user_id", "telegram_message_id"],
            unique=True,
        )
