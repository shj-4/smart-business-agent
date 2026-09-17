"""
الوصول الموحّد لميزة «البونس والمكافآت»:

  1) تسجيل بونس وتقاريره     — عبر معاملات بتصنيف "بونس" (expense/income) يعيد
                               البوت تقريرًا مخصصًا لكل فترة.
  2) فعاليات ترويجية          — BonusEvent: فترة بعنوان وميزانية بونس.
  3) مكافآت موظفين دورية      — EmployeeBonusPlan: مبلغ/موعد استحقاق وسقف شهري
                               مع تذكير تلقائي.
  4) نقاط ولاء لعملاء         — LoyaltyAccount + LoyaltyConfig: تراكم نقاط عند
                               المبيعات (مبيع income باسم العميل) واستبدالها خصمًا.

الملاحظات:
- النقاط تُستبدل باعتبارها خصمًا (تُحدَّث المحفظة ويُعرض المبلغ) ولا تُنشئ
  معاملة تلقائيًا كي لا تتضاعف التسجيلات؛ المستخدم يسجّل الخصم الفعلي كمعاملة
  بونس إن شاء.
- منح مكافأة عادة = معاملة expense بتصنيف "بونس" و person=اسم الموظف/العميل.
"""

import calendar
import math
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database.models import (
    BonusEvent,
    EmployeeBonusPlan,
    LoyaltyAccount,
    LoyaltyConfig,
    Transaction,
)
from app.timeutil import now_utc

# التصنيف الموحّد للبونس في المعاملات المالية
BONUS_CATEGORY = "بونس"

# أسماء متعارف عليها تُطابَق (غير حساسة لحالة الأحرف) في حقل category
BONUS_ALIASES = (
    "بونس",
    "بونص",
    "مكافأة",
    "مكافات",
    "هدية",
    "bonus",
    "bouns",
    "bonys",
    "gift",
)

EVENT_STATUS_LABELS = {"planned": "مجدولة", "active": "جارية", "ended": "منتهية"}


def _category_like(category: str) -> str:
    """نمط LIKE هارب لنص التصنيف (بحث فرعي غير حساس لحالة الحرف)."""
    escaped = (
        category.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _bonus_category_filter():
    """شرط SQL: التصنيف يطابق أيًا من أسماء البونس المتعارف عليها (نص عادي)."""
    return or_(*[Transaction.category.ilike(_category_like(a), escape="\\") for a in BONUS_ALIASES])


def _base_currency_amount(tx) -> Decimal | None:
    """المبلغ بعملة الأساس إن توثّقت (سعر صرف وقت التسجيل) وإلا المبلغ الأصلي."""
    from app.config import settings

    base = (settings.base_currency or "").upper().strip()
    if (
        base
        and tx.base_currency_at_creation == base
        and tx.amount_in_base_currency is not None
    ):
        return Decimal(str(tx.amount_in_base_currency))
    return tx.amount


# ---------- 1) تسجيل وتقارير البونس (معاملات) ----------


def list_bonus_transactions(
    db: Session,
    telegram_user_id: int,
    period: str = "this_month",
    person: str | None = None,
    limit: int = 50,
) -> list[Transaction]:
    """معاملات البونس (expense/income بتصنيف بونس) في فترة، عبر مساحة العمل."""
    from app.database.crud import accessible_user_ids, get_period_range

    start, _end = get_period_range(period)
    q = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Transaction.deleted_at.is_(None),
            _bonus_category_filter(),
        )
        .order_by(Transaction.created_at.desc())
    )
    if start:
        q = q.filter(Transaction.created_at >= start)
    if person:
        q = q.filter(Transaction.person == person)
    return q.limit(limit).all()


