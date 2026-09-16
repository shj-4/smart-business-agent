"""
Unit tests للتنبؤات (/forecast) وانحراف الإنفاق (/deviation) — منطق crud وعرض formatters.
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import bot.reminders as reminders_mod
from app.database import models  # noqa: F401
from app.database.crud import (
    _linear_forecast,
    create_transaction,
    deviation_summary,
    forecast_totals,
)
from app.timeutil import now_local, to_utc_naive
from bot.formatters import format_deviation, format_forecast
from bot.reminders import deviation_check

USER_A = 111
CUR = "USD"


def _seed_tx(db, telegram_user_id, kind, amount, year, month, day=15):
    """ينشئ معاملة بتاريخ في الماضي (المعرّف يبقى كما هو بعد تعديل created_at)."""
    tx = create_transaction(
        db,
        telegram_user_id,
        {"type": kind, "amount": amount, "currency": CUR},
        raw_message=f"{kind} {amount}",
    )
    local = datetime(year, month, day, 12, 0)
    tx.created_at = to_utc_naive(local)
    db.commit()
    db.refresh(tx)
    return tx


class TestLinearForecast:
    def test_insufficient_data_returns_none(self):
        assert _linear_forecast([], 3) == [None, None, None]
        assert _linear_forecast([Decimal("5")], 1) == [None]

    def test_constant_series_stays_constant(self):
        out = _linear_forecast([Decimal("100")] * 4, 2)
        assert out == [Decimal("100.0"), Decimal("100.0")]

    def test_increasing_series_trends_up(self):
        out = _linear_forecast([Decimal("100"), Decimal("200"), Decimal("300")], 1)
        assert out[0] > Decimal("300")

    def test_never_negative(self):
        out = _linear_forecast([Decimal("10"), Decimal("2")], 2)
        assert all(v >= Decimal("0.00") for v in out)


class TestForecastTotals:
    def test_empty_db_returns_no_months(self, db_session):
        payload = forecast_totals(db_session, USER_A, months=2)
        assert payload["months"] == []
        assert payload["last_key"] is None

    def test_predicts_constant_expense(self, db_session):
        y, m = now_local().year, now_local().month
        # ستة أشهر ثابتة من المصروفات تغطي نافذة التاريخ بالكامل (تساوي القيم المتوقعة)
        for offset in (5, 4, 3, 2, 1, 0):
            yy, mm = y, m - offset
            while mm < 1:
                mm += 12
                yy -= 1
            _seed_tx(db_session, USER_A, "expense", 1000, yy, mm)

        payload = forecast_totals(db_session, USER_A, months=2, history=6)
        assert payload["last_key"].startswith(f"{y:04d}-")
        assert len(payload["months"]) == 2
        for month in payload["months"]:
            assert month["expense"][CUR] == Decimal("1000.00")
        # العملة الأساس موحّدة إن أمكن غير أنها إرشادية — لا نفحص None
        assert payload["base"]


class TestDeviationSummary:
    def test_high_spike_is_significant(self, db_session):
        y, m = now_local().year, now_local().month
        yy, mm = y, m - 1
        while mm < 1:
            mm += 12
            yy -= 1
        _seed_tx(db_session, USER_A, "expense", 500, yy, mm)
        yy, mm = y, m - 2
        while mm < 1:
            mm += 12
            yy -= 1
        _seed_tx(db_session, USER_A, "expense", 500, yy, mm)
        yy, mm = y, m - 3
        while mm < 1:
            mm += 12
            yy -= 1
        _seed_tx(db_session, USER_A, "expense", 500, yy, mm)
        _seed_tx(db_session, USER_A, "expense", 1000, y, m)

        payload = deviation_summary(db_session, USER_A)
        exp = [d for d in payload["deviations"] if d["kind"] == "expense" and d["currency"] == CUR]
        assert exp and exp[0]["current"] == Decimal("1000")
        assert exp[0]["average"] == Decimal("500.00")
        assert exp[0]["pct"] == 100.0
        assert exp[0]["significant"] is True

    def test_no_baseline_skipped(self, db_session):
        y, m = now_local().year, now_local().month
        _seed_tx(db_session, USER_A, "income", 100, y, m)
        payload = deviation_summary(db_session, USER_A)
        incomes = [d for d in payload["deviations"] if d["kind"] == "income"]
        assert incomes == []

    def test_full_decrease_to_zero_is_significant(self, db_session):
        """انخفاض الإنفاق إلى صفر تمامًا (-100%) أكبر انحراف ممكن — يجب أن
        يُصنَّف ملحوظًا مثل نظيره من الزيادة (متماثل)."""
        y, m = now_local().year, now_local().month
        for offset in (1, 2, 3):
            yy, mm = y, m - offset
            while mm < 1:
                mm += 12
                yy -= 1
            _seed_tx(db_session, USER_A, "expense", 500, yy, mm)
        # لا معاملة مصروف في الشهر الحالي → current 0 مقابل متوسط 500

        payload = deviation_summary(db_session, USER_A)
        exp = [d for d in payload["deviations"] if d["kind"] == "expense" and d["currency"] == CUR]
        assert exp and exp[0]["current"] == Decimal("0")
        assert exp[0]["average"] == Decimal("500.00")
        assert exp[0]["pct"] == -100.0
        assert exp[0]["significant"] is True

    def test_empty_db_no_deviations(self, db_session):
        assert deviation_summary(db_session, USER_A)["deviations"] == []

    def test_reports_partial_month_metadata(self, db_session):
        payload = deviation_summary(db_session, USER_A)
        assert isinstance(payload["month_partial"], bool)
        assert 0 < payload["month_elapsed_pct"] <= 100


class TestForecastFormatters:
    def test_format_forecast_empty(self):
        text = format_forecast({"months": [], "history": 6})
        assert "لا توجد بيانات" in text

    def test_format_forecast_months(self):
        text = format_forecast(
            {
                "months": [
                    {
                        "label": "10/2026",
                        "expense": {"USD": Decimal("1000.00")},
                        "income": {"USD": Decimal("1200.00")},
                        "unified_expense": None,
                        "unified_income": None,
                    }
                ],
                "history": 6,
                "last_key": "2026-09",
                "base": "ILS",
            }
        )
        assert "10/2026" in text
        assert "1000.00" in text

    def test_format_deviation_empty(self):
        assert "لا توجد انحرافات" in format_deviation({"threshold_pct": 30, "deviations": []})

    def test_format_deviation_rows(self):
        text = format_deviation(
            {
                "threshold_pct": 30,
                "deviations": [
                    {
                        "currency": "USD",
                        "kind": "expense",
                        "current": Decimal("1000"),
                        "average": Decimal("500"),
                        "pct": 100.0,
                        "significant": True,
                    }
                ],
            }
        )
        assert "USD" in text
        assert "100.0%" in text
        assert "⚠️" in text

    def test_format_deviation_partial_notice(self):
        text = format_deviation(
            {
                "threshold_pct": 30,
                "month_partial": True,
                "month_elapsed_pct": 46,
                "deviations": [
                    {
                        "currency": "USD",
                        "kind": "expense",
                        "current": Decimal("1000"),
                        "average": Decimal("500"),
                        "pct": 100.0,
                        "significant": True,
                    }
                ],
            }
        )
        assert "month_partial" not in text
        assert "46%" in text
        assert "⚠️" in text

    def test_format_deviation_no_notice_when_complete(self):
        text = format_deviation(
            {
                "threshold_pct": 30,
                "month_partial": False,
                "month_elapsed_pct": 100,
                "deviations": [],
            }
        )
        assert "لا توجد انحرافات" in text
        assert "⚠️" not in text


@pytest.fixture
def db_env(monkeypatch):
    """بيئة db في الذاكرة مع SessionLocal معمّق في bot.reminders (لاختبار التنبيه)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(reminders_mod, "SessionLocal", SessionMaker)
    yield SessionMaker
    models.Base.metadata.drop_all(engine)
    engine.dispose()


