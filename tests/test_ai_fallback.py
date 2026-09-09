"""
اختبارات سلوك إصلاح الـ JSON والمنطق البديل (fallback) في ai_service.

مُوَكّلة بالكامل (mock) — لا اتصال بـ Gemini. تختبر:
  1) إصلاح JSON تالف بمحاولة إضافية.
  2) التسليم بأمان عندما يفشل الإصلاح.
  3) قراءة فاتورة من صورة (تحليل Vision) وتطبيع ناتجها.
  4) تفرّع الوسائط في bot/conversation نحو مسار الصور.
"""

import json
from unittest.mock import MagicMock, patch

from app.ai_service import analyze_message, analyze_receipt_image


def _resp(text: str) -> MagicMock:
    r = MagicMock()
    r.text = text
    return r


class TestJsonRepairFallback:
    @patch("app.ai_service.client.models.generate_content")
    def test_repairs_broken_json_with_one_extra_call(self, mock_gen):
        """JSON تالف (سطر نصي أضافه النموذج) يُصلَح بمحاولة إضافية واحدة."""
        broken = 'هذا هو المطلوب: {"intent": "record", "type": "expense", "amount": 300, "currency": "شيكل"}'
        fixed = json.dumps(
            {
                "intent": "record",
                "type": "expense",
                "amount": 300,
                "currency": "شيكل",
                "person": "محمد",
                "description": "شراء مواد",
                "date": None,
                "missing_fields": [],
                "query_details": None,
            }
        )
        mock_gen.side_effect = [_resp(broken), _resp(fixed)]
        result = analyze_message("دفعت 300 شيكل للمورد محمد")
        assert result["intent"] == "record"
        assert result["type"] == "expense"
        assert result["amount"] == 300
        assert mock_gen.call_count == 2

    @patch("app.ai_service.client.models.generate_content")
    def test_returns_failed_parse_when_repair_fails_too(self, mock_gen):
        """الإصلاح فشل أيضًا → fallback آمن بدل استثناء."""
        garbage = "نص عشوائي لا علاقة له"
        mock_gen.side_effect = [_resp(garbage), _resp("آسف، لا أملك JSON هنا")]
        result = analyze_message("دفعت 300 شيكل")
        assert result["intent"] == "unknown"
        assert result["error"] == "failed_to_parse"
        assert mock_gen.call_count == 2

    @patch("app.ai_service.client.models.generate_content")
    def test_fallback_works_also_for_narratives(self, mock_gen):
        """ردود المحادثة العادية تبقى تمر عبر نفس مسار التطبيع."""
        mock_gen.return_value = _resp(
            '{"intent": "query", "type": null, '
            '"query_details": {"metric": "count_transactions", "period": "this_month"}}'
        )
        result = analyze_message("كم عملية سجلتها هذا الشهر؟")
        qd = result.get("query_details") or {}
        assert result["intent"] == "query"
        assert qd.get("metric") == "count_transactions"
        assert mock_gen.call_count == 1


class TestAnalyzeReceiptImage:
    @patch("app.ai_service.client.models.generate_content")
    def test_extracts_receipt_fields(self, mock_gen):
        """صورة فاتورة → مبالغ/مورد/تاريخ وتطبيع intent=record."""
        mock_gen.return_value = _resp(
            json.dumps(
                {
                    "intent": "record",
                    "type": "expense",
                    "amount": "150.50",
                    "currency": "شيقل",
                    "person": "مخزن القدس",
                    "category": "مشتريات",
                    "description": "فاتورة مواد بناء",
                    "date": "2026-09-05",
                    "missing_fields": [],
                }
            )
        )
        result = analyze_receipt_image(b"\xff\xd8\xff\xe0fakejpeg")
        assert result["intent"] == "record"
        assert result["type"] == "expense"
        assert result["amount"] == 150.5
        assert result["person"] == "مخزن القدس"

    @patch("app.ai_service.client.models.generate_content")
    def test_not_a_receipt_returns_unknown(self, mock_gen):
        mock_gen.return_value = _resp(
            json.dumps({"intent": "record", "type": "unknown", "amount": None})
        )
        result = analyze_receipt_image(b"not-an-image")
        assert result["type"] == "unknown"

    @patch("app.ai_service.client.models.generate_content")
    def test_gemini_unavailable_returns_safe_fallback(self, mock_gen):
        mock_gen.side_effect = ConnectionError("network down")
        result = analyze_receipt_image(b"\x00\xff")
        assert result["type"] == "unknown"
        assert result["error"] == "gemini_unavailable"

    def test_receipt_parse_repair_path(self):
        """عندما يُعيد النموذج نصًا معبأً؛ يتعامل _parse_json مع الـ markdown."""
        # تمرير مباشر عبر تلميح داخلي: json.loads على نص نظيف بنجاح
        from app.ai_service import _parse_json

        assert (
            _parse_json('```json\n{"intent": "record", "type": "expense"}\n```')["type"]
            == "expense"
        )
        assert _parse_json("no json here") is None


class TestMediaImageBranch:
    """تفرّع الصور في bot/conversation._analyze_media_sync."""

    def test_image_kind_routes_to_receipt_reader(self):
        from bot.conversation import _analyze_media_sync

        with patch("bot.conversation.analyze_receipt_image") as mock_receipt:
            mock_receipt.return_value = {
                "intent": "record",
                "type": "expense",
                "amount": 90.0,
                "currency": "ILS",
                "person": "بقالة",
                "description": "فاتورة",
            }
            kind, text, result = _analyze_media_sync(b"jpgbytes", "image/jpeg", "فاتورة (صورة)")
        assert kind == "فاتورة (صورة)"
        assert "90" in text
        assert "بقالة" in text
        assert result["type"] == "expense"

    def test_unknown_receipt_produces_empty_text(self):
        from bot.conversation import _analyze_media_sync

        with patch("bot.conversation.analyze_receipt_image") as mock_receipt:
            mock_receipt.return_value = {"intent": "record", "type": "unknown", "error": None}
            kind, text, result = _analyze_media_sync(b"jpg", "image/jpeg", "فاتورة (صورة)")
        assert text == ""
        assert kind == "فاتورة (صورة)"
