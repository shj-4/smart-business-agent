"""
اختبارات ارتدادية للمسألة #8: كل استدعاء يحتمل شبكة (app.exchange: تحويل عملات)
من المعالجات غير المتزامنة (async) يجب أن يُنفَّذ عبر asyncio.to_thread خارج
حلقة الأحداث كي لا يتجمّد البوت بأكمله عند بطء الشبكة أو انقطاعها.

معيار الاختبار حاسم: الخيط المنفِّذ لعملية التبادل يختلف عن خيط حلقة الأحداث.
"""

import asyncio
import threading
from decimal import Decimal
from types import SimpleNamespace

import app.exchange as exchange_mod
import bot.conversation as conversation
import bot.handlers as handlers
import bot.menus as menus

USER_A = 111


def _fake_session():
    return SimpleNamespace(close=lambda: None)


def _context(user_data=None):
    return SimpleNamespace(user_data=user_data or {})


def _build_query(data: str, user_id: int = USER_A):
    edited = []

    class _Query:
        def __init__(self):
            self.data = data
            self.from_user = SimpleNamespace(id=user_id)

        async def answer(self, *args, **kwargs):
            pass

        async def edit_message_text(self, text, **kwargs):
            edited.append((text, kwargs.get("reply_markup")))

    return _Query(), edited


def _recorder_convert_totals_to_base(recorded, total="123.45"):
    def fake(totals, base, stored=None):
        recorded["thread"] = threading.get_ident()
        return {"total": Decimal(total), "base": base}

    return fake


def _run_live(recorded, coro):
    async def go():
        recorded["loop"] = threading.get_ident()
        return await coro

    return asyncio.run(go())


class TestReportMetric:
    def test_exchange_conversion_runs_off_event_loop(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            exchange_mod,
            "convert_totals_to_base",
            _recorder_convert_totals_to_base(recorded),
        )
        monkeypatch.setattr(menus, "SessionLocal", _fake_session)
        monkeypatch.setattr(
            menus,
            "run_query",
            lambda db, uid, details: {
                "metric": "total_expenses",
                "period": "this_month",
                "result": {"USD": Decimal("100"), "ILS": Decimal("50")},
            },
        )
        q, edited = _build_query("rpt:m:total_expenses:this_month")
        _run_live(
            recorded,
            menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()),
        )
        assert recorded["thread"] != recorded["loop"]
        assert edited and "المجموع الموحّد" in edited[0][0]


class TestToolChart:
    def test_chart_conversion_runs_off_event_loop(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            exchange_mod,
            "convert_totals_to_base",
            _recorder_convert_totals_to_base(recorded, total="0.00"),
        )
        monkeypatch.setattr(menus, "SessionLocal", _fake_session)
        monkeypatch.setattr(
            "app.database.crud.monthly_totals",
            lambda db, uid, months=6, include_stored=False: [
                {
                    "label": "2026-09",
                    "by_currency": {
                        "USD": {"expense": 0, "income": 0},
                        "ILS": {"expense": 0, "income": 0},
                    },
                    "stored": None,
                    "stored_base": "ILS",
                }
            ],
        )
        q, edited = _build_query("tool:chart")
        _run_live(
            recorded,
            menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()),
        )
        assert recorded["thread"] != recorded["loop"]
        assert edited  # المعالج اكتمل دون تجميد الحلقة


class TestBudgetList:
    def test_budget_payload_runs_off_event_loop(self, monkeypatch):
        recorded = {}

        def fake_payload(db, uid):
            recorded["thread"] = threading.get_ident()
            return ["ميزانياتك:"], []

        monkeypatch.setattr(menus, "_budget_list_payload", fake_payload)
        monkeypatch.setattr(menus, "SessionLocal", _fake_session)
        q, edited = _build_query("bg:list")
        _run_live(
            recorded,
            menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()),
        )
        assert recorded["thread"] != recorded["loop"]
        assert edited and "ميزانياتك" in edited[0][0]


class TestConvertCommand:
    def test_convert_runs_off_event_loop(self, monkeypatch):
        recorded = {}

        def fake_convert(amount, from_cur, to_cur):
            recorded["thread"] = threading.get_ident()
            return {"amount": "300", "rate": "3.5", "result": "1050", "from": "USD", "to": "ILS"}

        monkeypatch.setattr(handlers, "convert", fake_convert)
        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=reply_text),
            effective_user=SimpleNamespace(id=USER_A),
        )
        ctx = SimpleNamespace(args=["300", "USD", "ILS"], user_data={})
        _run_live(recorded, handlers.convert_command(update, ctx))
        assert recorded["thread"] != recorded["loop"]
        assert replies and "1050" in replies[0]


class TestConvertAddValue:
    def test_convert_runs_off_event_loop(self, monkeypatch):
        recorded = {}

        def fake_convert(amount, from_cur, to_cur):
            recorded["thread"] = threading.get_ident()
            return {"amount": "300", "rate": "3.5", "result": "1050", "from": "USD", "to": "ILS"}

        monkeypatch.setattr(exchange_mod, "convert", fake_convert)
        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        update = SimpleNamespace(
            message=SimpleNamespace(text="300 دولار إلى شيكل", reply_text=reply_text),
            effective_user=SimpleNamespace(id=USER_A),
        )
        ctx = _context({"pending_convert": True})
        _run_live(recorded, conversation.convert_add_value(update, ctx))
        assert recorded["thread"] != recorded["loop"]
        assert replies and "1050" in replies[0]


class TestConfirmYes:
    def test_save_record_runs_off_event_loop(self, monkeypatch):
        recorded = {}

        def fake_save(db, uid, result, raw, msg_id=None):
            recorded["thread"] = threading.get_ident()
            return "تم حفظ العملية."

        monkeypatch.setattr(conversation, "save_record", fake_save)
        monkeypatch.setattr(conversation, "SessionLocal", _fake_session)

        query, edited = _build_query("confirm:yes")
        update = SimpleNamespace(callback_query=query)
        ctx = _context(
            {
                "confirm_result": {"type": "expense", "amount": "100", "currency": "ILS"},
                "confirm_raw": "100 شيكل",
                "confirm_message_id": 1,
            }
        )
        _run_live(recorded, conversation.confirm_yes(update, ctx))
        assert recorded["thread"] != recorded["loop"]
        assert edited and "تم حفظ" in edited[0][0]


class TestQueryIntent:
    def test_query_intent_runs_off_event_loop(self, monkeypatch):
        recorded = {}

        def fake_handle(result, uid):
            recorded["thread"] = threading.get_ident()
            return "نتيجة تجريبية"

        monkeypatch.setattr(conversation, "handle_query_intent", fake_handle)
        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=reply_text),
            effective_user=SimpleNamespace(id=USER_A),
        )
        _run_live(recorded, conversation._handle_query(update, {"intent": "query"}))
        assert recorded["thread"] != recorded["loop"]
        assert replies and "نتيجة تجريبية" in replies[0]
