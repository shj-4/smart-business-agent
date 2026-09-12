"""
الفواتير والطلبيات وحالاتها.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import (
    Invoice,
    Note,
)
from app.timeutil import now_utc, to_local_naive, to_utc_naive

ORDER_STATUSES = ("open", "done")

def create_invoice(
    db: Session,
    telegram_user_id: int,
    data: dict,
    raw_message: str | None = None,
) -> Invoice | None:
    """ينشئ فاتورة آجلة؛ amount إلزامي. يعيد None على قيم غير صالحة."""
    from app.database.crud import (
        _clean_person,
        _clean_text,
        _invalidate_caches,
        _to_decimal,
        normalize_currency,
    )


    amount = _to_decimal(data.get("amount"))
    if amount is None or amount <= 0:
        return None
    currency = normalize_currency(data.get("currency")) or (settings.base_currency or "").upper()
    due_date = data.get("due_date")
    if isinstance(due_date, str) and due_date.strip():
        try:
            due_date = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
        except ValueError:
            due_date = None
        if due_date is not None and due_date.tzinfo is not None:
            due_date = to_utc_naive(due_date)
    if isinstance(due_date, datetime) and due_date.tzinfo is not None:
        due_date = to_utc_naive(due_date)

    invoice = Invoice(
        telegram_user_id=telegram_user_id,
        person=_clean_person(data.get("person")),
        amount=amount.quantize(Decimal("0.01")),
        currency=currency or None,
        description=_clean_text(data.get("description")) or _clean_text(raw_message),
        due_date=due_date,
        status="pending",
        alerted=False,
    )
    db.add(invoice)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(invoice)
    _invalidate_caches(db, telegram_user_id)
    return invoice

def list_invoices(
    db: Session, telegram_user_id: int, status: str | None = None, limit: int = 50
) -> list[Invoice]:
    """فواتير المستخدم (كل أعضاء المساحة) مطابقة لحالة اختيارية، الأحدث أولًا."""
    from app.database.crud import accessible_user_ids


    q = db.query(Invoice).filter(
        Invoice.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id))
    )
    if status and status in ("pending", "paid", "overdue"):
        q = q.filter(Invoice.status == status)
    invoices = q.order_by(Invoice.created_at.desc()).limit(limit).all()

    # تعليم المتأخرة (حالة عرض) عند قراءتها — التغيير الفعلي للموديل الوارد في
    # المهمة يبقى في مهمة الفحص الدوري حتى لا نكتب على كل قراءة.
    for inv in invoices:
        if (
            inv.status == "pending"
            and inv.due_date is not None
            and to_local_naive(inv.due_date) < to_local_naive(now_utc())
        ):
            inv.status = "overdue"
    return invoices

def mark_invoice_paid(db: Session, telegram_user_id: int, invoice_id: int) -> bool:
    """سداد فاتورة آجلة — تعيد True عند النجاح."""
    from app.database.crud import _invalidate_caches, accessible_user_ids

    invoice = (
        db.query(Invoice)
        .filter(
            Invoice.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Invoice.id == invoice_id,
        )
        .first()
    )
    if invoice is None or invoice.status == "paid":
        return False

    invoice.status = "paid"
    invoice.paid_at = now_utc()
    invoice.alerted = True  # لا مزيد من تنبيهات التأخر
    db.commit()
    _invalidate_caches(db, telegram_user_id)
    return True

def mark_overdue_invoices(db: Session, now_dt=None) -> list[Invoice]:
    """يميّز الفواتير المعلّقة المتأخرة (استحقاقها مضى) — يرجّع المتأخرة حديثًا.

    مستخدم من مهمة الفحص الدوري (reminders)؛ يكتب الحالة للعرض فقط دون إرسال.
    """
    now_dt = now_dt or now_utc()
    overdue = []
    rows = (
        db.query(Invoice)
        .filter(Invoice.status == "pending", Invoice.due_date.isnot(None), Invoice.due_date < now_dt)
        .all()
    )
    for inv in rows:
        if inv.status != "overdue":
            inv.status = "overdue"
            overdue.append(inv)
    if overdue:
        db.commit()
        for inv in overdue:
            db.refresh(inv)  # حتى تبقى سماته قابلة للقراءة بعد إغلاق الجلسة
    return overdue

def list_orders(
    db: Session, telegram_user_id: int, status: str | None = None, limit: int = 50
) -> list[Note]:
    """الطلبيات (note_type=order) مع فلتر حالة اختياري، الأحدث أولًا."""
    from app.database.crud import accessible_user_ids

    q = db.query(Note).filter(
        Note.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
        Note.deleted_at.is_(None),
        Note.note_type == "order",
    )
    if status and status in ORDER_STATUSES:
        q = q.filter(Note.status == status)
    return q.order_by(Note.created_at.desc()).limit(limit).all()

def set_order_status(db: Session, telegram_user_id: int, note_id: int, status: str) -> bool:
    """يغيّر حالة طلبية (open/done) — يعيد False إن لم توجد طلبية أو حالة غير صالحة."""
    from app.database.crud import _invalidate_caches, accessible_user_ids

    if status not in ORDER_STATUSES:
        return False
    note = (
        db.query(Note)
        .filter(
            Note.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Note.id == note_id,
            Note.note_type == "order",
            Note.deleted_at.is_(None),
        )
        .first()
    )
    if note is None:
        return False

    note.status = status
    note.updated_at = now_utc()
    db.commit()
    _invalidate_caches(db, telegram_user_id)
    return True
