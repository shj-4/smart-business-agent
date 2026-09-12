"""
أدوات وثوابت مشتركة لكل مجموعات الوصول (تنقية/تحويل/تحميل انسجامًا للملفات).
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from dateutil import parser as date_parser
from sqlalchemy.orm import Session

from app.cache import clear as clear_cache
from app.events import emit
from app.timeutil import to_utc_naive

CURRENCY_ALIASES = {
    "شيكل": "ILS",
    "الشيكل": "ILS",
    "شواكل": "ILS",
    "شيقل": "ILS",
    "شياقل": "ILS",
    "شيقلا": "ILS",
    "₪": "ILS",
    "nis": "ILS",
    "₪:": "ILS",
    "shekel": "ILS",
    "shekels": "ILS",
    "ils": "ILS",
    "دولار": "USD",
    "الدولار": "USD",
    "دولارات": "USD",
    "$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "دينار": "JOD",
    "الدينار": "JOD",
    "دنانير": "JOD",
    "jd": "JOD",
    "يورو": "EUR",
    "€": "EUR",
    "euro": "EUR",
    "eur": "EUR",
}

MAX_DESCRIPTION_LEN = 500  # حد أقصى لطول النصوص الحرة (الوصف/الطلبية/الملاحظة)

def normalize_currency(raw: str | None) -> str | None:
    """يحوّل أي صيغة عملة إلى رمز ISO موحّد (ILS لشيقل، USD لدولار...)."""
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "")
    if key in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[key]
    # تطابق جزئي (مثل "شيكل جديد", "دولار امريكي") — بمقارنة غير حساسة لحالة الأحرف
    raw_lower = raw.strip().lower()
    for alias, code in CURRENCY_ALIASES.items():
        if alias in raw_lower:
            return code
    return raw.strip() or None

def _clean_text(value) -> str | None:
    """ينظّف نصًا حرًا: يقلّص المسافات ويحدّ طوله (أو يعيد None)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:MAX_DESCRIPTION_LEN]

def _clean_person(value) -> str | None:
    """ينظّف حقل الشخص: نص فاضي → None (حتى لا يكسر فلترة person في الاستعلامات)."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None

def _to_decimal(value) -> Decimal | None:
    """يحوّل المبلغ إلى Decimal بدقة نقطتين عشريتين (برای تجنّب أخطاء تقريب float).

    الحقل amount معرّف بـ Numeric(12,2) في قاعدة البيانات؛ استخدام float مباشرة
    قد يُدخل قيمًا مثل 0.30000000000000004. نحوّل هنا عبر Decimal مع تقريب مصرفي.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None

def _is_duplicate_message(
    db: Session, model, telegram_user_id: int, telegram_message_id: int | None
) -> bool:
    """يتحقق هل تم تسجيل نفس الرسالة مسبقًا (idempotency)."""
    if telegram_message_id is None:
        return False
    return (
        db.query(model)
        .filter(
            model.telegram_user_id == telegram_user_id,
            model.telegram_message_id == telegram_message_id,
        )
        .first()
        is not None
    )

def _like_escape(value: str) -> str:
    """يهرّب محارف LIKE (\\ % _) ليحاكي البحث الحرفي الفرعي في Python."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def parse_date_local(date_str: str) -> datetime | None:
    """يحوّل نص تاريخ (صيغة ISO أو صيغة مرنة) إلى datetime بالتوقيت المحلي (UTC).

    يتوافق مع كل إصدارات بايثون: نجرب أولاً صيغة صريحة "YYYY-MM-DD HH:MM[:SS]"
    ثم dateutil المرن. القيمة الناتجة تُعتبر بالتوقيت المحلي وتُرجع كـ UTC.
    """
    if not date_str:
        return None
    s = date_str.strip().replace("Z", "+00:00")
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(s, fmt)
            return to_utc_naive(parsed)
        except ValueError:
            continue
    # معالجة أي جزء زمني صريح (+00:00 إلخ)
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        try:
            parsed = date_parser.parse(s)
        except Exception:
            return None
    return to_utc_naive(parsed)

def _invalidate_caches(db: Session, telegram_user_id: int) -> None:
    """يُستدعى بعد أي كتابة: يمسح الاستعلامات المؤقتة لكل من يرى بيانات هذا المستخدم.

    في مساحة مشتركة تُبنى مفاتيح run_query على accessible_user_ids (كل الأعضاء)،
    لذا يجب إبطال كاش كل الأعضاء لا الكاتب فقط — وإلا بقي عضو آخر يرى إجماليات
    قديمة حتى انتهاء TTL.
    """
    from app.database.crud import accessible_user_ids

    for uid in accessible_user_ids(db, telegram_user_id):
        clear_cache(f"run_query:{uid}")
    emit("data_written")  # admin يستمع لهذا الحدث لمسح كاش الإحصائيات
