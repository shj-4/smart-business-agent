"""add last_alert_month to credit_limits

Revision ID: r1s2t3u4v5w6
Revises: q0r1s2t3u4v5
"""
import sqlalchemy as sa
from alembic import op

revision = "r1s2t3u4v5w6"
down_revision = "q0r1s2t3u4v5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("credit_limits")}
    if "last_alert_month" not in cols:
        with op.batch_alter_table("credit_limits") as batch:
            batch.add_column(sa.Column("last_alert_month", sa.String(7), nullable=False, server_default=""))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("credit_limits")}
    if "last_alert_month" in cols:
        with op.batch_alter_table("credit_limits") as batch:
            batch.drop_column("last_alert_month")
