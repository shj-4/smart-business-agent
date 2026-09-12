"""
تفضيلات اللغة لكل مستخدم (UserPref).
"""
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.database.models import (
    UserPref,
)
def get_user_lang(db: Session, telegram_user_id: int) -> str:
    """لغة الواجهة المحفوظة للمستخدم (ar افتراضي)."""
    row = db.query(UserPref).filter(UserPref.telegram_user_id == telegram_user_id).first()
    return row.lang if row and row.lang else "ar"

def set_user_lang(db: Session, telegram_user_id: int, lang: str) -> str:
    """يحفظ لغة الواجهة ويعيدها (يقنّن إلى ar/en)."""
    lang = "en" if (lang or "").strip().lower() == "en" else "ar"
    row = db.query(UserPref).filter(UserPref.telegram_user_id == telegram_user_id).first()
    if row is None:
        row = UserPref(telegram_user_id=telegram_user_id, lang=lang)
        db.add(row)
    else:
        row.lang = lang
        row.updated_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return lang
