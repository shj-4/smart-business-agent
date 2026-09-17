"""
بناء نص التقارير الدورية التلقائية (يومي/أسبوعي/شهري).

يُنشئ ملخصًا نصيًا يعرض:
- إجمالي المصاريف والإيرادات للفترة (مع المجموع الموحّد بالعملة الأساسية إن أمكن).
- عدد المهام المعلّقة والمتأخرة مع ذكر أوضح المهام المتأخرة.

الإرسال والجدولة يعيشان في bot.reminders (periodic_report_job)؛ هنا فقط بناء النص.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.database.crud import accessible_user_ids, mark_overdue_tasks
from app.database.models import Task, Transaction
from app.formatting import FREQUENCY_NAMES
from app.formatting import fmt_amount as _fmt_amount
from app.formatting import totals_line as _totals_line
from app.timeutil import now_local, to_local_naive
from bot.icons import EXPENSE, INCOME, TASK, WARNING

# الدورية ↔ الفترة المستخدمة للمقارنة (لدى التقارير الأسبوعي/الشهري نعرض الفترة السابقة)
FREQUENCY_PERIOD = {
    "daily": "today",
    "weekly": "this_week",
    "monthly": "this_month",
}

# فترات العرض: لليومي "اليوم" وللأسبوعي/الشهري الفترة السابقة
DISPLAY_LABELS = {
    "daily": "اليوم",
    "weekly": "الأسبوع الماضي",
    "monthly": "الشهر الماضي",
}


def _totals_between(
    db: Session, user_id: int, lo: datetime | None, hi: datetime | None, tx_type: str
) -> dict:
    """إجمالي عمليات نوع معيّن بين حدود زمنية (بصيغة UTC) مصنّفًا بالعملة.

    المبالغ تُجمع في Python لأنها مخزّنة مشفّرة.
    """
    from app.database.crud import _sum_amounts_by_currency

    q = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(accessible_user_ids(db, user_id)),
        Transaction.deleted_at.is_(None),
        Transaction.type == tx_type,
        Transaction.created_at >= lo,
        Transaction.created_at < hi,
    )
    return _sum_amounts_by_currency(q.all())


def _overdue_info(db: Session, user_id: int) -> tuple[int, list[dict]]:
    """يعيد (عدد متأخر, قائمة أهم المهام المتأخرة)."""
    mark_overdue_tasks(db, user_id)
    rows = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(accessible_user_ids(db, user_id)),
            Task.status == "overdue",
            Task.deleted_at.is_(None),
        )
        .order_by(Task.due_date.asc().nulls_last())
        .all()
    )
    top = []
    for t in rows[:3]:
        due = ""
        if t.due_date:
            due = to_local_naive(t.due_date).strftime("%Y-%m-%d")
        top.append({"description": (t.description or "")[:50], "due_date": due})
    return len(rows), top


def _period_bounds(frequency: str) -> tuple[datetime | None, datetime | None]:
    """حدود الفترة المعروضة (UTC naive). للدوري اليومي نعرض اليوم، وللأسبوعي/الشهري الفترة السابقة."""
    from app.database.crud import get_comparison_ranges

    ranges = get_comparison_ranges(FREQUENCY_PERIOD[frequency])
    if frequency == "daily":
        return ranges["current"]
    return ranges["previous"]


def _unified_line(totals: dict, stored: dict | None = None) -> str | None:
    from app.config import settings
    from bot.formatters import _unified_amount

    base = settings.base_currency
    total = _unified_amount(totals, stored=stored)
    if total is None:
        return None
    if len(totals) > 1:
        hint = " (بأسعار مثبّتة لحظة التسجيل)" if stored else " (تقريبًا)"
        return f"المجموع الموحّد{hint}: {_fmt_amount(total)} {base}"
    return f"المجموع الموحّد: {_fmt_amount(total)} {base}"


def _stored_totals_between(
    db: Session, user_id: int, lo: datetime | None, hi: datetime | None, tx_type: str
) -> dict:
    """مبالغ نوع معيّن بين حدود زمنية محوَّلة ومثبّتة بعملة الأساس عند التسجيل.

    يُفضَّل في التقارير التاريخية على تحويل أسعار اليوم (لأنها توثّق سعر
    لحظة العملية الفعلية). العملات بلا سعر مخزَّن تُستبعد فتُحسب حيّة.
    """

    from app.config import settings

    base = (settings.base_currency or "").upper().strip()
    q = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(accessible_user_ids(db, user_id)),
        Transaction.deleted_at.is_(None),
        Transaction.type == tx_type,
        Transaction.created_at >= lo,
        Transaction.created_at < hi,
    )
    stored: dict = {}
    for r in q.all():
        if (
            r.base_currency_at_creation == base
            and r.amount_in_base_currency is not None
            and r.currency
        ):
            c = r.currency
            stored[c] = stored.get(c, Decimal("0")) + Decimal(str(r.amount_in_base_currency))
    return stored


def build_periodic_summary(
    db: Session,
    telegram_user_id: int,
    frequency: str,
    now_dt: datetime | None = None,
) -> str:
    """يبني نص تقرير دوري للمستخدم (لا يرسل أي شيء)."""
    from app.database.crud import get_comparison_ranges

    frequency = frequency if frequency in FREQUENCY_PERIOD else "daily"
    display_label = DISPLAY_LABELS[frequency]
    ranges = get_comparison_ranges(FREQUENCY_PERIOD[frequency])
    if frequency == "daily":
        lo, hi = ranges["current"]
    else:
        lo, hi = ranges["previous"]

    if now_dt is None:
        now_dt = now_local()
    today_str = now_dt.strftime("%Y-%m-%d")

    expenses = _totals_between(db, telegram_user_id, lo, hi, "expense")
    incomes = _totals_between(db, telegram_user_id, lo, hi, "income")
    stored_expenses = _stored_totals_between(db, telegram_user_id, lo, hi, "expense")
    stored_incomes = _stored_totals_between(db, telegram_user_id, lo, hi, "income")
    overdue_count, overdue_top = _overdue_info(db, telegram_user_id)
    pending_count = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Task.status == "pending",
            Task.deleted_at.is_(None),
        )
        .count()
    )

    lines = [f"📊 تقريرك {FREQUENCY_NAMES[frequency]} — {today_str}\n"]

    lines.append(f"{EXPENSE} المصاريف ({display_label}): {_totals_line(expenses) or 'لا توجد'}")
    if expenses:
        unified = _unified_line(expenses, stored=stored_expenses)
        if unified:
            lines.append(unified)

    lines.append("")
    lines.append(f"{INCOME} الإيرادات ({display_label}): {_totals_line(incomes) or 'لا توجد'}")
    if incomes:
        unified = _unified_line(incomes, stored=stored_incomes)
        if unified:
            lines.append(unified)

    lines.append("")
    lines.append(f"{TASK} مهامك: {pending_count} قيد الانتظار")
    if overdue_count:
        lines.append(f"{WARNING} {overdue_count} متأخرة:")
        for t in overdue_top:
            due_txt = f" (موعد: {t['due_date']})" if t["due_date"] else ""
            lines.append(f"• {t['description']}{due_txt}")
    else:
        lines.append("لا توجد مهام متأخرة. ممتاز!")

    lines.append("")
    lines.append("أرسل /chart لعرض رسم بياني بالإيرادات والمصروفات. 💹")
    return "\n".join(lines)
