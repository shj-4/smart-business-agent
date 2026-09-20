"""
أدوات وثوابت مشتركة لكل مجموعات الوصول (تنقية/تحويل/تحميل انسجامًا للملفات).
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

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
    "دينار اردني": "JOD",
    "دينار": "JOD",
    "الدينار": "JOD",
    "الدينار الاردني": "JOD",
    "دنانير": "JOD",
    "jd": "JOD",
    "دينار عراقي": "IQD",
    "الدينار العراقي": "IQD",
    "iqd": "IQD",
    "يورو": "EUR",
    "€": "EUR",
    "euro": "EUR",
    "eur": "EUR",
    # عملات كانت في KNOWN_CURRENCIES دون alias عربي — توحيد القوائم البيضاء
    "جنيه مصري": "EGP",
    "الجنيه المصري": "EGP",
    "جنيه": "EGP",
    "egp": "EGP",
    "جنيه استرليني": "GBP",
    "استرليني": "GBP",
    "gbp": "GBP",
    "pound": "GBP",
    "ريال سعودي": "SAR",
    "الريال السعودي": "SAR",
    "sar": "SAR",
    "ريال": None,  # مجردة — تُترك ليُسأل المستخدم (لا تسقط للأساس بصمت)
    "درهم": "AED",
    "درهم اماراتي": "AED",
    "aed": "AED",
    "دينار كويتي": "KWD",
    "kwd": "KWD",
    "دينار بحريني": "BHD",
    "bhd": "BHD",
    "ريال قطري": "QAR",
    "qar": "QAR",
    "ريال عماني": "OMR",
    "omr": "OMR",
    "ليرة لبنانية": "LBP",
    "lbp": "LBP",
    "ليرة تركية": "TRY",
    "try": "TRY",
    "دولار كندي": "CAD",
    "cad": "CAD",
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
    # نُرتّب حسب طول الاسم تنازليًا حتى لا يطغى "دولار" على "دولار كندي"
    # إصلاح: الأسماء القصيرة (SAR, ILS, TRY طول ≤3) كانت تطابق داخل أسماء أشخاص
    # (Sarah→SAR, Nisreen→ILS جزئيًا، Tryphon→TRY) — نطلب مطابقة كلمة كاملة
    # أو حدود كلمات لضمان عدم الخلط.
    import re

    raw_lower = raw.strip().lower()
    # أولاً: جرّب الأسماء الطويلة (≥4) كمطابقة جزئية عادية
    for alias, code in sorted(CURRENCY_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(alias) <= 3:
            continue
        if alias in raw_lower:
            return code
    # ثانيًا: الأسماء القصيرة (≤3) فقط بمطابقة كلمة كاملة بحدود كلمات
    for alias, code in sorted(CURRENCY_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(alias) > 3:
            continue
        # استخدم حدود كلمات: \balias\b
        if re.search(r"\b" + re.escape(alias) + r"\b", raw_lower):
            return code
        # أيضًا جرّب المطابقة التامة بدون مسافات (مثل "$" أو "₪")
        if alias == raw_lower:
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
    """ينظّف حقل الشخص: نص فاضي → None (حتى لا يكسر فلترة person في الاستعلامات).

    يطبّق طيّ الأسماء (أحمد→احمد عبر إزالة همزة الألف) لتوحيد "أحمد" و"احمد"
    كنفس الشخص — يُستخدم في المطابقات اللاحقة.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # طيّ للعرض المخزن: نحتفظ بالنص الأصلي لكن نُطبّق folding للمقارنة
    # في أماكن التجميع (person_debts, company_filter) — هنا نحتفظ كما هو
    # لكن نُقلّص المسافات الزائدة
    return s or None


