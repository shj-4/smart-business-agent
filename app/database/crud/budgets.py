"""
الموازنات: إنشاء/استهلاك/إعادة تعيين شهري، ورصيد الأشخاص.
"""
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import log_audit
from app.config import settings
from app.database.models import (
    Budget,
    Transaction,
)
from app.exchange import CURRENCY_NAMES
from app.formatting import current_month_key as _current_month_key
from app.money import _unified_totals_for_rows
from app.timeutil import now_local, to_utc_naive


def create_budget(
    db: Session,
    telegram_user_id: int,
    scope: str,
    target: str,
    monthly_limit,
    name: str | None = None,
) -> Budget | None:
    """ينشئ ميزانية شهرية: scope=currency/person/category، target هو العملة/الاسم/التصنيف.

    يعيد None إذا الميزانية موجودة مسبقًا (لكل مستخدم واحد لكل scope/هدف).
    """
    from app.database.crud import _clean_text, _invalidate_caches, normalize_currency


    scope = (scope or "").lower().strip()
    currency = normalize_currency(target) if scope == "currency" else None
    person = target.strip() if scope == "person" else None
    category = _clean_text(target) if scope == "category" else None

    if scope == "currency":
        if currency is None or currency.upper() not in CURRENCY_NAMES:
            # عملة غير معروفة (normalize_currency تمرر النص كما هو) — رفضها
            # بدل إنشاء ميزانية لن تطابقها أي معاملة أبدًا (تبقى 0% للأبد).
            return None

    if not monthly_limit:
        return None
    try:
        limit = Decimal(str(monthly_limit)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if limit <= 0:
        return None

    if not currency and not person and not category:
        return None

    budget = Budget(
        telegram_user_id=telegram_user_id,
        scope=scope,
        currency=currency,
        person=person,
        category=category,
        name=_clean_text(name),
        monthly_limit=limit,
        month_key=_current_month_key(),
    )
    db.add(budget)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(budget)
    _invalidate_caches(db, telegram_user_id)
    return budget

def list_budgets(db: Session, telegram_user_id: int) -> list[Budget]:
    from app.database.crud import accessible_user_ids

    return (
        db.query(Budget)
        .filter(Budget.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)))
        .order_by(Budget.created_at.asc())
        .all()
    )

def get_budget(db: Session, telegram_user_id: int, budget_id: int) -> Budget | None:
    from app.database.crud import accessible_user_ids

    return (
        db.query(Budget)
        .filter(
            Budget.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Budget.id == budget_id,
        )
        .first()
    )

def delete_budget(db: Session, telegram_user_id: int, budget_id: int) -> bool:
    from app.database.crud import _invalidate_caches

    budget = get_budget(db, telegram_user_id, budget_id)
    if not budget:
        return False

    detail = f"scope={budget.scope} target={budget.person or budget.currency} limit={budget.monthly_limit}"
    db.delete(budget)
    db.commit()
    log_audit(telegram_user_id, "delete_budget", f"budget:{budget_id}", detail=detail)
    _invalidate_caches(db, telegram_user_id)
    return True