def bonus_report_summary(
    db: Session, telegram_user_id: int, period: str = "this_month", person: str | None = None
) -> dict:
    """ملخّص تقارير البونس: إجمالي ممنوح (expense) ومستلم (income) لكل عملة."""
    rows = list_bonus_transactions(db, telegram_user_id, period, person, limit=200)
    expense: dict = {}
    income: dict = {}
    count_expense = 0
    count_income = 0
    for tx in rows:
        cur = tx.currency or "غير محددة"
        if tx.type == "expense":
            expense[cur] = expense.get(cur, Decimal("0")) + (tx.amount or Decimal("0"))
            count_expense += 1
        else:
            income[cur] = income.get(cur, Decimal("0")) + (tx.amount or Decimal("0"))
            count_income += 1
    return {
        "period": period,
        "expense": expense,
        "income": income,
        "count_expense": count_expense,
        "count_income": count_income,
        "rows": rows,
    }


def record_bonus_grant(
    db: Session,
    telegram_user_id: int,
    amount,
    currency: str | None,
    *,
    direction: str = "expense",
    person: str | None = None,
    description: str | None = None,
    raw_message: str | None = None,
) -> Transaction | None:
    """يسجّل بونس/مكافأة كمعاملة بتصنيف "بونس" (expense منح/income استلام)."""
    from app.database.crud import _clean_person, _clean_text, create_transaction

    if direction not in ("expense", "income"):
        direction = "expense"
    return create_transaction(
        db,
        telegram_user_id,
        {
            "type": direction,
            "amount": amount,
            "currency": currency,
            "person": _clean_person(person),
            "category": BONUS_CATEGORY,
            "description": _clean_text(description),
        },
        raw_message=raw_message or "",
    )


# ---------- 2) فعاليات ترويجية ----------


