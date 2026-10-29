"""
مخططات (Schemas) Pydantic لضبط وتحقق صحة استجابة مساعد الذكاء الاصطناعي.

الغرض: لا نثق بأي JSON يرجعه Gemini — نمرّره عبر هذا المخطط للتحقق من الأنواع،
وتصحيح (coerce) القيم الشائعة الخاطئة (مثل amount كنص بدل رقم)، وتعبئة القيم
الافتراضية للحقول الغائبة. الناتج دائمًا dict موحّد يمكن استهلاكه بأمان
في bot.py و crud.py دون استثناءات غير متوقعة.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.normalize import normalize_priority, normalize_recurrence
from app.validation import (
    clamp_amount,
    clean_free_text,
    clean_person,
    normalize_currency,
    sanitize_analysis_result,
    valid_intent,
    valid_record_type,
)


def _to_str_or_none(value) -> str | None:
    """يحوّل أي قيمة إلى نص، أو None إذا كانت خالية/غير مناسبة."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, (list, dict)):
        return None  # لا نسمح ببنى معقدة في حقول نصية
    return str(value).strip() or None


def _coerce_amount(value) -> float | None:
    """يحوّل المبلغ إلى float أو None (يقبل رقمًا أو نصًا يحتوي رقمًا)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    if isinstance(value, str):
        s = str(value).strip()
        # تحويل الأرقام العربية ٠-٩ والفواصل العربية
        arabic_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
        s = s.translate(arabic_digits)
        s = s.replace("\u066c", ",").replace("\u060c", ",").replace("٫", ".").replace("٬", ",")
        # إزالة فواصل الآلاف: 1,500 → 1500 ، 1,500,000 → 1500000
        # إذا كان النمط \d{1,3}(,\d{3})+ → فواصل آلاف
        if re.search(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
            s = s.replace(",", "")
        elif "," in s and "." not in s:
            # فاصلة عشرية مفردة: 12,5 → 12.5
            s = s.replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", s)
        if match:
            try:
                return float(match.group(0))
            except Exception:
                return None
    return None


def _coerce_missing_fields(value) -> list[str]:
    """يضمن أن missing_fields قائمة من النصوص."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = list(value.keys())
    if isinstance(value, list):
        result = []
        for item in value:
            s = _to_str_or_none(item)
            if s:
                result.append(s)
        return result
    return []


class QueryDetailsSchema(BaseModel):
    """تفاصيل الاستعلام — تُحلَّل من query_details في استجابة الـ AI."""

    model_config = ConfigDict(extra="ignore")

    metric: str | None = None
    period: str | None = None
    person: str | None = None

    @field_validator("metric", "period", "person", mode="before")
    @classmethod
    def _qstr(cls, v):
        return _to_str_or_none(v)


class AnalysisResultSchema(BaseModel):
    """البنية المتوقعة لاستجابة analyze_message بعد التحقق والتصحيح."""

    model_config = ConfigDict(extra="ignore")

    intent: str | None = None
    type: str | None = None
    amount: float | None = None
    currency: str | None = None
    # حفظ العملة الخام عند ورود عملة غير معروفة (يوان/BTC/ريال المبهم) لكشفها لاحقًا
    currency_raw: str | None = None
    raw_currency: str | None = None
    person: str | None = None
    category: str | None = None
    description: str | None = None
    date: str | None = None
    is_credit: bool | None = None
    ledger_entry: str | None = None
    debt_direction: str | None = None
    ledger_kind: str | None = None
    reply: str | None = None
    vat_rate: float | None = None
    vat_amount: float | None = None
    priority: str | None = None
    recurrence: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    query_details: QueryDetailsSchema | None = None

    # حقول داخلية إضافية تحمل معلومات خطأ/خام عند الفشل
    error: str | None = None
    raw: str | None = None

    @field_validator(
        "intent", "type", "currency", "person", "category", "description", "date", "currency_raw", "raw_currency", mode="before"
    )
    @classmethod
    def _strs(cls, v):
        return _to_str_or_none(v)

    @field_validator("intent", mode="after")
    @classmethod
    def _intent(cls, v):
        return v if valid_intent(v) else None

    @field_validator("type", mode="after")
    @classmethod
    def _type(cls, v):
        return v if valid_record_type(v) else None

    @field_validator("currency", mode="after")
    @classmethod
    def _currency(cls, v):
        return normalize_currency(v)

    @field_validator("amount", mode="before")
    @classmethod
    def _amt(cls, v):
        return _coerce_amount(v)

    @field_validator("amount", mode="after")
    @classmethod
    def _amt_bounds(cls, v, info):
        # limit per currency: LBP etc larger max
        try:
            cur = info.data.get("currency") if isinstance(info.data, dict) else None
        except Exception:
            cur = None
        return clamp_amount(v, currency=cur)

    @field_validator("person", mode="after")
    @classmethod
    def _person(cls, v):
        return clean_person(v)

    @field_validator("description", "category", mode="after")
    @classmethod
    def _free_text(cls, v):
        return clean_free_text(v)

    @field_validator("priority", mode="before")
    @classmethod
    def _priority(cls, v):
        s = _to_str_or_none(v)
        if not s:
            return None
        return normalize_priority(s)

    @field_validator("recurrence", mode="before")
    @classmethod
    def _recurrence(cls, v):
        s = _to_str_or_none(v)
        if not s:
            return None
        return normalize_recurrence(s)

    @field_validator("is_credit", mode="before")
    @classmethod
    def _credit(cls, v):
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("true", "1", "yes", "y", "t", "credit", "آجل", "ذمة"):
                return True
            if low in ("false", "0", "no", "n", "f", "cash", "نقدي", "صندوق"):
                return False
            return None
        if isinstance(v, int) and not isinstance(v, bool):
            return bool(v)
        return None

    @field_validator("ledger_entry", "debt_direction", mode="before")
    @classmethod
    def _ledger_entry(cls, v):
        if v is None:
            return None
        if isinstance(v, bool):
            return "charge" if v else None
        s = str(v).strip().lower()
        if s in ("charge", "آجل", "ذمة", "credit", "true", "1", "yes"):
            return "charge"
        if s in ("settlement", "تسديد", "تسوية", "payment", "cash", "نقدي", "false", "0", "no"):
            return "settlement" if s in ("settlement", "تسديد", "تسوية", "payment") else None
        if s in ("none", "null", ""):
            return None
        return s if s in ("charge", "settlement") else None

    @field_validator("missing_fields", mode="before")
    @classmethod
    def _miss(cls, v):
        return _coerce_missing_fields(v)

    @field_validator("query_details", mode="before")
    @classmethod
    def _qd(cls, v):
        if isinstance(v, dict):
            return v
        return None


