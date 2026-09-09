"""
خدمة Gemini AI مع retry تلقائي عند انقطاع الشبكة أو أخطاء الخادم.

تستخدم tenacity (موجودة بالفعل في requirements) لإعادة المحاولة تلقائيًا
بمدة انتظار تصاعدية (exponential backoff) عند أي استثناء — شبكة، خادم، أو
أخطاء rate limit. هذا يمنع سقوط البوت بصمت عندما ينقطع الاتصال مؤقتًا.
"""

import json
import logging

from google import genai
from google.genai import types
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)

# نموذج Gemini المستخدم للتحليل ونسخ الصوت
GEMINI_MODEL = "gemini-3.1-flash-lite"

# إعدادات retry:最多 3 محاولات، انتظار تصاعدي 1→2→4 ثوانٍ
_RETRY_STOP = stop_after_attempt(3)
_RETRY_WAIT = wait_exponential(multiplier=1, min=1, max=10)
_RETRY_RETRY = retry_if_exception_type(Exception)  # كل الاستثناءات (شبكة، خادم، rate limit)

STT_PROMPT = """
أعد كتابة الكلام في هذا الملف الصوتي كنص مكتوب حرفيًا (نص فقط، بدون أي شرح أو مقدمة أو علامات اقتباس).
حافظ على الصياغة كما قالها المتحدث وبنفس اللغة. إذا كان الكلام بالعربية أعد النص بالعربية.
"""

