"""
ملاحظات تصحيح الذكاء الاصطناعي ومراجعتها.
"""
import re as _re_feedback

from sqlalchemy.orm import Session

from app.database.models import (
    CorrectionFeedback,
)
from app.events import emit

_SENSITIVE_RE_FB = _re_feedback.compile(r"(?:\d{3,})|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _scrub_feedback_text(text: str) -> str:
    """يزيل بيانات حساسة قبل حفظ feedback (مبالغ/أرقام طويلة/إيميل)."""
    if not text:
        return text
    # اقطع الطول أولًا
    text = text[:500]
    # أخفِ الأرقام الطويلة (قد تكون مبالغ/هواتف)
    text = _SENSITIVE_RE_FB.sub("[REDACTED]", text)
    return text


def _has_feedback_consent(db: Session, telegram_user_id: int) -> bool:
    try:
        from app.database.crud.common import get_or_create_user_pref
        pref = get_or_create_user_pref(db, telegram_user_id)
        return bool(getattr(pref, "feedback_consent", True))
    except Exception:
        return True


def record_correction_feedback(
    db: Session,
    telegram_user_id: int,
    raw_message: str | None,
    source: str,
    data_type: str | None = None,
) -> CorrectionFeedback | None:
    """يسجّل رسالة كان تحليلها خاطئًا (إلغاء / رفض تأكيد) لمراجعتها يدويًا.

    source: "cancel" (ألغى المستخدم أثناء الجمع) أو "reject_confirm" (رفض
    شاشة التأكيد). النص يُخزَّن مشفّرًا؛ يُحترم تفضيل feedback_consent
    ويُطبَّق حد 500 حرف مع إخفاء الأرقام/الإيميل. للاحتفاظ: راجع purge_old_feedback.
    """
    if not raw_message or not raw_message.strip():
        return None
    if not _has_feedback_consent(db, telegram_user_id):
        return None
    cleaned = _scrub_feedback_text(raw_message.strip())
    if not cleaned:
        return None
    fb = CorrectionFeedback(
        telegram_user_id=telegram_user_id,
        source=source,
        raw_message=cleaned,
        data_type=(data_type or None),
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    emit("data_written")  # عدد "بانتظار المراجعة" في admin_stats يتغيّر
    return fb


def purge_old_feedback(db: Session, days: int = 30) -> int:
    """يحذف feedback المراجع أو الأقدم من days (سياسة احتفاظ). يعيد عدد المحذوف."""
    from datetime import timedelta

    from app.timeutil import now_utc
    cutoff = now_utc() - timedelta(days=days)
    # احذف المراجع + القديم غير المراجع
    q = db.query(CorrectionFeedback).filter(
        (CorrectionFeedback.reviewed.is_(True)) | (CorrectionFeedback.created_at < cutoff)
    )
    count = q.count()
    if count:
        q.delete(synchronize_session=False)
        db.commit()
        emit("data_written")
    return count


def delete_user_feedback(db: Session, telegram_user_id: int) -> int:
    """حذف نهائي (hard delete) لكل feedback لمستخدم — حق النسيان (/privacy)."""
    q = db.query(CorrectionFeedback).filter(CorrectionFeedback.telegram_user_id == telegram_user_id)
    count = q.count()
    if count:
        q.delete(synchronize_session=False)
        db.commit()
        emit("data_written")
    return count

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
    emit("data_written")
    return True
