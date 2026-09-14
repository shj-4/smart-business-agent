"""
Unit tests للتذكيرات التلقائية (bot/reminders) — بلا شبكة.

يستخدم mock لجلسة قاعدة البيانات (في الذاكرة) وmock لـ context.bot.
الدوال المُنفَّذة عبر job_queue يجب أن تكون async (await callback) — تُختبر
عبر asyncio.run مثل مسار Job.run الحقيقي.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import bot.reminders as reminders_mod
from app.database import models  # noqa: F401
from app.database.crud import create_invoice, create_task
from app.timeutil import now_utc
from bot.reminders import (
    budget_check,
    credit_check,
    daily_backup_job,
    deviation_check,
    invoice_check,
    overdue_check,
    periodic_report_job,
    setup_credit_check,
    setup_invoice_check,
    setup_overdue_reminder,
)

USER_A = 111


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_env(monkeypatch):
    """بيئة db في الذاكرة مع SessionLocal معمّق في bot.reminders."""
    from app.database.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(reminders_mod, "SessionLocal", SessionMaker)

    yield SessionMaker

    Base.metadata.drop_all(engine)
    engine.dispose()


def _make_context():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return context


class TestOverdueCheck:
    def test_sends_nothing_when_no_tasks(self, db_env):
        context = _make_context()
        _run(overdue_check(context))
        context.bot.send_message.assert_not_called()

    def test_no_notification_for_pending_future_task(self, db_env):
        SessionMaker = db_env
        db = SessionMaker()
        create_task(
            db,
            USER_A,
            {"description": "مستقبلية", "date": "2099-01-01 10:00"},
            raw_message="مستقبلية",
        )
        db.close()

        context = _make_context()
        _run(overdue_check(context))
        context.bot.send_message.assert_not_called()

    def test_notifies_for_overdue_task_once(self, db_env):
        SessionMaker = db_env
        db = SessionMaker()
        past = (now_utc() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        create_task(
            db,
            USER_A,
            {"description": "متأخرة", "date": past},
            raw_message="متأخرة",
        )
        db.close()

        context = _make_context()
        _run(overdue_check(context))
        assert context.bot.send_message.call_count == 1
        sent_text = context.bot.send_message.call_args.kwargs["text"]
        assert "متأخرة" in sent_text

        # الفحص الثاني لا يرسل مجددًا (reminder_sent=True)
        context2 = _make_context()
        _run(overdue_check(context2))
        context2.bot.send_message.assert_not_called()

    def test_no_reminder_for_done_task(self, db_env):
        SessionMaker = db_env
        db = SessionMaker()
        past = (now_utc() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        task = create_task(
            db,
            USER_A,
            {"description": "منجزة", "date": past},
            raw_message="منجزة",
        )
        task.status = "done"
        db.commit()
        db.close()

        context = _make_context()
        _run(overdue_check(context))
        context.bot.send_message.assert_not_called()


class TestSetupReminder:
    def test_registers_job_when_queue_available(self):
        app = MagicMock()
        app.job_queue = MagicMock()
        setup_overdue_reminder(app)
        app.job_queue.run_repeating.assert_called_once()

    def test_skips_when_queue_missing(self):
        app = MagicMock()
        app.job_queue = None
        # يجب ألا يرفع استثناء ولا يحاول الاتصال
        setup_overdue_reminder(app)


class TestInvoiceCheck:
    def test_marks_and_notifies_overdue_invoice(self, db_env):
        db = db_env()
        past = now_utc() - timedelta(days=2)
        inv = create_invoice(
            db, USER_A, {"person": "مورّد", "amount": 700, "currency": "ILS", "due_date": past}, "فاتورة"
        )
        db.close()

        context = _make_context()
        _run(invoice_check(context))
        assert context.bot.send_message.called

        # التكرار الثاني لا يرسل مجددًا (alerted=True)
        context2 = _make_context()
        _run(invoice_check(context2))
        context2.bot.send_message.assert_not_called()

        db = db_env()
        assert db.query(models.Invoice).filter_by(id=inv.id).first().status == "overdue"
        db.close()

    def test_future_invoice_no_alert(self, db_env):
        db = db_env()
        future = now_utc() + timedelta(days=5)
        create_invoice(db, USER_A, {"person": "مورّد", "amount": 700, "currency": "ILS", "due_date": future}, "فاتورة")
        db.close()
        context = _make_context()
        _run(invoice_check(context))
        context.bot.send_message.assert_not_called()


class TestCreditCheck:
    def test_alerts_over_credit_once(self, db_env):
        from app.database.crud import create_transaction, set_credit_limit

        db = db_env()
        set_credit_limit(db, USER_A, "خالد", "1000")
        create_transaction(
            db, USER_A, {"type": "expense", "amount": 1200, "currency": "ILS", "person": "خالد"}, "دفعة"
        )
        db.close()

        context = _make_context()
        _run(credit_check(context))
        assert context.bot.send_message.called

        context2 = _make_context()
        _run(credit_check(context2))
        context2.bot.send_message.assert_not_called()

        db = db_env()
        row = db.query(models.CreditLimit).filter_by(person="خالد").first()
        assert row.alerted_status == 2
        db.close()

    def test_no_credit_no_alert(self, db_env):
        db = db_env()
        db.close()
        context = _make_context()
        _run(credit_check(context))
        context.bot.send_message.assert_not_called()

    def test_alerts_receivable_side_for_customer(self, db_env):
        """عميل مدين لك (income > expense) يتجاوز سقفه — يجب أن يُنبَّه (كان يتخطَّى سابقًا)."""
        from app.database.crud import create_transaction, set_credit_limit

        db = db_env()
        set_credit_limit(db, USER_A, "سامر", "1000")
        create_transaction(
            db, USER_A, {"type": "income", "amount": 1500, "currency": "ILS", "person": "سامر"}, "بيع بالأجل"
        )
        db.close()

        context = _make_context()
        _run(credit_check(context))
        assert context.bot.send_message.called
        sent = context.bot.send_message.call_args.kwargs["text"]
        assert "سامر" in sent
        assert "مدين لك" in sent

        db = db_env()
        row = db.query(models.CreditLimit).filter_by(person="سامر").first()
        assert row.alerted_status == 2
        db.close()


class TestSetupReminderB3:
    def test_registers_invoice_and_credit_jobs(self):
        for setup in (setup_invoice_check, setup_credit_check):
            app = MagicMock()
            app.job_queue = MagicMock()
            setup(app)
            app.job_queue.run_repeating.assert_called_once()

    def test_skips_invoice_and_credit_without_queue(self):
        for setup in (setup_invoice_check, setup_credit_check):
            app = MagicMock()
            app.job_queue = None
            setup(app)  # لا استثناء


class TestJobCallbacksAreCoroutines:
    """JobQueue في PTB v20+ ينفّذ `await callback(context)` — أي callback
    sync يُرجع None فيفشل بـ TypeError في كل تشغيل. جميع الجوبات يجب أن تكون
    async (تضمن عدم عودة الخلل بصمت)."""

    def test_all_callbacks_are_coroutine_functions(self):
        import inspect

        callbacks = [
            overdue_check,
            budget_check,
            invoice_check,
            credit_check,
            periodic_report_job,
            deviation_check,
            daily_backup_job,
        ]
        for cb in callbacks:
            assert inspect.iscoroutinefunction(cb), f"{cb.__name__} ليست async"

    def test_callback_executes_via_await(self, db_env):
        """المسار الحقيقي: await callback(context) يعمل وينفّذ body الدالة."""
        context = _make_context()
        _run(overdue_check(context))
        context.bot.send_message.assert_not_called()