def budget_usage(db: Session, budget: Budget) -> dict:
    """استهلاك الميزانية هذا الشهر (محليًا).

    يعيد: {spent: Decimal, limit: Decimal, percent: float, over: bool}
    """
    from app.database.crud import _like_escape, accessible_user_ids


    local_now = now_local()
    local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = to_utc_naive(local_start)

    q = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(accessible_user_ids(db, budget.telegram_user_id)),
        Transaction.deleted_at.is_(None),
        Transaction.type == "expense",
        Transaction.created_at >= start,
    )
    if budget.scope == "currency":
        q = q.filter(Transaction.currency == budget.currency)
    elif budget.scope == "category":
        q = q.filter(Transaction.category.like(f"%{_like_escape(budget.category)}%", escape="\\"))
    else:
        q = q.filter(Transaction.person == budget.person)

    spent_rows = q.all()

    if budget.scope == "currency":
        # الميزانية مُقوَّمة بعملتها: مبالغ العملة نفسها تُجمع مباشرة.
        spent = sum((r.amount for r in spent_rows if r.amount is not None), Decimal("0"))
    else:
        # شخص/تصنيف بلا عملة: مبالغ العملات المتعددة تُحوَّل لعملة الأساس
        # (بالمبالغ المثبَّتة وقت التسجيل إن توفرت — الدقة التاريخية) بدل
        # جمع شيكل مع دولار في رقم واحد.
        unified = _unified_totals_for_rows(spent_rows, settings.base_currency)
        spent = unified["total"]
        if spent is None:
            # تعذّر تحويل كل المبالغ (لا شبكة/أسعار) — مجموع خام كتقدير أخير
            # حتى لا تُحتسب الميزانية 0% رغم وجود مصاريف (فلا يُنبه بالتجاوز).
            spent = sum(
                (r.amount for r in spent_rows if r.amount is not None), Decimal("0")
            )
    spent = spent.quantize(Decimal("0.01"))
    limit = (budget.monthly_limit or Decimal("0")).quantize(Decimal("0.01"))

    percent = float(spent / limit * 100) if limit else 0.0
    return {
        "spent": spent,
        "limit": limit,
        "percent": round(percent, 1),
        "over": spent >= limit,
    }

def budget_monthly_reset(db: Session, budget: Budget) -> bool:
    """يرجّع True إذا تغيّر الشهر ويجب إعادة ضبط حالة التنبيه."""
    current_key = _current_month_key()
    if budget.month_key != current_key:
        budget.month_key = current_key
        budget.alerted_status = 0
        db.commit()
        return True
    return False

def person_debts(db: Session, telegram_user_id: int) -> list[dict]:
    """رصيد كل شخص بعدة عملات (بماذا تدين له / بماذا يدين لك).

    يعيد قائمة مرتبة بالأحدث: لكل شخص by_currency: {مع الصفوف: income/expense/balance}
    و balance_unified (بالعملة الأساس بدقة تاريخية عبر المبالغ المخزّنة) إن أمكن.
    balance = income - expense: موجب = يدين لك، سالب = تدين له.
    """
    from app.database.crud import accessible_user_ids


    base = (settings.base_currency or "").upper().strip()
    ids = accessible_user_ids(db, telegram_user_id)
    rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(ids),
            Transaction.deleted_at.is_(None),
            Transaction.person.isnot(None),
        )
        .all()
    )

    persons: dict[str, list] = {}
    for r in rows:
        name = (r.person or "").strip()
        if not name:
            continue
        persons.setdefault(name, []).append(r)

    result = []
    for name, pr in persons.items():
        per_cur: dict[str, dict] = {}
        for r in pr:
            c = r.currency or "غير محددة"
            cell = per_cur.setdefault(c, {"income": Decimal("0"), "expense": Decimal("0")})
            if r.amount is not None:
                cell[r.type] = cell.get(r.type, Decimal("0")) + r.amount
        by_currency = {}
        for c, cell in per_cur.items():
            balance = cell["income"] - cell["expense"]
            by_currency[c] = {
                "income": cell["income"].quantize(Decimal("0.01")),
                "expense": cell["expense"].quantize(Decimal("0.01")),
                "balance": balance.quantize(Decimal("0.01")),
            }

        unified_inc = _unified_totals_for_rows(
            [r for r in pr if r.type == "income"], base
        )["total"]
        unified_exp = _unified_totals_for_rows(
            [r for r in pr if r.type == "expense"], base
        )["total"]
        if unified_inc is not None and unified_exp is not None:
            net = unified_inc - unified_exp
        else:
            net = None
        result.append(
            {
                "person": name,
                "by_currency": by_currency,
                "balance_unified": net.quantize(Decimal("0.01")) if net is not None else None,
                "base": base,
                "partial": unified_inc is None or unified_exp is None,
                # اتجاه الدين بلا أرقام: ما إذا كان صافي كل شيء موجبًا (يدين لك)
                "net_positive": net is not None and net >= 0,
                "last_activity": max((r.created_at for r in pr), default=None),
            }
        )

    result.sort(key=lambda d: (d["last_activity"] is not None, d["last_activity"]), reverse=True)
    return result