class TestDeviationCheck:
    def test_sends_nothing_when_no_deviation(self, db_env):
        SessionMaker = db_env
        db = SessionMaker()
        y, m = now_local().year, now_local().month
        _seed_tx(db, USER_A, "expense", 500, y, m)
        db.close()

        context = MagicMock()
        context.bot.send_message = AsyncMock()
        import asyncio

        asyncio.run(deviation_check(context))
        context.bot.send_message.assert_not_called()

    def test_sends_once_when_significant(self, db_env):
        SessionMaker = db_env
        db = SessionMaker()
        y, m = now_local().year, now_local().month
        _seed_tx(db, USER_A, "expense", 500, y, m - 1 if m > 1 else 12, m - 1 if m > 1 else 12)
        _seed_tx(db, USER_A, "expense", 500, y, m - 2 if m > 2 else 12, m - 2 if m > 2 else 12)
        _seed_tx(db, USER_A, "expense", 1000, y, m)
        db.close()

        context = MagicMock()
        context.bot.send_message = AsyncMock()
        import asyncio

        asyncio.run(deviation_check(context))
        assert context.bot.send_message.called
        sent = [call.kwargs.get("text") or call.args[1] for call in context.bot.send_message.call_args_list]
        assert any("/deviation" in s for s in sent)

        # الحارس اليومي يمنع التكرار في نفس اليوم
        context.bot.send_message.reset_mock()
        import asyncio

        asyncio.run(deviation_check(context))
        context.bot.send_message.assert_not_called()