def normalize_person_for_match(value: str | None) -> str | None:
    """يطوي اسم الشخص للمطابقة: أحمد == احمد (إزالة تشكيل/همزات)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        from app.normalize import fold_text

        return fold_text(s)
    except Exception:
        return s.lower()


_THREE_DECIMAL_CURRENCIES = frozenset({"JOD", "KWD", "BHD", "OMR"})


def _quantize_for_currency(value: Decimal, currency: str | None) -> Decimal:
    """يكمّم المبلغ حسب عملته: 3 منازل لـ JOD/KWD/BHD/OMR وإلا منزلتان."""
    if currency and currency.upper() in _THREE_DECIMAL_CURRENCIES:
        return value.quantize(Decimal("0.001"))
    return value.quantize(Decimal("0.01"))


def _to_decimal(value, currency: str | None = None) -> Decimal | None:
    """يحوّل المبلغ إلى Decimal بدقة مناسبة للعملة (3 لـ JOD/KWD/BHD/OMR وإلا 2).

    الحقل amount معرّف بـ Numeric(12,2) في قاعدة البيانات؛ استخدام float مباشرة
    قد يُدخل قيمًا مثل 0.30000000000000004. نحوّل هنا عبر Decimal مع تقريب مصرفي.
    """
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
        return _quantize_for_currency(d, currency)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_valid_amount(value, currency: str | None = None) -> Decimal | None:
    """حارس مركزي للمبالغ المالية: يحوّل عبر _to_decimal ثم يرفض أي قيمة
    غير موجبة أو خارج المدى المالي المسموح [AMOUNT_MIN, AMOUNT_MAX].

    يُستخدم لكل مسارات حفظ المعاملات (create_transaction, update_transaction,
    record_bonus_grant...) بدل الاعتماد فقط على طبقة AI schema — فيمنع تسريب
    مبالغ سالبة/صفرية/فاحشة حتى من أوامر يدوية أو استجابة AI مموّهة.
    """
    d = _to_decimal(value, currency=currency)
    if d is None:
        return None
    try:
        from app.validation import AMOUNT_MIN, get_amount_max

        max_v = get_amount_max(currency)
        if d < AMOUNT_MIN or d > max_v:
            return None
        if d <= 0:
            return None
        if not d.is_finite():
            return None
    except Exception:
        return None
    return d

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
    """يحوّل نص تاريخ صارم إلى UTC (يرفض تواريخ فضفاضة مثل "10" أو "May")."""
    if not date_str:
        return None
    s = date_str.strip().replace("Z", "+00:00")
    # رفض تواريخ قصيرة/فضفاضة (dateutil يتساهل ويُرجع 10 من الشهر أو May الحالي)
    if len(s) < 8 or s.isdigit() or s.strip().lower() in ("may", "jun", "jul", "today", "tomorrow"):
        # نسمح فقط بصيغ رقمية كاملة  YYYY-MM-DD...
        if not any(c in s for c in ("-", "/", ":", "T")):
            return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(s, fmt)
            return to_utc_naive(parsed)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(s)
        # fromisoformat لا يزال متساهل لكن لا يقبل "May"
        if parsed.year < 1900 or parsed.year > 2100:
            return None
        return to_utc_naive(parsed)
    except ValueError:
        return None

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


def get_or_create_user_pref(db: Session, telegram_user_id: int):
    from sqlalchemy.exc import IntegrityError

    from app.database.models import UserPref

    pref = db.query(UserPref).filter(UserPref.telegram_user_id == telegram_user_id).first()
    if pref is not None:
        return pref
    from app.timeutil import now_utc

    pref = UserPref(telegram_user_id=telegram_user_id, updated_at=now_utc())
    db.add(pref)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # سباق concurrent: آخر أنشأه قبلك
        pref = db.query(UserPref).filter(UserPref.telegram_user_id == telegram_user_id).first()
        if pref is None:
            raise
    else:
        db.refresh(pref)
    return pref


def toggle_notification_pref(db: Session, telegram_user_id: int, field: str, enabled: bool) -> str:
    """يُحدّث إعداد إشعار واحد. يعيد اسم الإعداد بعد التعديل."""
    VALID = {
        "notif_task_reminder", "notif_budget_alert", "notif_credit_alert",
        "notif_invoice_alert", "notif_morning_summary", "notif_deviation", "notif_periodic_report",
        "notif_proactive",
    }
    if field not in VALID:
        return f"إعداد غير معروف: {field}"
    pref = get_or_create_user_pref(db, telegram_user_id)
    setattr(pref, field, enabled)
    from app.timeutil import now_utc

    pref.updated_at = now_utc()
    db.commit()
    db.refresh(pref)
    return field