def create_bonus_event(
    db: Session,
    telegram_user_id: int,
    name: str,
    *,
    budget=None,
    currency: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    note: str | None = None,
) -> BonusEvent | None:
    """ينشئ فعالية ترويجية. يعيد الكائن أو None عند اسم/قبل غير صالح."""
    from app.database.crud import _clean_text, _to_decimal, normalize_currency

    name = _clean_text(name)
    if not name:
        return None
    budget = _to_decimal(budget)
    if budget is not None and budget < 0:
        budget = None

    event = BonusEvent(
        telegram_user_id=telegram_user_id,
        name=name[:255],
        budget=budget.quantize(Decimal("0.01")) if budget is not None else None,
        currency=normalize_currency(currency) or currency,
        start_at=start_at,
        end_at=end_at,
        note=_clean_text(note),
        status="active" if (start_at or None) is not None else "planned",
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def list_bonus_events(
    db: Session, telegram_user_id: int, status: str | None = None
) -> list[BonusEvent]:
    """فعاليات المستخدم (أحدث أولًا) — عبر مساحة العمل — مع فلترة اختيارية بالحالة."""
    from app.database.crud import accessible_user_ids

    uids = accessible_user_ids(db, telegram_user_id)
    q = db.query(BonusEvent).filter(BonusEvent.telegram_user_id.in_(uids))
    if status:
        q = q.filter(BonusEvent.status == status)
    return q.order_by(BonusEvent.id.desc()).all()


def set_bonus_event_status(
    db: Session, telegram_user_id: int, event_id: int, status: str
) -> BonusEvent | None:
    """يتحكم بحالة الفعالية (planned/active/ended) — يملكها أعضاء المساحة."""
    from app.database.crud import accessible_user_ids

    if status not in EVENT_STATUS_LABELS:
        return None
    uids = accessible_user_ids(db, telegram_user_id)
    event = (
        db.query(BonusEvent)
        .filter(BonusEvent.id == event_id, BonusEvent.telegram_user_id.in_(uids))
        .first()
    )
    if event is None:
        return None
    event.status = status
    event.updated_at = now_utc()
    db.commit()
    db.refresh(event)
    return event


def end_bonus_event(db: Session, telegram_user_id: int, event_id: int) -> BonusEvent | None:
    """يختم الفعالية (status=ended) — اختصار لـ set_bonus_event_status."""
    return set_bonus_event_status(db, telegram_user_id, event_id, "ended")


def event_bonus_summary(db: Session, event: BonusEvent) -> dict:
    """إجماليات بونس داخل فترة الفعالية: ممنوح + مبيع وفترة الفعالية وميزانيتها.

    الممنوح = معاملات expense بتصنيف بونس داخل [start_at, end_at)؛
    المبيع = معاملات income (كل التصنيفات) داخل الفترة نفسها — تُقرأ عبر
    مساحة عمل المالك لتعكس المبيعات الحقيقية.
    """
    from app.database.crud import accessible_user_ids

    if event.start_at is None:
        return {
            "granted": {},
            "sales": {},
            "budget": event.budget,
            "currency": event.currency,
            "percent": None,
            "window": (None, None),
        }

    lo = event.start_at
    hi = event.end_at
    uids = accessible_user_ids(db, event.telegram_user_id)

    granted_rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(uids),
            Transaction.deleted_at.is_(None),
            Transaction.type == "expense",
            _bonus_category_filter(),
            Transaction.created_at >= lo,
            Transaction.created_at < (hi or now_utc()),
        )
        .all()
    )
    granted: dict = {}
    for tx in granted_rows:
        cur = tx.currency or "غير محددة"
        granted[cur] = granted.get(cur, Decimal("0")) + (tx.amount or Decimal("0"))

    sales_rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(uids),
            Transaction.deleted_at.is_(None),
            Transaction.type == "income",
            Transaction.created_at >= lo,
            Transaction.created_at < (hi or now_utc()),
        )
        .all()
    )
    sales: dict = {}
    for tx in sales_rows:
        cur = tx.currency or "غير محددة"
        sales[cur] = sales.get(cur, Decimal("0")) + (tx.amount or Decimal("0"))

    total_granted = None
    percent = None
    if event.budget is not None and event.budget > 0:
        # النسبة لا تُبنى بخلط عملات مختلفات: عملة واحدة تُقارن مباشرة
        # (الميزانية تُفسَّر بها)؛ عدة عملات تُوحَّد لعملة الأساس بالمبالغ
        # المثبّتة وقت التسجيل أو أسعار اليوم — وبلا توحيد تُترك None.
        spent_rows = [tx for tx in granted_rows if tx.amount is not None]
        spent_currencies = {tx.currency or "غير محددة" for tx in spent_rows}
        if len(spent_currencies) <= 1:
            total_granted = sum((tx.amount or Decimal("0")) for tx in spent_rows).quantize(Decimal("0.01"))
        else:
            from app.config import settings
            from app.money import _unified_totals_for_rows

            base = (settings.base_currency or "").upper().strip()
            granted_unified = _unified_totals_for_rows(spent_rows, base).get("total")
            if granted_unified is not None:
                total_granted = granted_unified.quantize(Decimal("0.01"))
        if total_granted is not None:
            percent = int(round(float(total_granted) / float(event.budget) * 100))

    return {
        "granted": granted,
        "sales": sales,
        "budget": event.budget,
        "currency": event.currency,
        "percent": percent,
        "window": (lo, hi),
    }


# ---------- 3) مكافآت موظفين دورية ----------


