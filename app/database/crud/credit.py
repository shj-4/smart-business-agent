"""
حدود الائتمان واستهلاكها.
"""
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.database.models import (
    CreditLimit,
    Transaction,
)
from app.timeutil import now_utc


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
    """استخدام السقف الائتماني لشخص: outstanding = المصروفات - الإيرادات.

    موجب = الدين عليك لهذا الشخص؛ سلبي = يستفيد هو منك. يعيد:
    {outstanding, limit, percent, over} حيث percent نسبة الدين الفعلي (الموجب فقط).
    """
    from app.database.crud import accessible_user_ids

    rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(accessible_user_ids(db, limit_row.telegram_user_id)),
            Transaction.deleted_at.is_(None),
            Transaction.person == limit_row.person,
        )
        .all()
    )
    income = sum((r.amount for r in rows if r.amount is not None and r.type == "income"), Decimal("0"))
    expense = sum(
        (r.amount for r in rows if r.amount is not None and r.type == "expense"), Decimal("0")
    )
    outstanding = expense - income
    total = outstanding.quantize(Decimal("0.01"))
    limit = (limit_row.limit_amount or Decimal("0")).quantize(Decimal("0.01"))
    percent = float(total / limit * 100) if limit and total > 0 else 0.0
    return {
        "outstanding": total,
        "limit": limit,
        "percent": round(percent, 1),
        "over": total >= limit,
    }
