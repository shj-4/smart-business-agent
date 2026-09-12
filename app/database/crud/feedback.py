"""
ملاحظات تصحيح الذكاء الاصطناعي ومراجعتها.
"""
from sqlalchemy.orm import Session
from app.database.models import (
    CorrectionFeedback,
)
def record_correction_feedback(
    db: Session,
    telegram_user_id: int,
    raw_message: str | None,
    source: str,
    data_type: str | None = None,
) -> CorrectionFeedback | None:
    """يسجّل رسالة كان تحليلها خاطئًا (إلغاء / رفض تأكيد) لمراجعتها يدويًا.

    source: "cancel" (ألغى المستخدم أثناء الجمع) أو "reject_confirm" (رفض
    شاشة التأكيد). النص يُخزَّن مشفّرًا؛ لا يُمسح من الجدول تلقائيًا.
    """
    if not raw_message or not raw_message.strip():
        return None
    fb = CorrectionFeedback(
        telegram_user_id=telegram_user_id,
        source=source,
        raw_message=raw_message,
        data_type=(data_type or None),
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    from app.admin import clear_admin_cache

    clear_admin_cache()  # عدد "بانتظار المراجعة" في admin_stats يتغيّر
    return fb

def list_correction_feedback(
    db: Session,
    only_unreviewed: bool = True,
    limit: int = 50,
) -> list[CorrectionFeedback]:
    """يعيد سجلات التحليل الخاطئ (الأحدث أولًا) للمراجعة اليدوية."""
    q = db.query(CorrectionFeedback)
    if only_unreviewed:
        q = q.filter(CorrectionFeedback.reviewed.is_(False))
    return q.order_by(CorrectionFeedback.created_at.desc()).limit(limit).all()

def mark_correction_reviewed(db: Session, feedback_id: int) -> bool:
    """يعلّم سجلًا كمراجَع يدويًا (لم يعد يظهر في القوائم)."""
    row = db.query(CorrectionFeedback).filter(CorrectionFeedback.id == feedback_id).first()
    if row is None:
        return False
    row.reviewed = True
    db.commit()
    from app.admin import clear_admin_cache

    clear_admin_cache()
    return True