def create_employee_bonus_plan(
    db: Session,
    telegram_user_id: int,
    person: str,
    amount,
    *,
    currency: str | None = None,
    frequency: str = "monthly",
    next_due_at: datetime | None = None,
    monthly_cap=None,
    note: str | None = None,
) -> EmployeeBonusPlan | None:
    """يضيف خطة مكافأة موظف (شهري/ربع سنوي/مرة واحدة)."""
    from app.database.crud import _clean_person, _clean_text, _to_decimal, normalize_currency

    person = _clean_person(person)
    if not person:
        return None
    amount = _to_decimal(amount)
    if amount is None or amount <= 0:
        return None
    cap = _to_decimal(monthly_cap)
    if cap is not None and cap <= 0:
        cap = None
    if frequency not in ("monthly", "quarterly", "one_off"):
        frequency = "monthly"

    plan = EmployeeBonusPlan(
        telegram_user_id=telegram_user_id,
        person=person[:255],
        amount=amount.quantize(Decimal("0.01")),
        currency=normalize_currency(currency) or currency,
        frequency=frequency,
        next_due_at=next_due_at,
        monthly_cap=cap.quantize(Decimal("0.01")) if cap is not None else None,
        note=_clean_text(note),
        enabled=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def list_employee_bonus_plans(
    db: Session, telegram_user_id: int, include_disabled: bool = False
) -> list[EmployeeBonusPlan]:
    """خطط مكافآت الموظفين (المفعّلة أولًا) — عبر مساحة العمل."""
    from app.database.crud import accessible_user_ids

    uids = accessible_user_ids(db, telegram_user_id)
    q = db.query(EmployeeBonusPlan).filter(EmployeeBonusPlan.telegram_user_id.in_(uids))
    if not include_disabled:
        q = q.filter(EmployeeBonusPlan.enabled.is_(True))
    return q.order_by(EmployeeBonusPlan.next_due_at.asc()).all()


def disable_employee_bonus_plan(
    db: Session, telegram_user_id: int, plan_id: int
) -> EmployeeBonusPlan | None:
    """يعطّل خطة مكافأة موظف (لا حذف — لحفظ السجل التاريخي)."""
    from app.database.crud import accessible_user_ids

    uids = accessible_user_ids(db, telegram_user_id)
    plan = (
        db.query(EmployeeBonusPlan)
        .filter(EmployeeBonusPlan.id == plan_id, EmployeeBonusPlan.telegram_user_id.in_(uids))
        .first()
    )
    if plan is None:
        return None
    plan.enabled = False
    plan.updated_at = now_utc()
    db.commit()
    db.refresh(plan)
    return plan


def due_employee_bonus_plans(db: Session, now: datetime | None = None) -> list[EmployeeBonusPlan]:
    """كل خطط المكافآت النشطة التي بلغ موعد منحها (تُستخدم للتذكير اليومي)."""
    now = now or now_utc()
    return (
        db.query(EmployeeBonusPlan)
        .filter(
            EmployeeBonusPlan.enabled.is_(True),
            EmployeeBonusPlan.next_due_at.isnot(None),
            EmployeeBonusPlan.next_due_at <= now,
        )
        .order_by(EmployeeBonusPlan.next_due_at.asc())
        .all()
    )


def _next_due(now: datetime, months: int) -> datetime:
    """الموعد التالي بعد months شهرًا مع تقليص اليوم إن تجاوز أيام الشهر (31 يناير → 28 فبراير)."""
    year = now.year + (now.month - 1 + months) // 12
    month = (now.month - 1 + months) % 12 + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def advance_employee_bonus_due(db: Session, plan: EmployeeBonusPlan, now: datetime | None = None) -> None:
    """يقدّم موعد المنح التالي حسب الدورية؛ المرة-الواحدة تُعطَّل الخطة.

    يُحسب الموعد انطلاقًا من الاستحقاق الفعلي (next_due_at) لا من لحظة التشغيل
    كي لا ينجرف الجدول مع تأخير التذكير؛ مع تقليص اليوم عند الشهور الأقصر
    (31 يناير → 28 فبراير).
    """
    base = plan.next_due_at or now or now_utc()
    if plan.frequency == "one_off":
        plan.enabled = False
    elif plan.frequency == "quarterly":
        plan.next_due_at = _next_due(base, 3)
    elif plan.frequency == "monthly":
        plan.next_due_at = _next_due(base, 1)
    else:
        plan.enabled = False
    plan.updated_at = now_utc()
    db.commit()


def employee_bonus_monthly_spent(db: Session, plan: EmployeeBonusPlan, now: datetime | None = None) -> Decimal:
    """صرف بونس هذا الشهر للموظّف (expense بتصنيف بونس باسمه) عبر مساحة العمل.

    العملات لا تُخلط: عملة واحدة تُجمع مباشرة (المحادّ يُفسَّر بها)؛ عدة عملات
    تُوحَّد لعملة الأساس بالمبالغ المثبّتة وقت التسجيل إن توفرت أو أسعار اليوم —
    وعند تعذّر التوحيد نجمع عملة الأساس فقط (لا رقمًا مختلطًا قد يضلل المقارنة
    مع monthly_cap).
    """
    from app.config import settings
    from app.database.crud import _unified_totals_for_rows, accessible_user_ids
    from app.timeutil import now_local, to_utc_naive

    local_now = now if now is not None else now_local()
    month_start = to_utc_naive(local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(accessible_user_ids(db, plan.telegram_user_id)),
            Transaction.deleted_at.is_(None),
            Transaction.type == "expense",
            Transaction.person == plan.person,
            _bonus_category_filter(),
            Transaction.created_at >= month_start,
        )
        .all()
    )
    if not rows:
        return Decimal("0.00")
    currencies = {r.currency or "غير محددة" for r in rows if r.amount is not None}
    if len(currencies) <= 1:
        return sum((r.amount or Decimal("0")) for r in rows).quantize(Decimal("0.01"))
    base = (settings.base_currency or "").upper().strip()
    total = _unified_totals_for_rows([r for r in rows if r.amount is not None], base).get("total")
    if total is None:
        # لا توحيد متاح (لا أسعار حيّة لعملة أجنبية) — عملة الأساس فقط، بلا خلط
        total = sum(
            (r.amount for r in rows if r.amount is not None and (r.currency or "").upper() == base),
            Decimal("0"),
        )
    return total.quantize(Decimal("0.01"))


