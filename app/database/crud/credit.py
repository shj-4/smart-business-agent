"""
حدود الائتمان واستهلاكها.
"""
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.database.models import (
    CreditLimit,
    Transaction,
)
from app.timeutil import now_local, now_utc, to_local_naive


def set_credit_limit(
    db: Session, telegram_user_id: int, person: str, limit_amount,
) -> CreditLimit | None:
    """يحدد/يحدّث سقفًا ائتمانيًا لشخص. يعيد None على قيم غير صالحة."""
    from app.database.crud import _clean_person, _invalidate_caches, accessible_user_ids

    person = _clean_person(person)
    if not person:
        return None
    try:
        limit = Decimal(str(limit_amount)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if limit <= 0:
        return None

    row = (
        db.query(CreditLimit)
        .filter(
            CreditLimit.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            CreditLimit.person == person,
        )
        .first()
    )
    if row is None:
        row = CreditLimit(telegram_user_id=telegram_user_id, person=person, limit_amount=limit)
        db.add(row)
    else:
        row.limit_amount = limit

    row.alerted_status = 0  # إعادة تنبيه بحدود جديدة
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    _invalidate_caches(db, telegram_user_id)
    return row

def credit_monthly_reset(db: Session, limit_row: CreditLimit) -> bool:
    """يرجّع True إذا تغيّر الشهر المحلي ويجب إعادة ضبط حالة التنبيه.

    الميزانيات تحمل month_key صريحًا؛ الائتمان لا — نستخدم علامة آخر نشاط
    (updated_at، أو created_at إن لم يُحدَّث بعد) لنعرف شهر آخر نشاط:
    إن كان من شهر سابق تُصفَّر حالة التنبيه، فيصل تنبيه جديد في الشهر الجديد
    حتى لو بقي التجاوز قائمًا (كان التجاوز يُنبه مرة واحدة للأبد بلا إعادة
    ضبط إلا بتحديد الحد يدويًا). العلامة تُقدَّم للشهر الجاري حتى لا يُعاد
    التصفير في كل فحص من نفس الشهر.
    """
    stamp = limit_row.updated_at or limit_row.created_at
    if stamp is None:
        return False
    current = now_local()
    stamp_local = to_local_naive(stamp)
    if (stamp_local.year, stamp_local.month) == (current.year, current.month):
        return False
    limit_row.alerted_status = 0
    limit_row.updated_at = now_utc()
    db.commit()
    return True


def list_credit_limits(db: Session, telegram_user_id: int) -> list[CreditLimit]:
    from app.database.crud import accessible_user_ids

    return (
        db.query(CreditLimit)
        .filter(CreditLimit.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)))
        .order_by(CreditLimit.created_at.asc())
        .all()
    )

def get_credit_limit(db: Session, telegram_user_id: int, person: str) -> CreditLimit | None:
    from app.database.crud import accessible_user_ids

    return (
        db.query(CreditLimit)
        .filter(
            CreditLimit.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            CreditLimit.person == person,
        )
        .first()
    )

def credit_usage(db: Session, limit_row: CreditLimit) -> dict:
    """استخدام السقف الائتماني لشخص في كلا الاتجاهين.

    outstanding = المصروفات - الإيرادات: موجبة = ما عليك له، سالبة = ما هو مدين لك.
    amount = القيمة المطلقة التي يُقاس عليها السقف؛ percent/over مبنيان عليها —
    فيُنبي النظام عند تجاوز السقف سواء كان الدين عليك له (مورّد) أو عليه لك (عميل).

    العملات لا تُخلط أبدًا (كانتُ تُجمع قبلًا كأنها واحدة — أرقام مضللة):
      - عملة واحدة فقط: outstanding بصافي تلك العملة (المحادّ يُفسَّر بها).
      - عدة عملات: توحيد لعملة الأساس بالمبالغ المثبّتة وقت التسجيل إن توفرت
        (أو أسعار اليوم)، فإن تعذّر التحويل (لا شبكة) يُعاد outstanding=None
        و unified_ok=False بدل بناء رقم خاطئ — ويمتنع التنبيه/العرض المضلل.

    يعيد: {outstanding, amount, by_currency, unified_ok, side, limit, percent, over}
    side: "payable" (عليك له) | "receivable" (مدين لك) | "balanced" | "unknown".
    """
    from app.config import settings
    from app.database.crud import accessible_user_ids
    from app.money import _unified_totals_for_rows

    rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(accessible_user_ids(db, limit_row.telegram_user_id)),
            Transaction.deleted_at.is_(None),
            Transaction.person == limit_row.person,
        )
        .all()
    )

    per_cur: dict = {}
    for r in rows:
        if r.amount is None:
            continue
        c = r.currency or "غير محددة"
        cell = per_cur.setdefault(c, {"income": Decimal("0"), "expense": Decimal("0")})
        cell[r.type] = cell.get(r.type, Decimal("0")) + r.amount
    by_currency = {
        c: {
            "income": cell["income"].quantize(Decimal("0.01")),
            "expense": cell["expense"].quantize(Decimal("0.01")),
            "balance": (cell["expense"] - cell["income"]).quantize(Decimal("0.01")),
        }
        for c, cell in per_cur.items()
    }

    limit = (limit_row.limit_amount or Decimal("0")).quantize(Decimal("0.01"))
    unified_ok = True
    if not by_currency:
        outstanding = Decimal("0.00")
    elif len(by_currency) == 1:
        outstanding = next(iter(by_currency.values()))["balance"]
    else:
        base = (settings.base_currency or "").upper().strip()
        exp_uni = _unified_totals_for_rows(
            [r for r in rows if r.type == "expense" and r.amount is not None], base
        ).get("total")
        inc_uni = _unified_totals_for_rows(
            [r for r in rows if r.type == "income" and r.amount is not None], base
        ).get("total")
        if exp_uni is not None and inc_uni is not None:
            outstanding = exp_uni - inc_uni
        else:
            outstanding = None
            unified_ok = False

    if outstanding is None:
        return {
            "outstanding": None,
            "amount": None,
            "by_currency": by_currency,
            "unified_ok": False,
            "side": "unknown",
            "limit": limit,
            "percent": None,
            "over": False,
        }

    outstanding = outstanding.quantize(Decimal("0.01"))
    amount = abs(outstanding)
    percent = float(amount / limit * 100) if limit and amount > 0 else 0.0
    side = "payable" if outstanding > 0 else ("receivable" if outstanding < 0 else "balanced")
    return {
        "outstanding": outstanding,
        "amount": amount,
        "by_currency": by_currency,
        "unified_ok": unified_ok,
        "side": side,
        "limit": limit,
        "percent": round(percent, 1),
        "over": amount >= limit,
    }
