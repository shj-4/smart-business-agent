"""
خدمة Gemini AI مع retry تلقائي عند انقطاع الشبكة أو أخطاء الخادم.

تستخدم tenacity (موجودة بالفعل في requirements) لإعادة المحاولة تلقائيًا
بمدة انتظار تصاعدية (exponential backoff) على الفشل العابر (شبكة/مهلة/5xx)،
وتميّز أخطاء الحصة: 429 تُعاد مع احترام Retry-After، بينما أخطاء العميل
الدائمة (4xx عدا 429) لا تُعاد. هذا يمنع سقوط البوت بصمت دون إغراق Gemini
بمحاولات متكررة عند استنفاد الحصة.
"""

import json
import logging

from google import genai
from google.genai import types
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
)

from app.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

# أنماط حقن البرومبت (prompt injection) تُكتشف قبل الاتصال بـ Gemini،
# وتُحوَّل الرسالة إلى محادثة عامة بدل السماح لها بتنفيذ/تسجيل.
INJECTION_PATTERNS = (
    "ignore all previous",
    "ignore previous",
    "ignore the above",
    "disregard",
    "don't follow",
    "do not follow",
    "forget your",
    "تجاهل التعليمات",
    "تجاهل كل التعليمات",
    "تجاهل ما سبق",
    "تجاهل تعليماتك",
    "انسَ تعليماتك",
    "انس التعليمات",
    "أهمل التعليمات",
    "لا تتبع تعليماتك",
    "تجاوز التعليمات",
    "أنت الآن",
    "you are now",
    "pretend you are",
    "roleplay as",
    "act as system",
    "system prompt",
    "reveal your prompt",
    "show your instructions",
    "اشرح البرومبت",
    "ما هي تعليماتك",
    "استخرج تعليماتك",
    "jailbreak",
)


def has_injection_pattern(text: str | None) -> bool:
    """يكشف وجود أنماط حقن برومبت شائعة في رسالة المستخدم (بمطابقة جزئية وحرفية)."""
    needle = (text or "").lower()
    return any(pattern in needle for pattern in INJECTION_PATTERNS)


client = genai.Client(api_key=GEMINI_API_KEY)

# نموذج Gemini المستخدم للتحليل ونسخ الصوت
GEMINI_MODEL = "gemini-3.1-flash-lite"

# إعدادات retry:最多 3 محاولات، انتظار تصاعدي، مع تمييز أخطاء الفترة (quota) عن
# أخطاء الشبكة العابرة: نعيد المحاولة فقط على (شبكة/مهلة/5xx/429) ونحترم
# Retry-After إن وُجد — بدل تكرار كل استثناء أعمى (كان ذلك يضاعف ضغط الحصة).
_RETRY_STOP = stop_after_attempt(3)
_RETRY_AFTER_CAP = 60  # حد أقصى ثوانٍ للانتظار خلف 429


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("code", "status_code", "status"):
        raw = getattr(exc, attr, None)
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    return None


def _is_retryable(exc: Exception) -> bool:
    """يُعيد المحاولة فقط للفشل العابر — ويتوقف فورًا أمام أخطاء العميل الدائمة."""
    status = _extract_status_code(exc)
    if status is not None:
        # 429 (rate limit/quota) يُعاد مع احترام Retry-After؛ أخطاء 4xx أخرى دائمة
        return status >= 500 or status == 429
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    # استثناء غير مصنّف — نعتبره عابرًا (توافق مع السلوك السابق)
    return True


def _extract_retry_after(exc: Exception) -> float | None:
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        resp = getattr(exc, "response", None)
        if resp is not None:
            raw = (getattr(resp, "headers", None) or {}).get("retry-after")
    if raw is None:
        return None
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    return min(_RETRY_AFTER_CAP, val)


def _retry_wait(retry_state) -> float:
    exc = retry_state.outcome.exception()
    retry_after = _extract_retry_after(exc)
    if retry_after is not None:
        return retry_after
    return min(10.0, float(2 ** (retry_state.attempt_number - 1)))