def employee_bonus_overview(db: Session, telegram_user_id: int) -> dict:
    """عرض خطط الموظفين مع صرف الشهر والمقارنة بالسقف الشهري."""
    plans = list_employee_bonus_plans(db, telegram_user_id)
    out = []
    for p in plans:
        spent = employee_bonus_monthly_spent(db, p)
        entry = {
            "id": p.id,
            "person": p.person,
            "amount": p.amount,
            "currency": p.currency,
            "frequency": p.frequency,
            "next_due_at": p.next_due_at,
            "monthly_cap": p.monthly_cap,
            "spent_this_month": spent,
            "cap_status": None,
        }
        if p.monthly_cap is not None:
            percent = int(round(float(spent) / float(p.monthly_cap) * 100)) if p.monthly_cap > 0 else 0
            entry["cap_status"] = "over" if spent >= p.monthly_cap else ("near" if percent >= 80 else "ok")
        out.append(entry)
    return {"plans": out}


# ---------- 4) نقاط الولاء ----------


def _workspace_anchor(db: Session, telegram_user_id: int) -> int:
    """مرتكز مساحة العمل (معرّف المالك) — تُثبَّت عنده سجلات النقاط المشتركة.

    في المساحة المشتركة تكون سجلات LoyaltyConfig/Account لكل شخص واحدة
    (قيدا uq_loyalty_user و uq_loyalty_user_person) ويجب أن تُنسب إلى مالك
    واحد حتى لا تتكاثر بإنشاء أكثر من عضو. فرديًّا فهو المستخدم نفسه.
    """
    from app.database.crud import workspace_for_user

    return workspace_for_user(db, telegram_user_id) or telegram_user_id


def loyalty_config_get(db: Session, telegram_user_id: int) -> LoyaltyConfig | None:
    """إعدادات نقاط الولاء عبر مساحة العمل (تفضّل صف المالك إن وُجد)."""
    from app.database.crud import accessible_user_ids

    anchor = _workspace_anchor(db, telegram_user_id)
    cfg = (
        db.query(LoyaltyConfig)
        .filter(LoyaltyConfig.telegram_user_id == anchor)
        .first()
    )
    if cfg is not None:
        return cfg
    uids = accessible_user_ids(db, telegram_user_id)
    return (
        db.query(LoyaltyConfig)
        .filter(LoyaltyConfig.telegram_user_id.in_(uids))
        .order_by(LoyaltyConfig.id.asc())
        .first()
    )


