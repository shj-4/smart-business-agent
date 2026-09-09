"""
اختبارات تكاملية لـ analyze_message من app.ai_service (تُشغَّل يدويًا فقط).

هذه الاختبارات تستدعي Gemini فعليًا (تحتاج اتصال + مفتاح API صالح):
  pytest -m integration            # ويحتاج متغيرات البيئة (ملف .env)
  python -m tests.test_ai          # تشغيل يدوي سريع / smoke

لأنها معلمة بـ @pytest.mark.integration وحاصلة على التهيئة في pytest.ini
(addopts = -m "not integration")، لن تُشغَّل مع `pytest` العادي أبدًا (منفصلة عن CI).
"""

import json

import pytest

from app.ai_service import analyze_message

pytestmark = pytest.mark.integration


class TestAnalyzeMessage:
    def test_returns_parsed_json_dict(self):
        result = analyze_message("دفعت 300 شيكل للمورد محمد")
        assert isinstance(result, dict)

    def test_record_expense_includes_amount_and_currency(self):
        result = analyze_message("دفعت 300 شيكل للمورد محمد")
        assert result.get("intent") == "record"
        assert result.get("type") == "expense"
        assert result.get("amount") == 300
        assert result.get("currency") == "شيكل"

    def test_task_creation_with_date(self):
        result = analyze_message("ذكرني أتصل بسامر غدا الساعة 10")
        assert result.get("intent") == "record"
        assert result.get("type") == "task"

    def test_query_total_expenses_has_query_details(self):
        result = analyze_message("كم صرفت هذا الشهر؟")
        assert result.get("intent") == "query"
        qd = result.get("query_details") or {}
        assert qd.get("metric") == "total_expenses"
        assert qd.get("period") == "this_month"


def run_smoke_test():
    """تشغيل يدوي سريع عبر analyze_message الموحّد."""
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