_RETRY_WAIT = _retry_wait
_RETRY_RETRY = retry_if_exception(_is_retryable)

def _prompt_fallback(name: str) -> str:
    """الافتراضي نص تحذير قصير فقط — النص الفعلي في prompts/<name> هو المصدر الوحيد (بلا نسخ مكرر)."""
    return (
        f"تعذر تحميل البرومبت «{name}» — الملف مفقود في مجلد prompts/ أو غير قابل للقراءة. "
        "لا تستخدم هذا النص كتوجيه فعلي."
    )


STT_PROMPT = _prompt_fallback("stt.md")

SYSTEM_PROMPT = _prompt_fallback("system_general.md")


def _call_gemini(contents, config):
    """استدعاء Gemini مع retry تلقائي (最多 3 محاولات)."""
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=config,
    )


def _clean_json(raw_text: str) -> str:
    """يزيل أغطاء markdown الشائعة حول JSON قبل المحاولة."""
    return (raw_text or "").strip().replace("```json", "").replace("```", "").strip()


def _parse_json(raw_text: str) -> dict | None:
    """يحاول json.loads على النص — إما dict أو None (فشل)."""
    try:
        value = json.loads(_clean_json(raw_text))
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


REPAIR_PROMPT = _prompt_fallback("repair_json.md")


def _repair_json_once(bad_text: str) -> dict | None:
    """يحاول مرة واحدة أن يُصلح JSON تالف عبر استدعاء Gemini إضافي."""
    try:
        response = retry(
            stop=_RETRY_STOP,
            wait=_RETRY_WAIT,
            retry=_RETRY_RETRY,
        )(
            lambda: _call_gemini(
                contents=REPAIR_PROMPT.format(text=bad_text[:2000]),
                config={"safety_settings": []},
            )
        )()
    except Exception as exc:  # noqa: BLE001
        logger.info("فشل استدعاء إصلاح الـ JSON: %s", exc)
        return None
    return _parse_json(response.text or "")


def analyze_message(text: str) -> dict:
    from app.dialects import detect_dialect, dialect_instruction
    from app.timeutil import now_local

    # حارس حقن البرومبت: إن حاولت الرسالة إعادة برمجة المساعد، تُعامَل كحديث عام
    if has_injection_pattern(text):
        logger.info("رُصدت محاولة حقن برومبت — تجاهلت التحليل: %.60s", text)
        return {"intent": "chat", "injection_guard": True, "raw": text}

    today = now_local()
    date_reference = (
        f"التاريخ المرجعي اليوم (بالتوقيت المحلي لفلسطين / {today.tzinfo}): "
        f"{today.strftime('%Y-%m-%d %H:%M')}"
    )
    dialect = detect_dialect(text)
    try:
        response = retry(
            stop=_RETRY_STOP,
            wait=_RETRY_WAIT,
            retry=_RETRY_RETRY,
        )(
            lambda: _call_gemini(
                contents=(
                    f"{date_reference}\n{dialect_instruction(dialect)}\n\nرسالة المستخدم: {text}"
                ),
                config={
                    "safety_settings": [],
                    "system_instruction": SYSTEM_PROMPT,
                },
            )
        )()
    except RetryError as exc:
        logger.error("فشل Gemini بعد 3 محاولات لتحليل الرسالة: %s", exc)
        return {"intent": "unknown", "error": "gemini_unavailable", "raw": text}
    except Exception as exc:  # noqa: BLE001
        logger.error("خطأ غير متوقع في analyze_message: %s", exc)
        return {"intent": "unknown", "error": str(exc)[:200], "raw": text}

    parsed = _parse_json(response.text or "")
    if parsed is None:
        # fallback ذكي: محاولة إصلاح واحدة قبل الإعلان عن الفشل
        repaired = _repair_json_once(response.text or "")
        if repaired is not None:
            logger.info("تم إصلاح JSON تالف لتحليل الرسالة.")
            parsed = repaired
        else:
            parsed = {"intent": "unknown", "error": "failed_to_parse", "raw": response.text}
    from app.schemas import normalize_analysis

    return normalize_analysis(parsed)


