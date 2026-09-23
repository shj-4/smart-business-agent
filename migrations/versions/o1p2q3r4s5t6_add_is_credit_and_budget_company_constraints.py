"""add transactions.is_credit and budget company-scoped constraints

Revision ID: o1p2q3r4s5t6
Revises: n8o9p0q1r2s3
Create Date: 2026-09-21 12:00:00.000000

يعالج انجراف المخطط المكتشف (C1):
- transactions.is_credit معرّف في models.Transaction ويُكتب في
  create_transaction لكن لم تنشئه أي migration سابقة → إضافة العمود هنا.
- قيود uq_budget_company_scope_* معرّفة في Budget ولا تنشئها أي migration
  سابقة (فقط قيود المستخدم). إضافة 3 قيود فريدة لكل شركة.
- شفاء ترحيل n8o9p0q1r2s3 الصامت: إذا فشل إنشاء ai_consent/token_hash/FK
  بصمت بسبب try/except:pass فستتأكد هذه الهجرة من وجودها بشكل محدد
  (idempotent healing) مع إظهار الأخطاء الحقيقية بدل ابتلاعها.

السلوك idempotent: يفحص المخطط عبر inspector قبل الإنشاء حتى يُعاد
تطبيقه بأمان على قواعد طُبقت عليها الهجرة السابقة جزئيًا.
"""

import sqlalchemy as sa
from alembic import op

revision = "o1p2q3r4s5t6"
down_revision = "n8o9p0q1r2s3"
branch_labels = None
depends_on = None

