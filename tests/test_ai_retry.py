"""
اختبارات وحدة لمنطق retry في app.ai_service.

تختبر سلوك tenacity retry عند فشل Gemini (mock كامل — لا اتصال بالخادم).
تُشغَّل مع pytest العادي:
  pytest tests/test_ai_retry.py -v
"""

import json
from unittest.mock import MagicMock, patch

from app.ai_service import analyze_message, transcribe_audio


class TestAnalyzeMessageRetry:
    """اختبار retry في analyze_message عند فشل generate_content."""

    @patch("app.ai_service.client.models.generate_content")
    def test_returns_fallback_on_all_retries_failed(self, mock_gen):
        """إذا فشلت كل المحاولات، تُرجع fallback dict بدل استثناء."""
        mock_gen.side_effect = ConnectionError("network down")
        result = analyze_message("دفعت 300 شيكل")
        assert result["intent"] == "unknown"
        assert result["error"] == "gemini_unavailable"
        # تحقق أن المحاولة تمت 3 مرات (stop_after_attempt=3)
        assert mock_gen.call_count == 3

    @patch("app.ai_service.client.models.generate_content")
    def test_returns_fallback_on_timeout(self, mock_gen):
        """إذا انتهت المهلة في كل المحاولات."""
        import httpx

        mock_gen.side_effect = httpx.TimeoutException("timed out")
        result = analyze_message("رسالة اختبار")
        assert result["intent"] == "unknown"
        assert result["error"] == "gemini_unavailable"
        assert mock_gen.call_count == 3

    @patch("app.ai_service.client.models.generate_content")
    def test_succeeds_on_second_attempt(self, mock_gen):
        """إذا نجحت المحاولة الثانية (الأولى تفشل)."""
        good_response = MagicMock()
        good_response.text = json.dumps(
            {
                "intent": "record",
                "type": "expense",
                "amount": 500,
                "currency": "شيكل",
                "person": "أحمد",
                "description": "شراء",
                "date": None,
                "missing_fields": [],
                "query_details": None,
            }
        )
        mock_gen.side_effect = [
            ConnectionError("first attempt failed"),
            good_response,
        ]
        result = analyze_message("دفعت 500 شيكل لأحمد")
        assert result["intent"] == "record"
        assert result["type"] == "expense"
        assert mock_gen.call_count == 2


class TestTranscribeAudioRetry:
    """اختبار retry في transcribe_audio عند فشل generate_content."""

    @patch("app.ai_service.client.models.generate_content")
    def test_returns_empty_on_all_retries_failed(self, mock_gen):
        """إذا فشلت كل المحاولات، تُرجع نص فارغ."""
        mock_gen.side_effect = RuntimeError("service unavailable")
        result = transcribe_audio(b"\x00\x01\x02", mime_type="audio/ogg")
        assert result == ""
        assert mock_gen.call_count == 3

    @patch("app.ai_service.client.models.generate_content")
    def test_succeeds_on_third_attempt(self, mock_gen):
        """إذا نجحت المحاولة الثالثة."""
        good_response = MagicMock()
        good_response.text = "النص المكتوب من الصوت"
        mock_gen.side_effect = [
            ConnectionError("attempt 1"),
            ConnectionError("attempt 2"),
            good_response,
        ]
        result = transcribe_audio(b"\x00\x01\x02", mime_type="audio/ogg")
        assert result == "النص المكتوب من الصوت"
        assert mock_gen.call_count == 3

    @patch("app.ai_service.client.models.generate_content")
    def test_returns_empty_on_retry_error_exception(self, mock_gen):
        """RetryError المُولَّد من tenacity لا يتسرب كاستثناء."""
        from tenacity import RetryCallState, RetryError

        last_attempt = MagicMock()
        last_attempt.exception.return_value = RuntimeError("last failure")
        state = RetryCallState(retry_object=None, fn=MagicMock(), args=(), kwargs={})
        state.attempt_number = 3
        mock_gen.side_effect = RetryError(last_attempt=state)
        result = transcribe_audio(b"\x00\x01\x02")
        assert result == ""