DATE_SYSTEM_PROMPT = """
أنت مساعد يحوّل نص موعد (عربي / لهجات فلسطينية) إلى تاريخ بصيغة ISO.
أرجِع JSON فقط بدون أي شرح أو علامات markdown، بهذا الشكل:
{"date": "YYYY-MM-DD HH:MM" | null}

- استخدم "التاريخ المرجعي" الحالي لحساب التواريخ النسبية: "غدًا / بكرة / بكره"
  → اليوم التالي، "بعد يومين" → بعده بيومين، "الأسبوع القادم / الجاي" → الأسبوع التالي.
- عاملا الأسبوع (جمعة/سبت/...) تُحسب من التاريخ المرجعي نفسه.
- إن لم يتضمن النص موعدًا إطلاقًا، أرجِع {"date": null} — لا تخترع موعدًا.
- حدد ساعة افتراضية 09:00 إذا ذكر النص يومًا دون وقت، وإلا استخدم الوقت المذكور.
"""


def interpret_arabic_date(text: str, *, now=None) -> str | None:
    """يحوّل نص موعد عربي/لهجوي (مثل "بكرة الساعة 10") إلى ISO "YYYY-MM-DD HH:MM".

    يُستدعى عند تحرير حقل الموعد في شاشة التأكيد عندما لا يفهمه parse_date_local
    — يعتمد نفس مسار الذكاء الاصطناعي الذي يحسب التواريخ النسبية عند الإدخال.
    يعيد None إذا تعذّر الفهم أو فشل Gemini.
    """
    from app.database.crud import parse_date_local
    from app.timeutil import now_local, to_local_naive

    ref = now or now_local()
    contents = (
        f"التاريخ المرجعي اليوم (بالتوقيت المحلي): {ref.strftime('%Y-%m-%d %H:%M')}\n"
        f"نص الموعد: {text or ''}"
    )
    try:
        response = retry(
            stop=_RETRY_STOP,
            wait=_RETRY_WAIT,
            retry=_RETRY_RETRY,
        )(
            lambda: _call_gemini(
                contents=contents,
                config={"safety_settings": [], "system_instruction": DATE_SYSTEM_PROMPT},
            )
        )()
    except Exception as exc:  # noqa: BLE001
        logger.error("فشل تفسير الموعد عبر Gemini: %s", exc)
        return None

    parsed = _parse_json(response.text or "")
    if not isinstance(parsed, dict):
        return None
    raw_date = str(parsed.get("date") or "").strip()
    dt = parse_date_local(raw_date)
    if dt is None:
        return None
    # الرجوع بالتوقيت المحلي (naive) حتى يعيد create_* تحليله دون إزاحة زمنية
    return to_local_naive(dt).strftime("%Y-%m-%d %H:%M")


RECEIPT_SYSTEM_PROMPT = _prompt_fallback("receipt_system.md")


IMAGE_TEXT_PROMPT = """
اقرأ النص المكتوب الظاهر في هذه الصورة حرفيًا (نص فقط، بدون أي شرح أو مقدمة
أو علامات اقتباس). إن لم يكن في الصورة نص مكتوب، أعد فراغًا.
"""


