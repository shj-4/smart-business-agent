"""
اختبارات وحدة لتحويل العملات (app/exchange) — بلا شبكة (mock كامل).
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from app import exchange
from app.exchange import convert, get_rate


@pytest.fixture(autouse=True)
def _clear_cache():
    """يمسح التخزين المؤقت للأسعار بين كل اختبار (لا يعتمد الاختبار على ترتيب)."""
    exchange._cache.clear()
    exchange._last_rates.clear()
    yield
    exchange._cache.clear()
    exchange._last_rates.clear()


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