def normalize_analysis(data) -> dict:
    """يمرّر البيانات الخام عبر المخطط ويعيد dict موحّدًا (لا يرمي استثناءات).

    يُطبَّق بعد المخطط تنظيف إضافي (sanitize_analysis_result) كخط دفاع ثانٍ:
    حتى لو مرّ بعضه من الحقول، تُصرَّف القيم المخالفة للقوائم البيضاء وحدود
    المدى قبل تسليم النتيجة لأي مستهلك.

    يعالج حالة العملة غير المعروفة (يوان/BTC/ريال المبهم): إذا كانت القيمة الخام
    غير فارغة لكن التطبيع أعاد None، تُحفظ الخام في currency_raw/_raw_currency
    ويُضاف currency إلى missing_fields ليُسأل المستخدم بدل إسقاطها للأساس بصمت.
    """
    if not isinstance(data, dict):
        return {"intent": "unknown", "error": "invalid_structure", "raw": str(data)}

    try:
        # حفظ العملة الخام قبل أن يُطبّق المخطط تطبيعها إلى None
        raw_currency_before = data.get("currency") if isinstance(data, dict) else None
        # ومع ذلك قد تكون العملة الخام في حقل مخصص مسبقًا
        if raw_currency_before is None:
            raw_currency_before = data.get("currency_raw") or data.get("_raw_currency") or data.get("raw_currency")
        raw_str_before = str(raw_currency_before).strip() if raw_currency_before is not None and str(raw_currency_before).strip() != "" else None
        model = AnalysisResultSchema.model_validate(data)
        dumped = model.model_dump()
        # إذا كان الخام موجودًا لكن الناتج المطبّع None، احفظه قبل sanitize
        if raw_str_before and dumped.get("currency") is None:
            dumped["currency_raw"] = raw_str_before
            dumped["_raw_currency"] = raw_str_before
            dumped["raw_currency"] = raw_str_before
            # تأكد أن missing_fields يحتوي currency إن لم يكن موجودًا
            mf = dumped.get("missing_fields") or []
            if "currency" not in mf:
                mf = list(mf) + ["currency"]
                dumped["missing_fields"] = mf
        result = sanitize_analysis_result(dumped)
        # تأمين إضافي: إن كان sanitize لم يضفها لسبب ما، تأكد مرة أخرى
        if raw_str_before and result.get("currency") is None:
            mf2 = result.get("missing_fields") or []
            if "currency" not in mf2:
                mf2 = list(mf2) + ["currency"]
                result["missing_fields"] = mf2
            result.setdefault("currency_raw", raw_str_before)
            result.setdefault("_raw_currency", raw_str_before)
            result.setdefault("raw_currency", raw_str_before)
        return result
    except ValidationError:
        # فشل التحقق بالكامل → بنية آمنة بدل إسقاط المعالجة
        return {"intent": "unknown", "error": "validation_failed", "raw": str(data)}
