"""
اختبارات عملية لـ analyze_message من app.ai_service (single source of truth).

لا نُعيد تعريف SYSTEM_PROMPT أو الموديل هنا — بل نعتمد على analyze_message
من ai_service.py فقط، لتجنّب أي ازدواجية/تباين في السلوك (كما كان في ai_test.py القديم).

ملاحظة: هذه الاختبارات تستدعي Gemini فعليًا (تحتاج اتصال + مفتاح API)، لذا
تُشغَّل يدويًا (python -m tests.test_ai) وليست جزءًا من تشغيل البوت.
"""

import unittest
import json

from app.ai_service import analyze_message


class TestAnalyzeMessage(unittest.TestCase):
    def test_returns_parsed_json_dict(self):
        result = analyze_message("دفعت 300 شيكل للمورد محمد")
        self.assertIsInstance(result, dict)

    def test_record_expense_includes_amount_and_currency(self):
        result = analyze_message("دفعت 300 شيكل للمورد محمد")
        self.assertEqual(result.get("intent"), "record")
        self.assertEqual(result.get("type"), "expense")
        self.assertEqual(result.get("amount"), 300)
        self.assertEqual(result.get("currency"), "شيكل")

    def test_task_creation_with_date(self):
        result = analyze_message("ذكرني أتصل بسامر غدا الساعة 10")
        self.assertEqual(result.get("intent"), "record")
        self.assertEqual(result.get("type"), "task")

    def test_query_total_expenses_has_query_details(self):
        result = analyze_message("كم صرفت هذا الشهر؟")
        self.assertEqual(result.get("intent"), "query")
        qd = result.get("query_details") or {}
        self.assertEqual(qd.get("metric"), "total_expenses")
        self.assertEqual(qd.get("period"), "this_month")


def run_smoke_test():
    """تشغيل يدوي سريع (مثل ai_test.py القديم) ولكن عبر analyze_message الموحّد."""
    test_messages = [
        "دفعت 300 شيكل للمورد محمد مقابل شراء مواد",
        "استلمنا 1000 دولار من العميل أحمد",
        "ذكرني أتصل مع سامر بكرة الساعة 10",
        "كم صرفت هذا الشهر؟",
        "مرحبا كيفك",
    ]
    for msg in test_messages:
        print(f"\nالرسالة: {msg}")
        result = analyze_message(msg)
        print(f"النتيجة: {json.dumps(result, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    run_smoke_test()