def loyalty_config_enable(
    db: Session,
    telegram_user_id: int,
    points_rate=None,
    points_value=None,
    min_redeem_points: int | None = None,
) -> LoyaltyConfig:
    """يفعّل نقاط الولاء (إنشاء/تحديث الإعدادات) على مرتكز المساحة."""
    anchor = _workspace_anchor(db, telegram_user_id)
    cfg = loyalty_config_get(db, telegram_user_id)
    if cfg is None:
        cfg = LoyaltyConfig(
            telegram_user_id=anchor,
            points_rate=Decimal(str(points_rate if points_rate is not None else 1)),
            points_value=Decimal(str(points_value if points_value is not None else 0.01)),
            min_redeem_points=int(min_redeem_points if min_redeem_points is not None else 0),
        )
        db.add(cfg)
    else:
        if points_rate is not None:
            cfg.points_rate = Decimal(str(points_rate))
        if points_value is not None:
            cfg.points_value = Decimal(str(points_value))
        if min_redeem_points is not None:
            cfg.min_redeem_points = int(min_redeem_points)
    cfg.updated_at = now_utc()
    db.commit()
    db.refresh(cfg)
    return cfg


def set_loyalty_config(
    db: Session,
    telegram_user_id: int,
    points_rate=None,
    points_value=None,
    min_redeem_points: int | None = None,
) -> LoyaltyConfig | None:
    """يحدّث إعدادات النقاط الحالية. يعيد None إذا لم تكن مفعّلة بعد."""
    cfg = loyalty_config_get(db, telegram_user_id)
    if cfg is None:
        return None
    if points_rate is not None and float(points_rate) > 0:
        cfg.points_rate = Decimal(str(points_rate))
    if points_value is not None and float(points_value) > 0:
        cfg.points_value = Decimal(str(points_value))
    if min_redeem_points is not None:
        cfg.min_redeem_points = max(0, int(min_redeem_points))
    cfg.updated_at = now_utc()
    db.commit()
    db.refresh(cfg)
    return cfg


def disable_loyalty(db: Session, telegram_user_id: int) -> bool:
    """يعطّل نقاط الولاء (يحذف الإعداد — تُوقف التراكم التلقائي)."""
    cfg = loyalty_config_get(db, telegram_user_id)
    if cfg is None:
        return False
    db.delete(cfg)
    db.commit()
    return True


def loyalty_account_get(db: Session, telegram_user_id: int, person: str) -> LoyaltyAccount | None:
    from app.database.crud import _clean_person, accessible_user_ids

    person = _clean_person(person)
    if not person:
        return None
    anchor = _workspace_anchor(db, telegram_user_id)
    account = (
        db.query(LoyaltyAccount)
        .filter(
            LoyaltyAccount.telegram_user_id == anchor,
            LoyaltyAccount.person == person,
        )
        .first()
    )
    if account is not None:
        return account
    uids = accessible_user_ids(db, telegram_user_id)
    return (
        db.query(LoyaltyAccount)
        .filter(
            LoyaltyAccount.telegram_user_id.in_(uids),
            LoyaltyAccount.person == person,
        )
        .order_by(LoyaltyAccount.id.asc())
        .first()
    )