def image_text_for_guard(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """يستخرج النص المكتوب داخل الصورة لفحص حقن البرومبت قبل التحليل.

    المضمون: صورة فاتورة قد تحمل داخلها نصًا يحاول إعادة برمجة المساعد
    (مثل "تجاهل التعليمات السابقة") — نفحصه بالحارس البرمجي نفسه الذي
    يستخدمه مسار النصوص، بدل الاعتماد على توجيه ناعم داخل البرومبت.
    """
    try:
        response = retry(
            stop=_RETRY_STOP,
            wait=_RETRY_WAIT,
            retry=_RETRY_RETRY,
        )(
            lambda: _call_gemini(
                contents=[
                    IMAGE_TEXT_PROMPT,
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                ],
                config={"safety_settings": []},
            )
        )()
    except Exception as exc:  # noqa: BLE001
        logger.warning("تعذر استخراج نص الصورة لفحص الحقن: %s", exc)
        return ""
    return (response.text or "").strip()


def analyze_receipt_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """يحلل صورة فاتورة/إيصال ويستخرج (المبلغ، التاريخ، المورد...).

    يمرّ عبر نفس مسار _call_gemini مع retry، ونفس خطة إصلاح الـ JSON عند الفشل.
    يفحص نصّ الصورة بحارس حقن البرومبت قبل أي معالجة هيكلية.
    """
    transcript = image_text_for_guard(image_bytes, mime_type)
    if has_injection_pattern(transcript):
        logger.info(
            "رُصدت محاولة حقن برومبت داخل صورة فاتورة — تجاهلت التحليل: %.60s", transcript
        )
        return {"intent": "chat", "injection_guard": True, "type": "unknown", "raw": transcript[:200]}

    try:
        response = retry(
            stop=_RETRY_STOP,
            wait=_RETRY_WAIT,
            retry=_RETRY_RETRY,
        )(
            lambda: _call_gemini(
                contents=[
                    "استخرج بيانات الفاتورة من هذه الصورة:",
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                ],
                config={"safety_settings": [], "system_instruction": RECEIPT_SYSTEM_PROMPT},
            )
        )()
    except RetryError as exc:
        logger.error("فشل Gemini بعد 3 محاولات لقراءة الفاتورة: %s", exc)
        return {"intent": "record", "type": "unknown", "error": "gemini_unavailable"}
    except Exception as exc:  # noqa: BLE001
        logger.error("خطأ غير متوقع في analyze_receipt_image: %s", exc)
        return {"intent": "record", "type": "unknown", "error": str(exc)[:200]}

    parsed = _parse_json(response.text or "")
    if parsed is None:
        repaired = _repair_json_once(response.text or "")
        if repaired is not None:
            parsed = repaired
        else:
            parsed = {"intent": "record", "type": "unknown", "error": "failed_to_parse"}

    # فاتورة = عملية تسجيل دائمًا؛ نضمن intent=record والنوع الافتراضي expense
    parsed = dict(parsed)
    if parsed.get("type") not in ("unknown", None):
        parsed["intent"] = "record"
    if parsed.get("type") is None:
        parsed["type"] = "expense"

    from app.schemas import normalize_analysis

    return normalize_analysis(parsed)


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    try:
        response = retry(
            stop=_RETRY_STOP,
            wait=_RETRY_WAIT,
            retry=_RETRY_RETRY,
        )(
            lambda: _call_gemini(
                contents=[
                    STT_PROMPT,
                    types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                ],
                config={"safety_settings": []},
            )
        )()
    except RetryError as exc:
        logger.error("فشل Gemini بعد 3 محاولات لنسخ الصوت: %s", exc)
        return ""
    except Exception as exc:
        logger.error("خطأ غير متوقع في transcribe_audio: %s", exc)
        return ""

    return (response.text or "").strip()


# ---------- تحميل البرومبتات: النص الفعلي من ملفات prompts/*.md فقط (مصدر واحد بلا نسخ) ----------
# الافتراضي السطحي نص تحذير قصير؛ أي نقص في الملف يُشخَّص واضحًا بدل استخدام نسخة عشوائية قديمة.

from app.prompt_loader import load_prompt as _load_prompt  # noqa: E402

SYSTEM_PROMPT = _load_prompt("system_general.md", _prompt_fallback("system_general.md"))
RECEIPT_SYSTEM_PROMPT = _load_prompt("receipt_system.md", _prompt_fallback("receipt_system.md"))
STT_PROMPT = _load_prompt("stt.md", _prompt_fallback("stt.md"))
REPAIR_PROMPT = _load_prompt("repair_json.md", _prompt_fallback("repair_json.md"))
