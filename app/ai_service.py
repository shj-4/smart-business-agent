import os
import json
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

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
  "type": "expense" | "income" | "task" | "order" | "note" | "unknown",
  "amount": number | null,
  "currency": string | null,
  "person": string | null,
  "description": string | null,
  "date": string | null,
  "missing_fields": [array of strings],
  "query_details": {
    "metric": "total_expenses" | "total_income" | "list_tasks" | "list_overdue_tasks" | "count_transactions" | null,
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
- مهمة أو تذكير → "task"
- طلبية من/إلى عميل أو مورد → "order"
- معلومة عامة يريد حفظها → "note"
- غير واضح → "unknown"

قواعد حقل date (فقط عندما intent = "record"):
- التاريخ يجب أن يكون بصيغة ISO دائماً: "YYYY-MM-DD HH:MM" (مثال: "2026-09-03 10:00").
- استخدم اليوم الذي سأعطيك إياه كمرجع لحساب تواريخ نسبية (مثل "غدًا" أو "بعد يومين").
- إذا لم يُذكر أي تاريخ أو موعد في الرسالة، اتركه null.

قواعد query_details (مهمة، عندما intent = "query"):
- metric إجبارية ولا يمكن أن تكون null عندما intent = "query". اختر من: "total_expenses" (سؤال عن مصاريف/دفعات/صرف)، "total_income" (سؤال عن إيرادات/استلام/قبض)، "count_transactions" (سؤال عن عدد العمليات)، "list_tasks" (سؤال عن المهام القائمة)، "list_overdue_tasks" (سؤال عن المهام المتأخرة/المنتهية مواعيدها).
- period: الفترة الزمنية المقصودة. حدد بدقة. إذا لم تُذكر أي فترة، استخدم "all_time".
- person: إذا كان السؤال عن شخص معين (مثلاً "كم دفعت لمحمد؟")، ضع اسمه هنا، وإلا null.

أمثلة:
"دفعت 300 شيكل للمورد محمد" → intent: record, type: expense
"كم صرفت هذا الشهر؟" → intent: query, query_details: {metric: total_expenses, period: this_month, person: null}
"شو المصاريف يلي دفعتها لمحمد؟" → intent: query, query_details: {metric: total_expenses, person: محمد, period: all_time}
"كم استلمت هذا الشهر؟" → intent: query, query_details: {metric: total_income, period: this_month, person: null}
"كم دفعت لمحمد؟" → intent: query, query_details: {metric: total_expenses, period: all_time, person: محمد}
"ذكرني أتصل بسامر غدا الساعة 10" → intent: record, type: task, description: "الاتصال بسامر", person: "سامر", date: (غدًا بالـ ISO بناءً على التاريخ المرجعي)
"ما هي مهامي؟" → intent: query, query_details: {metric: list_tasks, period: all_time, person: null}
"شو المهام المتأخرة؟" → intent: query, query_details: {metric: list_overdue_tasks, period: all_time, person: null}
"مرحبا" → intent: chat

تذكير: أي رسالة يُقصد بها السؤال عن إجمالي/كمية/ملخص للبيانات المخزنة فهي intent=query، ولا تنسَ ملء metric وperiod وperson بدقة ودائمًا.
"""


def analyze_message(text: str) -> dict:
    from datetime import datetime

    today = datetime.utcnow()
    date_reference = f"التاريخ المرجعي اليوم: {today.strftime('%Y-%m-%d %H:%M')}"
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=f"{date_reference}\n\n{SYSTEM_PROMPT}\n\nرسالة المستخدم: {text}",
        config={
            "safety_settings": [],
            "system_instruction": SYSTEM_PROMPT,
        },
    )
    raw_text = response.text.strip()
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {"intent": "unknown", "error": "failed_to_parse", "raw": raw_text}


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[
            STT_PROMPT,
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ],
        config={"safety_settings": []},
    )
    return (response.text or "").strip()