SYSTEM_PROMPT = """
أنت مساعد ذكي يحلل رسائل المستخدمين المتعلقة بإدارة أعمالهم اليومية.
مهمتك الأولى: تحديد نية الرسالة (intent)، ثم استخراج التفاصيل المناسبة.

أرجع دائمًا JSON فقط بدون أي شرح أو علامات markdown، بهذا الشكل:

{
  "intent": "record" | "query" | "chat",
  "type": "expense" | "income" | "task" | "order" | "note" | "complete_task" | "unknown",
  "amount": number | null,
  "currency": string | null,
  "person": string | null,
  "category": string | null,
  "description": string | null,
  "date": string | null,
  "priority": "high" | "normal" | "low" | null,
  "recurrence": "daily" | "weekly" | "monthly" | null,
  "missing_fields": [array of strings],
  "query_details": {
    "metric": "total_expenses" | "total_income" | "person_balance" | "compare_periods" | "list_tasks" | "list_overdue_tasks" | "count_transactions" | null,
    "period": "today" | "this_week" | "this_month" | "this_year" | "all_time" | null,
    "person": string | null
  }
}

قواعد تحديد intent:
- "record": المستخدم يخبر عن عملية حدثت أو سيقوم بها (دفع، استلام، طلب مهمة جديدة، طلبية، ملاحظة).
- "query": المستخدم يسأل عن بيانات موجودة مسبقًا (كم، ما هو، أعطني، اعرض، ملخص، إجمالي...) بأي صياغة، حتى لو لم تحتوِ على أداة استفهام كلاسيكية.
- "chat": رسالة عامة لا تتعلق بتسجيل أو استعلام (تحية، سؤال عام، شكر...).

قواعد تحديد type (فقط عندما intent = "record"):
- دفع مبلغ لمورد → "expense"
- استلام مبلغ من عميل → "income"
- مهمة أو تذكير جديد → "task"
- إنهاء/إنجاز مهمة موجودة → "complete_task" + ضع وصف المهمة في "description"
- طلبية من/إلى عميل أو مورد → "order"
- معلومة عامة يريد حفظها → "note"
- غير واضح → "unknown"

قواعد حقل date (فقط عندما intent = "record"):
- التاريخ يجب أن يكون بصيغة ISO دائماً: "YYYY-MM-DD HH:MM" (مثال: "2026-09-03 10:00").
- استخدم اليوم الذي سأعطيك إياه كمرجع لحساب تواريخ نسبية (مثل "غدًا" أو "بعد يومين").
- إذا لم يُذكر أي تاريخ أو موعد في الرسالة، اتركه null.

قواعد حقل category (فقط عندما intent = "record" ونوع العملية مالي expense/income/order):
- صنّف المبلغ تحت تصنيف مختصر واضح من هذه القائمة إن أمكن:
  إيجار، رواتب، مواد خام، مشتريات، نقل وشحن، كهرباء، ماء، هاتف وانترنت، طعام، صيانة، تسويق وإعلان، ضرائب، أخرى.
- إذا لم يتضح التصنيف، اتركه null (لا تخترع تصنيفًا).

قواعد حقلي priority و recurrence (فقط عندما intent = "record" ونوع العملية task):
- priority: "high" مهمة عاجلة/مستعجلة/مهمة جدًا، "low" مهمة خفيفة/غير عاجلة، "normal" خلاف ذلك (أو null لترك القيمة الافتراضية عادية).
- recurrence: عندما تطلب المهمة تكرارًا صريحًا مثل "كل يوم" أو "كل أسبوع" أو "أسبوعيًا" أو "كل شهر" أو "شهريًا" — ضع "daily" أو "weekly" أو "monthly". وإلا اتركه null.

قواعد query_details (مهمة، عندما intent = "query"):
- metric إجبارية ولا يمكن أن تكون null عندما intent = "query". اختر من: "total_expenses" (سؤال عن مصاريف/دفعات/صرف)، "total_income" (سؤال عن إيرادات/استلام/قبض)، "person_balance" (سؤال عن رصيد/فرق مع شخص معيّن مثل "كم لي عند محمد" أو "كم عليّ لسامر" أو "شو رصيدي مع خالد")، "compare_periods" (سؤال يقارن فترة بحيث تُقارَن تلقائيًا بالفترة السابقة مثل "قارن مصاريف هذا الشهر بالشهر الماضي" أو "هل صرفي هذا الأسبوع أكثر من السابق؟")، "count_transactions" (سؤال عن عدد العمليات)، "list_tasks" (سؤال عن المهام القائمة)، "list_overdue_tasks" (سؤال عن المهام المتأخرة/المنتهية مواعيدها).
- عند metric = "compare_periods": ضع period على الفترة المذكورة (مثل "this_month") وسيقارن النظام تلقائيًا بالفترة السابقة المماثلة (الشهر الماضي، الأسبوع الماضي...).
- period: الفترة الزمنية المقصودة. حدد بدقة. إذا لم تُذكر أي فترة، استخدم "all_time".
- person: إذا كان السؤال عن شخص معين (مثلاً "كم دفعت لمحمد؟")، ضع اسمه هنا، وإلا null. عندما metric = "person_balance"، person إلزامي وتعني الشخص المقابل في الرصيد.

أمثلة:
"دفعت 300 شيكل للمورد محمد" → intent: record, type: expense
"كم صرفت هذا الشهر؟" → intent: query, query_details: {metric: total_expenses, period: this_month, person: null}
"شو المصاريف يلي دفعتها لمحمد؟" → intent: query, query_details: {metric: total_expenses, person: محمد, period: all_time}
"كم استلمت هذا الشهر؟" → intent: query, query_details: {metric: total_income, period: this_month, person: null}
"كم دفعت لمحمد؟" → intent: query, query_details: {metric: total_expenses, period: all_time, person: محمد}
"كم لي عند محمد؟" → intent: query, query_details: {metric: person_balance, period: all_time, person: محمد}
"كم عليّ لسامر؟" → intent: query, query_details: {metric: person_balance, period: all_time, person: سامر}
"شو رصيدي مع خالد؟" → intent: query, query_details: {metric: person_balance, period: all_time, person: خالد}
"قارن مصاريف هذا الشهر بالشهر الماضي" → intent: query, query_details: {metric: compare_periods, period: this_month, person: null}
"مصاريف هذا الأسبوع مقابل اللي قبله؟" → intent: query, query_details: {metric: compare_periods, period: this_week, person: null}
"هل صرفت اليوم أكثر من أمس؟" → intent: query, query_details: {metric: compare_periods, period: today, person: null}
"قارن مصاريفي مع محمد هذا الشهر بالشهر السابق" → intent: query, query_details: {metric: compare_periods, period: this_month, person: محمد}
"دفعت 500 شيكل إيجار للمحل" → intent: record, type: expense, category: "إيجار"
"ذكرني أتصل بسامر غدا الساعة 10" → intent: record, type: task, description: "الاتصال بسامر", person: "سامر", date: (غدًا بالـ ISO بناءً على التاريخ المرجعي)
"ذكرني كل أسبوع اتصل بالمورد" → intent: record, type: task, description: "الاتصال بالمورد", recurrence: "weekly", date: null
"مهمة عاجلة: اشتري مواد خام غدًا" → intent: record, type: task, description: "شراء مواد خام", priority: "high", date: (غدًا بالـ ISO)
"ما هي مهامي؟" → intent: query, query_details: {metric: list_tasks, period: all_time, person: null}
"شو المهام المتأخرة؟" → intent: query, query_details: {metric: list_overdue_tasks, period: all_time, person: null}
"مرحبا" → intent: chat
"أنجزت مهمة الاتصال بسامر" → intent: record, type: complete_task, description: "الاتصال بسامر"
"خلصت المهمة اللي بعنوانها شراء مواد" → intent: record, type: complete_task, description: "شراء مواد"

تذكير: أي رسالة يُقصد بها السؤال عن إجمالي/كمية/ملخص للبيانات المخزنة فهي intent=query، ولا تنسَ ملء metric وperiod وperson بدقة ودائمًا.
"""


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


