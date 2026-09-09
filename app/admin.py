"""
أوامر الأدمن المخفية — إنشاء نص الإحصائيات العامة من قاعدة البيانات.

المصادقة تعتمد على معرّف المستخدم في settings.admin_user_ids (معرّف واحد أو أكثر).
لا تُكشف بيانات فردية (أسماء، أوصاف، مبالغ) — فقط مجموعات عدّادية ونسب.
"""

from sqlalchemy.orm import Session

from app.cache import clear as clear_cache
from app.cache import get_or_set

_ADMIN_CACHE_KEY = "admin_stats"


def build_admin_stats(db: Session) -> dict:
    """يبني إحصائيات عامة مختصرة — لا يكشف أي معلومات شخصية.

    النتيجة تُخزَّن مؤقتًا (TTL قصير) لأنها تُبنى من قراءات متعددة للجداول؛
    أي كتابة تمسح نطاق الأدمن عبر clear_admin_cache() (تُنشَر من crud).
    """
    return get_or_set(_ADMIN_CACHE_KEY, lambda: _build_admin_stats_uncached(db))


def clear_admin_cache() -> None:
    clear_cache(_ADMIN_CACHE_KEY)


def _build_admin_stats_uncached(db: Session) -> dict:
    from app.database.crud import _sum_amounts_by_currency
    from app.database.models import (
        Budget,
        CorrectionFeedback,
        Note,
        ReportPref,
        Task,
        Transaction,
    )

    user_ids: set[int] = set()
    # نجمع معرّفات المستخدمين الحقيقية (بما فيها الحقول المشفّرة لا تؤثر)
    for model in (Transaction, Task, Note):
        rows = db.query(model.telegram_user_id).filter(model.deleted_at.is_(None)).distinct().all()
        for (uid,) in rows:
            if uid:
                user_ids.add(uid)

    tx_count = db.query(Transaction).filter(Transaction.deleted_at.is_(None)).count()
    task_count = db.query(Task).filter(Task.deleted_at.is_(None)).count()
    note_count = db.query(Note).filter(Note.deleted_at.is_(None)).count()

    pending_tasks = (
        db.query(Task).filter(Task.status == "pending", Task.deleted_at.is_(None)).count()
    )
    overdue_tasks = (
        db.query(Task).filter(Task.status == "overdue", Task.deleted_at.is_(None)).count()
    )
    budget_count = db.query(Budget).count()
    report_prefs_count = db.query(ReportPref).filter(ReportPref.frequency != "off").count()

    # إجماليات مالية (تجمع في Python لأنها مشفّرة)
    expense_rows = (
        db.query(Transaction)
        .filter(
            Transaction.deleted_at.is_(None),
            Transaction.type == "expense",
        )
        .all()
    )
    income_rows = (
        db.query(Transaction)
        .filter(
            Transaction.deleted_at.is_(None),
            Transaction.type == "income",
        )
        .all()
    )
    total_expenses = _sum_amounts_by_currency(expense_rows)
    total_incomes = _sum_amounts_by_currency(income_rows)

    stats = {
        "users_count": len(user_ids),
        "transactions_count": tx_count,
        "tasks_count": task_count,
        "notes_count": note_count,
        "pending_tasks": pending_tasks,
        "overdue_tasks": overdue_tasks,
        "budgets_count": budget_count,
        "active_reports": report_prefs_count,
        "pending_feedback": db.query(CorrectionFeedback)
        .filter(CorrectionFeedback.reviewed.is_(False))
        .count(),
        "total_expenses": total_expenses,
        "total_incomes": total_incomes,
    }

    # إحصائيات طابور الـ AI (إن كانت مفعّلة) — أرقام عامة فقط، لا تكشف بيانات
    try:
        from app.ai_queue import aiq

        stats["ai_queue"] = {
            "enabled": bool(aiq.enabled),
            "pending": aiq.pending,
            "processed": aiq.processed,
            "failed": aiq.failed,
        }
    except Exception:
        stats["ai_queue"] = None

    return stats
