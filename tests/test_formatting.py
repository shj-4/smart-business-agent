"""
اختبارات app/formatting (مصدر واحد للتنسيق المشترك):

- fmt_amount: أرقام بلا أصفار زائدة، أعداد صحيحة كما هي، وسقوط آمن.
- totals_line: دمج مجموع بعملات متعددة بسطر "+".
- current_month_key: مفتاح الشهر الحالي المحلي.
- حارس منع إعادة التكرار: لا يجوز إعادة تعريف هذه الدوال داخل bot.* أو crud.budgets.
"""

import inspect
import re
from decimal import Decimal

from app.formatting import current_month_key, fmt_amount, totals_line


class TestFmtAmount:
    def test_integer_stays_integer(self):
        assert fmt_amount(Decimal("1500")) == "1500"

    def test_trailing_zeros_stripped(self):
        assert fmt_amount(Decimal("300.50")) == "300.5"

    def test_fraction_preserved_without_padding(self):
        assert fmt_amount(Decimal("0.5")) == "0.5"

    def test_float_integral_keeps_exponent(self):
        assert fmt_amount(200.0) == "200.0"

    def test_unparseable_falls_back_to_str(self):
        assert fmt_amount("أحد عشر") == "أحد عشر"


class TestTotalsLine:
    def test_multi_currency_joined_with_plus(self):
        line = totals_line({"شيكل": Decimal("1500"), "USD": Decimal("200")})
        assert line == "1500 شيكل + 200 USD"

    def test_formatting_of_totals_matches_fmt_amount(self):
        line = totals_line({"ILS": Decimal("50.50"), "USD": Decimal("3")})
        assert line == "50.5 ILS + 3 USD"

    def test_empty_totals_returns_empty(self):
        assert totals_line({}) == ""


class TestCurrentMonthKey:
    def test_matches_yyyy_mm(self):
        assert re.fullmatch(r"\d{4}-\d{2}", current_month_key()) is not None


class TestNoDuplication:
    def test_bot_formatters_imports_shared_helpers(self):
        import bot.formatters
        import bot.reports

        src_f = inspect.getsource(bot.formatters)
        src_r = inspect.getsource(bot.reports)
        assert src_f.startswith("from app.formatting") or "app.formatting" in src_f.splitlines()[0] or "from app.formatting" in src_f
        assert "def _fmt_amount" not in src_f
        assert "def _totals_line" not in src_f
        assert "def _fmt_amount" not in src_r
        assert "def _totals_line" not in src_r

    def test_budgets_imports_shared_month_key(self):
        from app.database.crud import budgets

        assert "def _current_month_key" not in inspect.getsource(budgets)
