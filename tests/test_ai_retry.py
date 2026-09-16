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

    @patch("app.ai_service.client.models.generate_content")
    def test_client_4xx_errors_are_not_retried(self, mock_gen):
        """أخطاء عميل دائمة (4xx عدا 429) لا تُعاد محاولتها — تُفشل فورًا."""
        from app.ai_service import _is_retryable

        class ClientBadRequest(Exception):
            code = 400

        mock_gen.side_effect = ClientBadRequest("invalid argument")
        assert _is_retryable(ClientBadRequest("x")) is False
        result = analyze_message("دفعت 300 شيكل")
        assert result["intent"] == "unknown"
        assert mock_gen.call_count == 1

    @patch("app.ai_service.client.models.generate_content")
    def test_rate_limit_429_is_retried_and_waits_respecting_retry_after(self, mock_gen):
        """429 يُعاد مع احترام Retry-After (لا انتظار تصاعدي أعمى)."""
        from app.ai_service import _extract_retry_after

        class RateLimited(Exception):
            code = 429
            retry_after = 1

        assert _extract_retry_after(RateLimited("limit")) == 1.0
        good_response = MagicMock()
        good_response.text = json.dumps(
            {
                "intent": "record",
                "type": "expense",
                "amount": 100,
                "currency": "شيكل",
                "person": None,
                "description": "دفعة",
                "date": None,
                "missing_fields": [],
                "query_details": None,
            }
        )
        mock_gen.side_effect = [RateLimited("quota"), good_response]
        result = analyze_message("دفعت 100 شيكل")
        assert result["intent"] == "record"
        assert mock_gen.call_count == 2

    def test_retry_wait_honors_retry_after_and_caps(self):
        """الانتظار خلف 429 = قيمة Retry-After (محدودة بسقف)، وبدونه تصاعدي."""
        from tenacity import RetryCallState

        from app.ai_service import _extract_retry_after, _retry_wait

        class HitLimit(Exception):
            code = 429
            retry_after = 9999

        state = RetryCallState(retry_object=None, fn=MagicMock(), args=(), kwargs={})
        state.outcome = MagicMock()
        state.outcome.exception.return_value = HitLimit("limit")
        assert _extract_retry_after(HitLimit("limit")) == 60  # السقف 60 ثانية
        assert _retry_wait(state) == 60

        state2 = RetryCallState(retry_object=None, fn=MagicMock(), args=(), kwargs={})
        state2.outcome = MagicMock()
        state2.outcome.exception.return_value = ConnectionError("net")
        state2.attempt_number = 1
        assert _retry_wait(state2) == 1.0  # تصاعدي: 2^0

    def test_httpx_timeout_is_retryable(self):
        """httpx.TimeoutException يُعاد لأنه خطأ شبكة مؤقت."""
        import httpx

        from app.ai_service import _is_retryable

        assert _is_retryable(httpx.TimeoutException("timeout")) is True

    def test_httpx_connect_error_is_retryable(self):
        """httpx.ConnectError يُعاد لأنه خطأ اتصال."""
        import httpx

        from app.ai_service import _is_retryable

        assert _is_retryable(httpx.ConnectError("connect failed")) is True

    def test_unclassified_exception_is_not_retryable(self):
        """استثناء غير مصنّف لا يُعاد — يُفشل فورًا."""
        from app.ai_service import _is_retryable
        assert _is_retryable(RuntimeError("bug")) is False
        assert _is_retryable(ValueError("bad input")) is False
        assert _is_retryable(KeyError("missing")) is False


class TestTranscribeAudioRetry:
    """اختبار retry في transcribe_audio عند فشل generate_content."""

    @patch("app.ai_service.client.models.generate_content")
    def test_returns_empty_on_all_retries_failed(self, mock_gen):
        """إذا فشلت كل المحاولات (خطأ عابر معروف)، تُرجع نص فارغ."""
        mock_gen.side_effect = ConnectionError("service unavailable")
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

    @patch("app.ai_service.client.models.generate_content")
    def test_unclassified_exceptions_are_not_retried(self, mock_gen):
        """استثناء غير مصنّف (RuntimeError) لا يُعاد — يُفشل فورًا."""
        mock_gen.side_effect = RuntimeError("programming bug")
        result = transcribe_audio(b"\x00\x01\x02")
        assert result == ""
        assert mock_gen.call_count == 1