REPAIR_PROMPT = """
النص التالي كان من المفترض أن يكون JSON لكنه تالف/غير مكتمل.
أعده صحيحًا كـ JSON فقط بدون أي شرح أو علامات markdown، مع الحفاظ على كل الحقول والمفاتيح الموجودة:
{text}
"""


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


RECEIPT_SYSTEM_PROMPT = """
أنت مساعد يقرأ صور الفواتير والإيصالات والفواتير التجارية.
مهمتك: استخراج بيانات الدفع الأساسية من الصورة فقط (لا تخترع بيانات غير ظاهرة).

أرجع JSON فقط بدون أي شرح أو علامات markdown، بهذا الشكل:
{
  "intent": "record",
  "type": "expense",
  "amount": number | null,
  "currency": string | null,
  "person": string | null,
  "category": string | null,
  "description": string | null,
  "date": string | null,
  "missing_fields": [array of strings]
}

قواعد:
- amount: المبلغ الكلي الظاهر في الفاتورة بالعربي/الرموز الرقمية العربية (0-٩).
- currency: العملة إن ظهرت (شيكل/دولار/دينار/ريال...) وإلا null.
- person: اسم المورد/الجهة المصدرة إن ظهر، وإلا null.
- category: تصنيف مختصر من: إيجار، رواتب، مواد خام، مشتريات، نقل وشحن، كهرباء، ماء، هاتف وانترنت، طعام، صيانة، تسويق وإعلان، ضرائب، أخرى.
- date: تاريخ الفاتورة بصيغة "YYYY-MM-DD HH:MM" أو null إن لم يظهر.
- description: سطر مختصر يلخص طبيعة الفاتورة.
- إذا كانت الصورة ليست فاتورة/إيصالًا واضحة: أرجع type "unknown" وintent "record".
"""


def analyze_receipt_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """يحلل صورة فاتورة/إيصال ويستخرج (المبلغ، التاريخ، المورد...).

    يمرّ عبر نفس مسار _call_gemini مع retry، ونفس خطة إصلاح الـ JSON عند الفشل.
    """
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
