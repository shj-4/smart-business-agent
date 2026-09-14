"""
توحيد خريطة الأولوية والتكرار (عربي → إنجليزي) في مكان واحد بدل 5 نسخ متفرّقة.

كانت النسخ المنفصلة تختلف بقوائم كلمات قليلًا (مثل "مهم" و"عاجلة" و"عالى" و"h")،
فكل وحدة تتصرف بطريقتها مع نفس كلمة المستخدم. هذا الملف ورقتي لا يستورد شيئًا
من crud أو bot كي لا يولّد دورة — الجميع يستورد منه.

القيم الموحّدة للأولوية: high | normal | low (الافتراضي normal).
القيم الموحّدة للتكرار: daily | weekly | monthly (غير المعروف/الفارغ → None).
"""

import re

_PRIORITY_HIGH = frozenset(
    {"high", "h", "عالية", "عالي", "عالى", "عاجل", "عاجلة", "مهم", "مستعجل"}
)
_PRIORITY_LOW = frozenset(
    {"low", "l", "منخفضة", "منخفض", "ضعيفة", "عادية جدا"}
)

# التشكيل (الفتح/الضم/الشد ...) واختلافات الألف — تُجرَّد قبل المقارنة كي يطابق
# "مهمّ" و"كل أسبوع" و"مستعجل" نظيراتها بلا تشكيل.
_HARAKAT = re.compile(r"[\u064b-\u0652\u0670]")
_ALEF_TABLE = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
_WS_RUN = re.compile(r"\s+")


def _norm_token(value) -> str:
    """يوحّد مدخل نصي: تجريد التشكيل وتوحيد الألف ثم تطبيع حالة الأحرف."""
    s = _HARAKAT.sub("", (value or "").strip())
    return s.translate(_ALEF_TABLE).lower()


def fold_text(value) -> str:
    """يطوي نصًا للمطابقة الحرفية: ما تفعله _norm_token زائد جمع المسافات
    المتتالية (فواصل/أسطر متعددة) في مسافة واحدة.

    يستخدمها حارس حقن البرومبت (app/ai_service.py) لأنماط قد يكتبها المستخدم
    بتشكيل أو بمسافات مختلفة عن قوائم الكلمات.
    """
    return _WS_RUN.sub(" ", _norm_token(value))


def normalize_priority(value) -> str:
    """يقنن أي صيغة أولوية إلى high|normal|low (الافتراضي normal)."""
    token = _norm_token(value)
    if token in _PRIORITY_HIGH:
        return "high"
    if token in _PRIORITY_LOW:
        return "low"
    return "normal"


def normalize_recurrence(value) -> str | None:
    """يقنن أي صيغة تكرار إلى daily|weekly|monthly أو None إن لم تكن معروفة."""
    token = _norm_token(value)
    if token in ("daily", "يومي", "كل يوم", "يوم"):
        return "daily"
    if token in ("weekly", "أسبوعي", "كل اسبوع", "كل أسبوع", "اسبوعي", "اسبوع"):
        return "weekly"
    if token in ("monthly", "شهري", "كل شهر", "شهر"):
        return "monthly"
    return None
