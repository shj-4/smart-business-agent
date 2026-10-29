"""add direction column to invoices

Revision ID: q0r1s2t3u4v5
Revises: p9q0r1s2t3u4
"""
import sqlalchemy as sa
from alembic import op

revision = "q0r1s2t3u4v5"
down_revision = "p9q0r1s2t3u4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("invoices")}
    if "direction" not in cols:
        with op.batch_alter_table("invoices") as batch:
            batch.add_column(sa.Column("direction", sa.String(16), nullable=False, server_default="payable"))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("invoices")}
    if "direction" in cols:
        with op.batch_alter_table("invoices") as batch:
            batch.drop_column("direction")