def _loyalty_account_create(db: Session, telegram_user_id: int, person: str) -> LoyaltyAccount:
    from app.database.crud import _clean_person

    account = LoyaltyAccount(
        telegram_user_id=_workspace_anchor(db, telegram_user_id),
        person=_clean_person(person),
        points_balance=0,
        total_earned=0,
        total_redeemed=0,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def loyalty_add_points(
    db: Session, telegram_user_id: int, person: str, points: int
) -> LoyaltyAccount:
    """يضيف نقاطًا إلى محفظة عميل (إنشاء المحفظة إن غابت)."""
    points = max(0, int(points))
    account = loyalty_account_get(db, telegram_user_id, person)
    if account is None:
        account = _loyalty_account_create(db, telegram_user_id, person)
    account.points_balance = (account.points_balance or 0) + points
    account.total_earned = (account.total_earned or 0) + points
    account.updated_at = now_utc()
    db.commit()
    db.refresh(account)
    return account


def accrue_loyalty_for_transaction(db: Session, tx: Transaction) -> None:
    """تراكم تلقائي للنقاط عند تسجيل مبيع (income) باسم عميل إن كان البونس مفعّلًا.

    يُستدعى من create_transaction بعد الحفظ (مُغلَّف؛ لا يكسر التسجيل إن فشل):
    النقاط = المبلغ (بعملة الأساس إن توثّقت، وإلا الأصلي) × نقاط/وحدة.
    """
    if tx is None or not getattr(tx, "type", None) == "income":
        return
    person = getattr(tx, "person", None)
    if not person:
        return
    cfg = loyalty_config_get(db, tx.telegram_user_id)
    if cfg is None or cfg.points_rate is None or float(cfg.points_rate) <= 0:
        return
    base_amount = _base_currency_amount(tx)
    if base_amount is None or base_amount <= 0:
        return
    points = int(math.floor(float(base_amount) * float(cfg.points_rate)))
    if points <= 0:
        return
    loyalty_add_points(db, tx.telegram_user_id, person, points)


def loyalty_redeem_points(
    db: Session, telegram_user_id: int, person: str, points: int
) -> dict:
    """يستبدل نقاط عميل خصمًا. يعيد {ok, person, points, value, balance, error?}.

    القيمة خصم = النقاط × points_value (بعملة الأساس). لا تُنشأ معاملة تلقائيًا
    كي لا تتضاعف السجلات؛ المستخدم يسجّل الخصم الفعلي كمعاملة بونس إن شاء.
    """
    cfg = loyalty_config_get(db, telegram_user_id)
    account = loyalty_account_get(db, telegram_user_id, person)
    points = int(points)
    if cfg is None or account is None:
        return {
            "ok": False,
            "error": "نقاط الولاء غير مفعّلة أو لا توجد محفظة لهذا العميل.",
        }
    if points <= 0:
        return {"ok": False, "error": "عدد النقاط غير صالح."}
    if points < (cfg.min_redeem_points or 0):
        return {
            "ok": False,
            "error": f"أقل عدد للاستبدال هو {int(cfg.min_redeem_points) or 0} نقاط.",
        }
    if account.points_balance < points:
        return {
            "ok": False,
            "error": f"الرصيد غير كافٍ — لديه {account.points_balance} نقطة فقط.",
        }
    account.points_balance -= points
    account.total_redeemed = (account.total_redeemed or 0) + points
    account.updated_at = now_utc()
    db.commit()
    db.refresh(account)
    value = (Decimal(str(cfg.points_value)) * points).quantize(Decimal("0.01"))
    return {
        "ok": True,
        "person": account.person,
        "points": points,
        "value": value,
        "balance": account.points_balance,
    }


def list_loyalty_accounts(
    db: Session, telegram_user_id: int, limit: int = 20
) -> list[LoyaltyAccount]:
    from app.database.crud import accessible_user_ids

    uids = accessible_user_ids(db, telegram_user_id)
    return (
        db.query(LoyaltyAccount)
        .filter(LoyaltyAccount.telegram_user_id.in_(uids))
        .order_by(LoyaltyAccount.points_balance.desc())
        .limit(limit)
        .all()
    )


# ---------- نظرة شاملة ----------


def bonus_overview(db: Session, telegram_user_id: int) -> dict:
    """ملخص صفحة /bonus: بونس الشهر، الفعاليات، خطط الموظفين، نقاط الولاء."""
    report = bonus_report_summary(db, telegram_user_id, period="this_month")
    events = list_bonus_events(db, telegram_user_id)
    active_events = [e for e in events if e.status in ("planned", "active")]
    plans = employee_bonus_overview(db, telegram_user_id)["plans"]
    due_count = sum(
        1
        for p in plans
        if p["next_due_at"] is not None and p["next_due_at"] <= now_utc()
    )
    cfg = loyalty_config_get(db, telegram_user_id)
    accounts = list_loyalty_accounts(db, telegram_user_id, limit=5)
    total_points = sum((a.points_balance or 0) for a in accounts)
    return {
        "report": report,
        "events_count": len(active_events),
        "events": active_events[:5],
        "plans": plans,
        "plans_count": len(plans),
        "due_plans_count": due_count,
        "loyalty_enabled": cfg is not None,
        "loyalty": cfg,
        "loyalty_accounts": accounts,
        "total_points": total_points,
    }
