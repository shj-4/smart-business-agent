"""
Unit tests للتذكيرات التلقائية (bot/reminders) — بلا شبكة.

يستخدم mock لجلسة قاعدة البيانات (في الذاكرة) وmock لـ context.bot.
"""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import bot.reminders as reminders_mod
from app.database import models  # noqa: F401
from app.database.crud import create_task
from app.timeutil import now_utc
from bot.reminders import overdue_check, setup_overdue_reminder

USER_A = 111


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
    context.bot.send_message = MagicMock()
    return context


class TestOverdueCheck:
    def test_sends_nothing_when_no_tasks(self, db_env):
        context = _make_context()
        overdue_check(context)
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
        overdue_check(context)
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
        overdue_check(context)
        assert context.bot.send_message.call_count == 1
        sent_text = context.bot.send_message.call_args.kwargs["text"]
        assert "متأخرة" in sent_text

        # الفحص الثاني لا يرسل مجددًا (reminder_sent=True)
        context2 = _make_context()
        overdue_check(context2)
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
        overdue_check(context)
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
