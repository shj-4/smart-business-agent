"""add FK for company_id and retention indexes

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
"""
import sqlalchemy as sa
from alembic import op

revision = "n8o9p0q1r2s3"
down_revision = "m7n8o9p0q1r2"
branch_labels = None
depends_on = None

TABLES = [
    "transactions",
    "notes",
    "tasks",
    "budgets",
    "credit_limits",
    "invoices",
    "bonus_events",
    "employee_bonus_plans",
    "loyalty_accounts",
    "loyalty_configs",
]


def _has_fk(table: str, fk_name: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        fks = insp.get_foreign_keys(table)
        if any(fk.get("name") == fk_name for fk in fks):
            return True
        # Some engines leave FK unnamed but constrained on company_id
        return any(
            fk.get("referred_table") == "companies"
            and "company_id" in (fk.get("constrained_columns") or [])
            for fk in fks
        )
    except Exception:
        return False


def _has_column(table: str, column: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return column in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False


def _has_index(table: str, index: str) -> bool:
    try:
        insp = sa.inspect(op.get_bind())
        return index in {idx["name"] for idx in insp.get_indexes(table)}
    except Exception:
        return False


def upgrade() -> None:
    for table in TABLES:
        fk_name = f"fk_{table}_company_id"
        if _has_fk(table, fk_name):
            continue
        # ensure company_id column exists (m7n8 added it)
        if not _has_column(table, "company_id"):
            continue
        with op.batch_alter_table(table) as batch:
            batch.create_foreign_key(
                fk_name,
                "companies",
                ["company_id"],
                ["id"],
                ondelete="SET NULL",
            )

    # invite_links.token_hash + index
    if not _has_column("invite_links", "token_hash"):
        with op.batch_alter_table("invite_links") as batch:
            batch.add_column(sa.Column("token_hash", sa.String(64), nullable=True))
    if not _has_index("invite_links", "ix_invite_links_token_hash"):
        with op.batch_alter_table("invite_links") as batch:
            batch.create_index("ix_invite_links_token_hash", ["token_hash"])

    # user_prefs.ai_consent / feedback_consent
    if not _has_column("user_prefs", "ai_consent"):
        with op.batch_alter_table("user_prefs") as batch:
            batch.add_column(sa.Column("ai_consent", sa.Boolean(), nullable=False, server_default="1"))
    if not _has_column("user_prefs", "feedback_consent"):
        with op.batch_alter_table("user_prefs") as batch:
            batch.add_column(sa.Column("feedback_consent", sa.Boolean(), nullable=False, server_default="1"))

    # correction_feedback retention index
    if not _has_index("correction_feedback", "ix_correction_feedback_created"):
        with op.batch_alter_table("correction_feedback") as batch:
            batch.create_index("ix_correction_feedback_created", ["created_at"])


def downgrade() -> None:
    # correction_feedback index
    if _has_index("correction_feedback", "ix_correction_feedback_created"):
        with op.batch_alter_table("correction_feedback") as batch:
            batch.drop_index("ix_correction_feedback_created")

    # user_prefs columns
    if _has_column("user_prefs", "feedback_consent"):
        with op.batch_alter_table("user_prefs") as batch:
            batch.drop_column("feedback_consent")
    if _has_column("user_prefs", "ai_consent"):
        with op.batch_alter_table("user_prefs") as batch:
            batch.drop_column("ai_consent")

    # invite_links
    if _has_index("invite_links", "ix_invite_links_token_hash"):
        with op.batch_alter_table("invite_links") as batch:
            batch.drop_index("ix_invite_links_token_hash")
    if _has_column("invite_links", "token_hash"):
        with op.batch_alter_table("invite_links") as batch:
            batch.drop_column("token_hash")

    for table in reversed(TABLES):
        fk_name = f"fk_{table}_company_id"
        if _has_fk(table, fk_name):
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(fk_name, type_="foreignkey")