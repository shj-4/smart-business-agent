"""
تقييد مخرجات مساعد الذكاء الاصطناعي بحدود آمنة قبل الحفظ.

المبدأ: لا نثق بأي قيمة يعيدها Gemini. كل حقل حر (person/description) يمر عبر
تنظيف وإزالة محارف التحكم وفرض أقصى طول؛ المبلغ مقيد بمدى مالي صالح؛ والعملة
متاحة فقط ضمن قائمة بيضاء (العملات الشائعة في بيئة المشروع)؛ والنوع/النية ضمن
قوائم ثابتة. أي قيمة خارج الحدود تُرفض (None) فيُطلب من المستخدم تصحيحها خلفًا
لإكمال التسجيل — بدل حفظ بيانات خاطئة أو ضارة.

لا يستورد هذا الملف anything من crud أو bot كي لا يولّد دوائر استيراد؛ يعتمد
فقط على app.normalize (خفيفة).
"""

import re
from decimal import Decimal, InvalidOperation

# الأنواع التي يعرفها النظام قابلةً للحفظ فعليًا (تسجيل/إكمال مهمة)
RECORD_TYPES = frozenset({"expense", "income", "task", "order", "note", "complete_task"})

# النوايا الأربع التي يوجّه بها نظام المحادثة
INTENT_TYPES = frozenset({"record", "query", "chat", "company_info"})

# عملات مقبولة: مصدر موحّد مع app/exchange.CURRENCY_NAMES (كان 13 فقط بينما
# exchange/aliases أوسع، فيُرفض TRY/CAD في مسار AI ويُقبل في التحويل — غير متسق)
# لتجنب التكرار والاختلاف، نستورد القائمة من exchange كمصدر وحيد للحقيقة.
try:
    from app.exchange import CURRENCY_NAMES as _EXCHANGE_CURRENCY_NAMES

    KNOWN_CURRENCIES = frozenset(_EXCHANGE_CURRENCY_NAMES.keys())
except Exception:
    # احتياطي عند فشل الاستيراد المبكر (اختبارات/دورة استيراد)
    KNOWN_CURRENCIES = frozenset(
        {
            "ILS", "USD", "JOD", "EUR", "GBP", "EGP", "SAR", "AED",
            "KWD", "BHD", "QAR", "OMR", "LBP", "TRY", "CAD",
        }
    )

# مدى مالي مقبول للمعاملة الواحدة
AMOUNT_MIN = Decimal("0.01")
AMOUNT_MAX = Decimal("10000000")  # 10 ملايين بعملة التسجيل

# حدود أطوال الحقول الحرة
MAX_PERSON_LEN = 100
MAX_DESCRIPTION_LEN = 500

# محارف تحكم (ما عدا السطر الجديد) تُزاح من أي نص قبل الحفظ
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RUN = re.compile(r"\s+")


def strip_control_chars(value: str) -> str:
    """يزيل محارف التحكم من نص (يحفظ الأسطر/الجمل)."""
    if value is None:
        return value
    return _CONTROL_CHARS.sub("", value)


def clean_free_text(value, max_len: int = MAX_DESCRIPTION_LEN) -> str | None:
    """نص حر آمن غير فارغ ولا يتجاوز الحد (وصف/ملاحظة/طلبية)."""
    if value is None:
        return None
    s = str(value)
    s = strip_control_chars(s)
    s = _WS_RUN.sub(" ", s).strip()
    if not s:
        return None
    return s[:max_len]


def clean_person(value) -> str | None:
    """اسم شخص (مورد/عميل/موظف) منظف: لا محارف تحكم، لا أطوال فاحشة."""
    return clean_free_text(value, max_len=MAX_PERSON_LEN)


def valid_amount(amount) -> bool:
    """هل المبلغ ضمن المدى المالي المقبول؟ (يقبل int/float/str/Decimal)."""
    if amount is None:
        return False
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not value.is_finite():
        return False
    return AMOUNT_MIN <= value <= AMOUNT_MAX


def clamp_amount(amount) -> float | None:
    """يعيد المبلغ إن كان ضمن المدى، وإلا None (تُرفض القيم الخارجة)."""
    if not valid_amount(amount):
        return None
    return float(amount)


def normalize_currency(value) -> str | None:
    """يقرّب صيغة عملة إلى رمز ISO مقيّد بالقائمة البيضاء (ILS لشيكل...)."""
    if value is None:
        return None
    raw = str(value).strip()
    code = raw.upper().replace(" ", "")
    if code in KNOWN_CURRENCIES:
        return code
    # الأسماء المتداولة (شيكل/دولار/دينار...) تُطابَق عبر خريطة المصطلحات الموجودة
    from app.database.crud.common import normalize_currency as _normalize_crud

    mapped = _normalize_crud(raw)
    if mapped and mapped.upper() in KNOWN_CURRENCIES:
        return mapped.upper()
    return None


def valid_record_type(value) -> bool:
    """نوع قابل للتسجيل أو القيمة الحارسة «unknown» (توجّه إلى رسالة التراجع)."""
    return value in RECORD_TYPES or value == "unknown"


def valid_intent(value) -> bool:
    """نية توجّه (record/query/chat) أو القيمة الحارسة «unknown» (مسار آمن)."""
    return value in INTENT_TYPES or value == "unknown"


def sanitize_analysis_result(result: dict) -> dict:
    """يطبّق كل قيود الأمان على dict مفكوك من JSON قبل الحفظ.

    يمرر في المكان ويُعيد نفس الكائن لسهولة الاستهلاك. لا يرمي استثناءات أبدًا:
    القيم المخالفة تصبح None فيطلب المُسجِّلُها من المستخدم.
    """
    if not isinstance(result, dict):
        return result

    result["intent"] = result.get("intent") if valid_intent(result.get("intent")) else None
    result["type"] = result.get("type") if valid_record_type(result.get("type")) else None

    if result.get("amount") is not None:
        result["amount"] = clamp_amount(result.get("amount"))

    result["currency"] = normalize_currency(result.get("currency"))

    result["person"] = clean_person(result.get("person"))
    for key in ("description", "category"):
        result[key] = clean_free_text(result.get(key))

    return result
