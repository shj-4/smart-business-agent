import os
import json
from dotenv import load_dotenv
from google import genai


load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_PROMPT = """
أنت مساعد ذكي يحلل رسائل المستخدمين المتعلقة بإدارة أعمالهم اليومية.
مهمتك: استخراج نوع العملية والبيانات المرتبطة بها من نص الرسالة، وإرجاعها بصيغة JSON فقط بدون أي شرح إضافي.


الحقول المطلوبة:
{
  "type": "expense" | "income" | "task" | "order" | "note" | "unknown",
  "amount": number | null,
  "currency": string | null,
  "person": string | null,
  "description": string | null,
  "date": string | null,
  "missing_fields": [array of field names that are required but missing]
}


قواعد:
- إذا كانت الرسالة عن دفع مبلغ لشخص ما (مورد)، النوع هو "expense".
- إذا كانت الرسالة عن استلام مبلغ من عميل، النوع هو "income".
- إذا كانت الرسالة عن مهمة أو تذكير، النوع هو "task".
- إذا لم تفهم نوع الرسالة، النوع هو "unknown".
- أرجع JSON فقط، بدون أي نص إضافي أو علامات markdown.
"""


def analyze_message(text: str) -> dict:
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=f"{SYSTEM_PROMPT}\n\nرسالة المستخدم: {text}",
    )
    raw_text = response.text.strip()
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {"error": "failed_to_parse", "raw": raw_text}


if __name__ == "__main__":
    test_messages = [
        "دفعت 300 شيكل للمورد محمد مقابل شراء مواد",
        "استلمنا 1000 دولار من العميل أحمد",
        "ذكرني أتصل مع سامر بكرة الساعة 10",
        "مرحبا كيفك",
    ]

    for msg in test_messages:
        print(f"\nالرسالة: {msg}")
        result = analyze_message(msg)
        print(f"النتيجة: {json.dumps(result, ensure_ascii=False, indent=2)}")
