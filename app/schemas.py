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
        match = re.search(r"-?\d+(?:[.,]\d+)?", value.replace("\u0660", "0").strip())
        if match:
            try:
                return float(match.group(0).replace(",", "."))
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
    person: str | None = None
    category: str | None = None
    description: str | None = None
    date: str | None = None
    priority: str | None = None
    recurrence: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    query_details: QueryDetailsSchema | None = None

    # حقول داخلية إضافية تحمل معلومات خطأ/خام عند الفشل
    error: str | None = None
    raw: str | None = None

    @field_validator(
        "intent", "type", "currency", "person", "category", "description", "date", mode="before"
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
    def _amt_bounds(cls, v):
        return clamp_amount(v)

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
    """
    if not isinstance(data, dict):
        return {"intent": "unknown", "error": "invalid_structure", "raw": str(data)}

    try:
        model = AnalysisResultSchema.model_validate(data)
        return sanitize_analysis_result(model.model_dump())
    except ValidationError:
        # فشل التحقق بالكامل → بنية آمنة بدل إسقاط المعالجة
        return {"intent": "unknown", "error": "validation_failed", "raw": str(data)}
