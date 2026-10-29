"""
تفضيلات اللغة لكل مستخدم (UserPref).
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    UserPref,
)
from app.timeutil import now_utc


def get_user_lang(db: Session, telegram_user_id: int) -> str:
    """لغة واجهة البوت الثابتة."""
    return "ar"

def set_user_lang(db: Session, telegram_user_id: int, lang: str) -> str:
    """يحفظ اللغة العربية كتفضيل الواجهة الوحيد."""
    lang = "ar"
    row = db.query(UserPref).filter(UserPref.telegram_user_id == telegram_user_id).first()
    if row is None:
        row = UserPref(telegram_user_id=telegram_user_id, lang=lang, updated_at=now_utc())
        db.add(row)
    else:
        row.lang = lang
        row.updated_at = now_utc()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return lang
