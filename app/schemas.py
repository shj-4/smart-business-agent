"""
مخططات (Schemas) Pydantic لضبط وتحقق صحة استجابة مساعد الذكاء الاصطناعي.

الغرض: لا نثق بأي JSON يرجعه Gemini — نمرّره عبر هذا المخطط للتحقق من الأنواع،
وتصحيح (coerce) القيم الشائعة الخاطئة (مثل amount كنص بدل رقم)، وتعبئة القيم
الافتراضية للحقول الغائبة. الناتج دائمًا dict موحّد يمكن استهلاكه بأمان
في bot.py و crud.py دون استثناءات غير متوقعة.
"""

import re
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, ValidationError


def _to_str_or_none(value) -> Optional[str]:
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


def _coerce_amount(value) -> Optional[float]:
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


def _coerce_missing_fields(value) -> List[str]:
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

    metric: Optional[str] = None
    period: Optional[str] = None
    person: Optional[str] = None

    @field_validator("metric", "period", "person", mode="before")
    @classmethod
    def _qstr(cls, v):
        return _to_str_or_none(v)


class AnalysisResultSchema(BaseModel):
    """البنية المتوقعة لاستجابة analyze_message بعد التحقق والتصحيح."""

    model_config = ConfigDict(extra="ignore")

    intent: Optional[str] = None
    type: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    person: Optional[str] = None
    description: Optional[str] = None
    date: Optional[str] = None
    missing_fields: List[str] = Field(default_factory=list)
    query_details: Optional[QueryDetailsSchema] = None

    # حقول داخلية إضافية تحمل معلومات خطأ/خام عند الفشل
    error: Optional[str] = None
    raw: Optional[str] = None

    @field_validator("intent", "type", "currency", "person", "description", "date", mode="before")
    @classmethod
    def _strs(cls, v):
        return _to_str_or_none(v)

    @field_validator("amount", mode="before")
    @classmethod
    def _amt(cls, v):
        return _coerce_amount(v)

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
    """يمرّر البيانات الخام عبر المخطط ويعيد dict موحّدًا (لا يرمي استثناءات)."""
    if not isinstance(data, dict):
        return {"intent": "unknown", "error": "invalid_structure", "raw": str(data)}

    try:
        model = AnalysisResultSchema.model_validate(data)
        return model.model_dump()
    except ValidationError:
        # فشل التحقق بالكامل → بنية آمنة بدل إسقاط المعالجة
        return {"intent": "unknown", "error": "validation_failed", "raw": str(data)}