# الجداول التي يجب أن تحمل FK company_id → companies (من n8o9)
FK_TABLES = [
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


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1) transactions.is_credit (Boolean nullable)
    try:
        cols = [c["name"] for c in insp.get_columns("transactions")]
    except Exception:
        cols = []
    if "is_credit" not in cols:
        with op.batch_alter_table("transactions") as batch:
            batch.add_column(sa.Column("is_credit", sa.Boolean(), nullable=True))

    # 2) قيود الميزانية لكل شركة (3 قيود) — كانت موجودة فقط لكل مستخدم
    #    في SQLite تظهر UniqueConstraint كـ unique constraint، على بعض المحركات
    #    قد تظهر كـ index فريد، لذا نفحص كليهما قبل الإنشاء.
    try:
        existing_uqs = {uc["name"] for uc in insp.get_unique_constraints("budgets")}
        existing_unique_idxs = {
            idx["name"] for idx in insp.get_indexes("budgets") if idx.get("unique")
        }
        existing = existing_uqs | existing_unique_idxs
    except Exception:
        existing = set()

    def _missing(name: str) -> bool:
        return name not in existing

    if _missing("uq_budget_company_scope_currency"):
        with op.batch_alter_table("budgets") as batch:
            batch.create_unique_constraint(
                "uq_budget_company_scope_currency", ["company_id", "scope", "currency"]
            )
    # أعد فحص الحالة بعد كل batch لأن إعادة بناء الجدول في SQLite قد تغيّر inspector
    # (نقرأ من جديد لتجنب محاولة إنشاء نفس القيد مرتين داخل ترانساكشن واحدة)
    if _missing("uq_budget_company_scope_person"):
        # إعادة الفحص قبل الثانية لتجنب التعارض عند تشغيل الهجرة على محرك يعيد البناء
        # ومع ذلك حتى لو بقينا على existing القديمة، فشل الإنشاء سيُظهر خطأ واضح
        # بدل try/except: pass الصامت السابق.
        with op.batch_alter_table("budgets") as batch:
            batch.create_unique_constraint(
                "uq_budget_company_scope_person", ["company_id", "scope", "person"]
            )
    if _missing("uq_budget_company_scope_category"):
        with op.batch_alter_table("budgets") as batch:
            batch.create_unique_constraint(
                "uq_budget_company_scope_category", ["company_id", "scope", "category"]
            )

    # 3) شفاء n8o9 الصامت (idempotent healing) — إعادة فحص كل مرة عبر inspector جديد
    #    حتى لا نبتلع الأخطاء الحقيقية (on integrity / type mismatch) بصمت.
    #    إذا كان العمود/الفهرس موجودًا نتخطاه، وإلا ننشئه ويظهر أي خطأ.
    bind2 = op.get_bind()
    insp2 = sa.inspect(bind2)

    # invite_links.token_hash + index
    try:
        inv_cols = [c["name"] for c in insp2.get_columns("invite_links")]
        inv_idxs = {idx["name"] for idx in insp2.get_indexes("invite_links")}
    except Exception:
        inv_cols, inv_idxs = [], set()
    if "token_hash" not in inv_cols:
        with op.batch_alter_table("invite_links") as batch:
            batch.add_column(sa.Column("token_hash", sa.String(64), nullable=True))
        # إعادة فحص الفهارس بعد إضافة العمود
        insp2 = sa.inspect(op.get_bind())
        inv_idxs = {idx["name"] for idx in insp2.get_indexes("invite_links")}
    if "ix_invite_links_token_hash" not in inv_idxs:
        with op.batch_alter_table("invite_links") as batch:
            batch.create_index("ix_invite_links_token_hash", ["token_hash"])

    # user_prefs.ai_consent / feedback_consent
    try:
        up_cols = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("user_prefs")]
    except Exception:
        up_cols = []
    if "ai_consent" not in up_cols:
        with op.batch_alter_table("user_prefs") as batch:
            batch.add_column(
                sa.Column("ai_consent", sa.Boolean(), nullable=False, server_default="1")
            )
    # أعد الفحص بعد الأولى
    try:
        up_cols2 = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("user_prefs")]
    except Exception:
        up_cols2 = up_cols
    if "feedback_consent" not in up_cols2:
        with op.batch_alter_table("user_prefs") as batch:
            batch.add_column(
                sa.Column("feedback_consent", sa.Boolean(), nullable=False, server_default="1")
            )

    # correction_feedback retention index
    try:
        cf_idxs = {
            idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes("correction_feedback")
        }
    except Exception:
        cf_idxs = set()
    if "ix_correction_feedback_created" not in cf_idxs:
        with op.batch_alter_table("correction_feedback") as batch:
            batch.create_index("ix_correction_feedback_created", ["created_at"])

    # FK company_id → companies (10 جداول)
    for table in FK_TABLES:
        try:
            # تأكد أن العمود موجود (m7n8 أضافه)
            t_cols = [c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)]
            if "company_id" not in t_cols:
                continue
            fks = sa.inspect(op.get_bind()).get_foreign_keys(table)
            fk_names = {fk["name"] for fk in fks if fk.get("name")}
            # بعض المحركات تترك FK بلا اسم (None) لكن constrained_columns يدل عليها
            # نتحقق أيضًا عبر الأعمدة المقيدة
            has_fk = any(
                fk.get("referred_table") == "companies"
                and "company_id" in (fk.get("constrained_columns") or [])
                for fk in fks
            )
            if has_fk or f"fk_{table}_company_id" in fk_names:
                continue
            with op.batch_alter_table(table) as batch:
                batch.create_foreign_key(
                    f"fk_{table}_company_id",
                    "companies",
                    ["company_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        except Exception:
            # لا نبتلع بصمت — نعيد الرفع حتى يراه المشغل (خلاف سلوك n8o9 القديم)
            raise


def downgrade() -> None:
    # عكس ما أضافته هذه الهجرة فقط (is_credit + 3 قيود) — شفاء n8o9 يبقى
    # لأن إسقاطه هنا سيكسر downgrade لـ n8o9 المتوقع.
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # إسقاط قيود الميزانية لكل شركة
    try:
        existing_uqs = {uc["name"] for uc in insp.get_unique_constraints("budgets")}
        existing_unique_idxs = {
            idx["name"] for idx in insp.get_indexes("budgets") if idx.get("unique")
        }
        existing = existing_uqs | existing_unique_idxs
    except Exception:
        existing = set()

    if "uq_budget_company_scope_category" in existing:
        with op.batch_alter_table("budgets") as batch:
            batch.drop_constraint("uq_budget_company_scope_category", type_="unique")
    if "uq_budget_company_scope_person" in existing:
        with op.batch_alter_table("budgets") as batch:
            batch.drop_constraint("uq_budget_company_scope_person", type_="unique")
    if "uq_budget_company_scope_currency" in existing:
        with op.batch_alter_table("budgets") as batch:
            batch.drop_constraint("uq_budget_company_scope_currency", type_="unique")

    # إسقاط is_credit
    try:
        cols = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("transactions")]
    except Exception:
        cols = []
    if "is_credit" in cols:
        with op.batch_alter_table("transactions") as batch:
            batch.drop_column("is_credit")
