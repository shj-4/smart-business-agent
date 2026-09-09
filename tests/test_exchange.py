"""
اختبارات وحدة لتحويل العملات (app/exchange) — بلا شبكة (mock كامل).
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app import exchange
from app.exchange import convert, get_rate


@pytest.fixture(autouse=True)
def _clear_cache():
    """يمسح التخزين المؤقت للأسعار وحالة قاطع الدائرة بين كل اختبار."""
    exchange._cache.clear()
    exchange._last_rates.clear()
    exchange.reset_breaker()
    yield
    exchange._cache.clear()
    exchange._last_rates.clear()
    exchange.reset_breaker()


class TestConvert:
    def test_same_currency_no_conversion(self):
        result = convert("300", "ILS", "ILS")
        assert result["result"] == Decimal("300.00")
        assert result["rate"] == Decimal("1.0000")

    def test_invalid_amount(self):
        result = convert("abc", "USD", "ILS")
        assert "error" in result

    @patch("app.exchange._fetch_rates")
    def test_conversion_normal_path(self, mock_fetch):
        mock_fetch.side_effect = lambda base: {"USD": 1.0, "ILS": 3.75}
        result = convert("100", "USD", "ILS")
        assert not result.get("error")
        assert result["from"] == "USD"
        assert result["to"] == "ILS"
        assert result["rate"] == Decimal("3.7500")
        assert result["result"] == Decimal("375.00")

    @patch("app.exchange._fetch_rates")
    def test_unavailable_rate_returns_error(self, mock_fetch):
        mock_fetch.return_value = {"USD": 1.0, "ILS": 3.75}
        result = convert("100", "USD", "XYZ")
        assert result.get("error")

    @patch("app.exchange._fetch_rates")
    def test_fetch_failure_returns_error(self, mock_fetch):
        mock_fetch.return_value = None
        result = convert("100", "USD", "ILS")
        assert result.get("error")


class TestGetRate:
    def test_same_currency(self):
        assert get_rate("usd", "USD") == Decimal("1.0000")

    @patch("app.exchange._fetch_rates")
    def test_case_insensitive_codes(self, mock_fetch):
        mock_fetch.return_value = {"USD": 1.0, "ILS": 3.75}
        assert get_rate("usd", "ils") == Decimal("3.7500")

    def test_currency_names_registered(self):
        assert "ILS" in exchange.CURRENCY_NAMES
        assert "USD" in exchange.CURRENCY_NAMES
        assert "JOD" in exchange.CURRENCY_NAMES


class TestConvertTotalsToBaseStored:
    """convert_totals_to_base مع مبالغ مثبّتة بعملة الأساس وقت التسجيل (#25)."""

    @patch("app.exchange.get_rate")
    def test_stored_amounts_replace_live_rate(self, mock_rate):
        """العملة المتاحة في stored تُحسب من المبلغ المخزّن بلا سعر اليوم."""
        totals = {"USD": Decimal("100"), "JOD": Decimal("50")}
        stored = {"USD": Decimal("370.00")}  # سعر مثبّت لحظة التسجيل (370 شيكل/دولار)
        mock_rate.side_effect = lambda frm, to: Decimal("5.0000")  # JOD→ILS حي

        conv = exchange.convert_totals_to_base(totals, "ILS", stored=stored)
        assert conv["total"] == Decimal("620.00")
        assert conv["rates"]["USD"] == Decimal("1.0000")
        assert conv["partial"] is False

    def test_without_stored_falls_back_to_live(self):
        """بلا stored (سجلات قديمة) يبقى السلوك السابق: سعر اليوم."""
        with patch("app.exchange.get_rate") as mock_rate:
            mock_rate.return_value = Decimal("3.7500")
            conv = exchange.convert_totals_to_base(
                {"USD": Decimal("100")}, "ILS", stored=None
            )
        assert conv["total"] == Decimal("375.00")

    def test_stored_currency_same_as_base_is_ignored(self):
        """عملة أساس في stored لا تُطبق مرتين — المجموع الأصلي يُستخدم مباشرة."""
        conv = exchange.convert_totals_to_base(
            {"ILS": Decimal("200")}, "ILS", stored={"ILS": Decimal("999")}
        )
        assert conv["total"] == Decimal("200.00")

    def test_stored_only_currency_counts(self):
        """عملة بمبلغ مخزَّن فقط (بلا مقابل في totals) تُضاف من قيمتها المثبَّتة."""
        conv = exchange.convert_totals_to_base({}, "ILS", stored={"USD": Decimal("370.00")})
        assert conv["total"] == Decimal("370.00")
        assert conv["partial"] is False


class TestCircuitBreaker:
    def test_hits_stop_after_threshold_failures(self):
        """بعد 3 فشل متتالٍ تُفتح الدائرة ولا تُجرَّب الشبكة مجددًا."""
        fake = MagicMock(return_value=None)
        with patch("app.exchange._fetch_rates", fake), patch.object(
            exchange, "_CB_THRESHOLD", 3
        ), patch.object(exchange, "_CB_COOLDOWN", 120):
            for _ in range(3):
                convert("100", "USD", "ILS")  # كل مرة فشل → عدّاد++
            assert exchange.breaker_open() is True
            calls_after_open = fake.call_count
            convert("100", "USD", "ILS")  # الدائرة مفتوحة → لا استدعاء شبكة
            assert fake.call_count == calls_after_open

    def test_success_resets_counter(self):
        """نجاح وسيط يعيد العدّاد للصفر فلا تُفتح الدائرة هكذا."""
        responses = [None, None, {"USD": 1.0, "ILS": 3.75}]
        fake = MagicMock(side_effect=lambda base: responses.pop(0))
        with patch("app.exchange._fetch_rates", fake), patch.object(
            exchange, "_CB_THRESHOLD", 3
        ):
            convert("100", "USD", "ILS")
            convert("100", "USD", "ILS")
            convert("100", "USD", "ILS")  # نجاح يعيد العدّاد
            assert exchange._BREAKER["failures"] == 0
            assert exchange.breaker_open() is False

    def test_stale_rates_still_used_while_open(self):
        """الدائرة المفتوحة تمنع الشبكة لكنها لا تمنع آخر سعر مخزّن (fallback صامت)."""
        _t = __import__("time")
        exchange._last_rates["USD"] = ({"USD": 1.0, "ILS": 3.75}, _t.time() - 7200)  # أقدم من TTL
        exchange._BREAKER["failures"] = 5
        exchange._BREAKER["open_until"] = _t.time() + 300

        mock_fetch = MagicMock()
        with patch("app.exchange._fetch_rates", mock_fetch):
            assert get_rate("USD", "ILS") == Decimal("3.7500")
            mock_fetch.assert_not_called()

    def test_reset_breaker(self):
        exchange._BREAKER["failures"] = 9
        exchange._BREAKER["open_until"] = 10**12
        exchange.reset_breaker()
        assert exchange._BREAKER["failures"] == 0
        assert exchange.breaker_open() is False
