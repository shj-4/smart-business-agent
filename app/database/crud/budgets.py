"""
الموازنات: إنشاء/استهلاك/إعادة تعيين شهري، ورصيد الأشخاص.
"""
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import log_audit
from app.database.crud.company import company_filter, report_company_filter
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
    # لا نعالج "سنوي" عبر الاسم — الميزانية دائمًا شهرية؛ السنوية تُنشأ كـ 12× شهرية
    try:
        from app.database.crud.common import _quantize_for_currency

        tmp_cur = currency
        raw_dec = Decimal(str(monthly_limit).split()[0] if " " in str(monthly_limit) else str(monthly_limit))
        limit = _quantize_for_currency(raw_dec, tmp_cur)
    except InvalidOperation:
        return None
    except Exception:
        try:
            limit = Decimal(str(monthly_limit)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    if limit <= 0:
        return None

    if not currency and not person and not category:
        return None

    from app.database.crud.company import company_id_for_user as _cid_for

    _cid = _cid_for(db, telegram_user_id)
    # منع التكرار لكل شركة (وليس لكل مستخدم) — فحص قبل الإدخال
    # للشركات: نفس company_id + scope + target يُرفض؛ للأفراد: نفس user + scope + target
    if _cid is not None:
        exists_q = db.query(Budget).filter(Budget.company_id == _cid, Budget.scope == scope)
        if scope == "currency":
            exists_q = exists_q.filter(Budget.currency == currency)
        elif scope == "person":
            exists_q = exists_q.filter(Budget.person == person)
        else:
            exists_q = exists_q.filter(Budget.category == category)
        if exists_q.first() is not None:
            return None
    else:
        exists_q = db.query(Budget).filter(Budget.telegram_user_id == telegram_user_id, Budget.scope == scope)
        if scope == "currency":
            exists_q = exists_q.filter(Budget.currency == currency)
        elif scope == "person":
            exists_q = exists_q.filter(Budget.person == person)
        else:
            exists_q = exists_q.filter(Budget.category == category)
        if exists_q.first() is not None:
            return None
    budget = Budget(
        telegram_user_id=telegram_user_id,
        company_id=_cid,
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

    return (
        db.query(Budget)
        .filter(report_company_filter(db, telegram_user_id, Budget))
        .order_by(Budget.created_at.asc())
        .all()
    )

def get_budget(db: Session, telegram_user_id: int, budget_id: int) -> Budget | None:

    return (
        db.query(Budget)
        .filter(
            report_company_filter(db, telegram_user_id, Budget),
            Budget.id == budget_id,
        )
        .first()
    )

def delete_budget(db: Session, telegram_user_id: int, budget_id: int) -> bool:
    from app.database.crud import _invalidate_caches

    budget = get_budget(db, telegram_user_id, budget_id)
    if not budget:
        return False

    detail = f"scope={budget.scope} id={budget_id}"
    db.delete(budget)
    db.commit()
    log_audit(telegram_user_id, "delete_budget", f"budget:{budget_id}", detail=detail)
    _invalidate_caches(db, telegram_user_id)
    return True

def budget_usage(db: Session, budget: Budget) -> dict:
    """استهلاك الميزانية هذا الشهر (محليًا) — أو آخر 12 شهرًا إن كانت سنوية.

    يعيد: {spent: Decimal, limit: Decimal, percent: float, over: bool}

    فصل الصندوق عن الذمة:
    - scope=currency/category → نقدي فقط (P&L) — الذمم لا تُحتسب
    - scope=person → ذمم فقط — النقدي لشخص لا يُحتسب في ميزانية الشخص
    """
    from app.database.crud.common import _cash_filter, _charge_filter, _debt_filter, normalize_person_for_match
    from app.database.crud.company import get_user_base_currency


    local_now = now_local()
    # جميع الميزانيات شهرية فقط — السنوية تُحسب كـ 12 ميزانية شهرية منفصلة
    local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = to_utc_naive(local_start)

    q = db.query(Transaction).filter(
        company_filter(db, budget.telegram_user_id, Transaction),
        Transaction.deleted_at.is_(None),
        Transaction.type == "expense",
        Transaction.created_at >= start,
    )
    if budget.scope == "currency":
        q = q.filter(Transaction.currency == budget.currency, _cash_filter(Transaction))
    elif budget.scope == "category":
        # مطابقة تامة — كبقية النطاقات: تصنيف «صيانة» لا يلتقط معاملة
        # تصنيفها «صيانة سيارة» (أرقام إنفاق مضلِّلة في تنبيهات التجاوز).
        # + نقدي فقط
        q = q.filter(Transaction.category == budget.category, _cash_filter(Transaction))
    else:
        q = q.filter(_charge_filter(Transaction))
        _norm_budget_person = normalize_person_for_match(budget.person)

    if budget.scope == "person":
        spent_rows = [r for r in q.all() if normalize_person_for_match(r.person) == _norm_budget_person]
    else:
        spent_rows = q.all()

    if budget.scope == "currency":
        # الميزانية مُقوَّمة بعملتها: مبالغ العملة نفسها تُجمع مباشرة.
        spent = sum((r.amount for r in spent_rows if r.amount is not None), Decimal("0"))
    else:
        # شخص/تصنيف بلا عملة: مبالغ العملات المتعددة تُحوَّل لعملة الأساس
        # لشركة المستخدم إن وجدت (ليس settings.base_currency العام فقط)
        # (بالمبالغ المثبَّتة وقت التسجيل إن توفرت — الدقة التاريخية) بدل
        # جمع شيكل مع دولار في رقم واحد.
        base_for_user = get_user_base_currency(db, budget.telegram_user_id)
        unified = _unified_totals_for_rows(spent_rows, base_for_user)
        spent = unified["total"]
        if spent is None:
            # تعذّر تحويل كل المبالغ (لا شبكة/أسعار) — لا نخلط عملات في رقم واحد
            # (خطأ محاسبي مضلل)؛ نجمع عملة الأساس فقط كحدّ أدنى صادق حتى لا
            # تُعدّ الميزانية 0% رغم وجود مصاريف بعملة الأساس.
            base = get_user_base_currency(db, budget.telegram_user_id)
            spent = sum(
                (
                    r.amount
                    for r in spent_rows
                    if r.amount is not None and (r.currency or "").upper() == base
                ),
                Decimal("0"),
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

    يحترم report.view_all: من لا يملكه يرى سجلاته فقط، لا كل الشركة.
    يُحتسب على الذمم فقط — النقدي (is_credit=False) لا يظهر في الذمم.
    """
    from app.database.crud.common import _debt_filter
    from app.database.crud.company import get_user_base_currency, report_company_filter


    base = get_user_base_currency(db, telegram_user_id)
    rows = (
        db.query(Transaction)
        .filter(
            report_company_filter(db, telegram_user_id, Transaction),
            Transaction.deleted_at.is_(None),
            Transaction.person.isnot(None),
            _debt_filter(Transaction),
        )
        .all()
    )

    # تجميع بالأسماء مع طيّ للهمزات (احمد == أحمد)
    from app.database.crud.common import normalize_person_for_match

    persons: dict[str, list] = {}
    display_names: dict[str, str] = {}
    for r in rows:
        raw_name = (r.person or "").strip()
        if not raw_name:
            continue
        folded = normalize_person_for_match(raw_name) or raw_name
        # المفتاح هو المطوي، القيمة تحفظ الاسم الأصلي الأول للعرض
        if folded not in display_names:
            display_names[folded] = raw_name
        persons.setdefault(folded, []).append(r)

    result = []
    for folded, pr in persons.items():
        name = display_names.get(folded, folded)
        per_cur: dict[str, dict] = {}
        for r in pr:
            c = r.currency or "غير محددة"
            cell = per_cur.setdefault(c, {"income": Decimal("0"), "expense": Decimal("0")})
            if r.amount is not None:
                is_settlement = (r.debt_direction == "settlement" or r.ledger_entry == "settlement" or r.ledger_kind == "settlement")
                eff_type = r.type
                if is_settlement:
                    eff_type = "income" if r.type == "expense" else "expense"
                cell[eff_type] = cell.get(eff_type, Decimal("0")) + r.amount
        by_currency = {}
        for c, cell in per_cur.items():
            balance = cell["income"] - cell["expense"]
            by_currency[c] = {
                "income": cell["income"].quantize(Decimal("0.01")),
                "expense": cell["expense"].quantize(Decimal("0.01")),
                "balance": balance.quantize(Decimal("0.01")),
            }

        inc_rows = []
        exp_rows = []
        for r in pr:
            if r.amount is None:
                continue
            is_settlement = (r.debt_direction == "settlement" or r.ledger_entry == "settlement" or r.ledger_kind == "settlement")
            eff_type = r.type
            if is_settlement:
                eff_type = "income" if r.type == "expense" else "expense"
            if eff_type == "income":
                inc_rows.append(r)
            else:
                exp_rows.append(r)
        unified_inc = _unified_totals_for_rows(inc_rows, base)["total"]
        unified_exp = _unified_totals_for_rows(exp_rows, base)["total"]
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


def person_debts_aging(db: Session, telegram_user_id: int) -> list[dict]:
    """أعمار الديون: توزيع رصيد كل شخص على شرائح 0-30/31-60/61-90/+90 يوم."""
    from datetime import datetime, timedelta
    from app.database.crud.common import _debt_filter, normalize_person_for_match
    from app.database.crud.company import report_company_filter
    from app.timeutil import now_utc, to_local_naive

    now = now_utc()
    rows = (
        db.query(Transaction)
        .filter(
            report_company_filter(db, telegram_user_id, Transaction),
            Transaction.deleted_at.is_(None),
            Transaction.person.isnot(None),
            _debt_filter(Transaction),
        )
        .all()
    )
    buckets = ["0-30", "31-60", "61-90", "90+"]
    persons: dict[str, dict] = {}
    display: dict[str, str] = {}
    for r in rows:
        raw = (r.person or "").strip()
        if not raw:
            continue
        folded = normalize_person_for_match(raw) or raw
        if folded not in display:
            display[folded] = raw
        dt = r.occurred_at if getattr(r, "occurred_at", None) is not None else r.created_at
        age = (now - dt).days if dt is not None else 0
        if age <= 30:
            b = "0-30"
        elif age <= 60:
            b = "31-60"
        elif age <= 90:
            b = "61-90"
        else:
            b = "90+"
        cell = persons.setdefault(folded, {k: Decimal("0") for k in buckets})
        is_settlement = (r.debt_direction == "settlement" or r.ledger_entry == "settlement" or r.ledger_kind == "settlement")
        eff_type = r.type
        if is_settlement:
            eff_type = "income" if r.type == "expense" else "expense"
        amt = r.amount or Decimal("0")
        if eff_type == "income":
            cell[b] += amt
        else:
            cell[b] -= amt
    result = []
    for folded, data in persons.items():
        result.append({"person": display.get(folded, folded), "buckets": {k: data[k].quantize(Decimal("0.01")) for k in buckets}, "total": sum(data.values()).quantize(Decimal("0.01"))})
    return result
