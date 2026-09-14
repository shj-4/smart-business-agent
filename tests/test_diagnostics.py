"""
Unit tests للفحص الصحي (/health) وأمر /stats — بلا أي شبكة.

- run_health_checks: يعيد قائمة فحوصات منظمة {ok, label, detail}.
- health_command/stats_command: يعرضان نصًا عبر reply_text.
"""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.db import Base
from bot import handlers
from bot.diagnostics import run_health_checks
from bot.formatters import format_health_report, format_user_stats

_VALID_B64_KEY = base64.b64encode(b"0" * 32).decode("ascii")


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionMaker()
    yield db
    db.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


def _text_command_context():
    """update.effective_user + message.reply_text تلتقط النص المرسل."""
    replies = []

    class _Msg:
        async def reply_text(self, text, **kwargs):
            replies.append(text)

    update = SimpleNamespace(effective_user=SimpleNamespace(id=111), message=_Msg())
    return update, replies


class TestHealthChecks:
    def test_returns_structured_checks(self, db_env):
        checks = run_health_checks(db_env)
        assert isinstance(checks, list)
        assert len(checks) >= 5
        for c in checks:
            assert {"ok", "label", "detail"} <= set(c)
            assert isinstance(c["ok"], bool)

    def test_has_required_checks(self, db_env):
        labels = {c["label"] for c in run_health_checks(db_env)}
        assert "قاعدة البيانات" in labels
        assert "الهجرات" in labels
        assert "المتغيرات المطلوبة" in labels
        assert "الإصدارات" in labels

    def test_db_check_ok_on_working_engine(self, db_env):
        db_check = [c for c in run_health_checks(db_env) if c["label"] == "قاعدة البيانات"][0]
        assert db_check["ok"] is True

    def test_db_check_fails_on_broken_engine(self):
        class _BrokenBind:
            def connect(self):
                raise RuntimeError("gone")

        class _BrokenSession:
            bind = _BrokenBind()

        db_check = [c for c in run_health_checks(_BrokenSession()) if c["label"] == "قاعدة البيانات"][0]
        assert db_check["ok"] is False

    def test_versions_detail_lists_libraries(self, db_env):
        versions = [c for c in run_health_checks(db_env) if c["label"] == "الإصدارات"][0]
        assert "Python" in versions["detail"]
        assert "SQLAlchemy" in versions["detail"]

    def test_migration_check_without_alembic_table_is_warn_not_crash(self, db_env):
        migration = [c for c in run_health_checks(db_env) if c["label"] == "الهجرات"][0]
        # في قاعدة tests (create_all بلا alembic_version) لا يوجد الجدول → إنذار لا انهيار
        assert isinstance(migration["ok"], bool)
        assert "الهدف" in migration["detail"]

    def test_format_health_report_renders_marks(self, db_env):
        text = format_health_report(run_health_checks(db_env))
        assert "🩺" in text
        assert "✅" in text


class TestEncryptionCheck:
    def _encryption(self, db_env):
        return [c for c in run_health_checks(db_env) if c["label"] == "التشفير"][0]

    def test_valid_key_is_ok(self, db_env, monkeypatch):
        monkeypatch.setattr("app.config.settings.encryption_key", _VALID_B64_KEY)
        check = self._encryption(db_env)
        assert check["ok"] is True
        assert not check.get("warn")

    def test_missing_key_is_warn_not_failure(self, db_env, monkeypatch):
        monkeypatch.setattr("app.config.settings.encryption_key", "")
        check = self._encryption(db_env)
        assert check["ok"] is True
        assert check["warn"] is True

    def test_invalid_key_flags_check(self, db_env, monkeypatch):
        monkeypatch.setattr("app.config.settings.encryption_key", "abcd")
        check = self._encryption(db_env)
        assert check["ok"] is False
        assert check["warn"] is True
        assert "غير صالح" in check["detail"]


class TestHealthCommand:
    def test_replies_report(self, monkeypatch, db_env):
        monkeypatch.setattr(handlers, "SessionLocal", lambda: db_env)
        update, replies = _text_command_context()
        _run(handlers.health_command(update, SimpleNamespace()))
        assert replies
        assert "🩺 الفحص الصحي" in replies[0]


class TestStatsCommand:
    def test_replies_stats_text(self, monkeypatch, db_env):
        from app.database.crud import create_note, create_transaction

        create_transaction(
            db_env, 111, {"type": "expense", "amount": 300, "currency": "ILS"}, "دفعة"
        )
        create_note(db_env, 111, {"type": "order", "description": "مواد"}, "طلبية")
        monkeypatch.setattr(handlers, "SessionLocal", lambda: db_env)
        update, replies = _text_command_context()
        _run(handlers.stats_command(update, SimpleNamespace()))
        assert replies
        assert "📊" in replies[0]


class TestFormatStats:
    def test_format_user_stats_renders_sections(self, db_env):
        from app.database.crud import create_note, create_transaction, user_stats

        create_transaction(
            db_env, 111, {"type": "expense", "amount": 300, "currency": "ILS"}, "دفعة"
        )
        create_transaction(
            db_env, 111, {"type": "income", "amount": 500, "currency": "ILS"}, "استلام"
        )
        create_note(db_env, 111, {"type": "order", "description": "مواد"}, "طلبية")
        stats = user_stats(db_env, 111)
        text = format_user_stats(stats)
        assert "📊" in text
        assert "300.00 ILS" in text
        assert "500.00 ILS" in text
        assert stats["transactions"]["total"] == 2
        assert stats["transactions"]["expense"] == 1
        assert stats["transactions"]["income"] == 1
        assert stats["orders"]["open"] == 1
        assert stats["notes"] == 1
        assert stats["db_size_bytes"] is None or isinstance(stats["db_size_bytes"], int)


class TestUserStatsCounts:
    def test_peak_hour_uses_local_clock(self, db_env):
        from datetime import datetime

        from app.database.crud import create_transaction, user_stats
        from app.timeutil import to_utc_naive

        tx = create_transaction(
            db_env, 111, {"type": "expense", "amount": 100, "currency": "ILS"}, "دفعة"
        )
        tx.created_at = to_utc_naive(datetime(2026, 9, 1, 10, 0, 0))  # 10:00 محليًا
        db_env.commit()
        stats = user_stats(db_env, 111)
        assert stats["peak_hour_local"] == 10
        assert stats["peak_activity"] == 1

    def test_invoices_tasks_budgets_counts(self, db_env):
        from app.database.crud import (
            create_invoice,
            create_task,
            create_transaction,
            set_credit_limit,
            user_stats,
        )

        create_invoice(db_env, 111, {"person": "مورّد", "amount": 500, "currency": "ILS"}, "فاتورة")
        task = create_task(
            db_env,
            111,
            {"description": "الاتصال بالمورّد", "date": "2026-09-10 10:00"},
            raw_message="مهمة الاتصال",
        )
        task.status = "done"
        db_env.commit()
        set_credit_limit(db_env, 111, "خالد", "2000")
        create_transaction(
            db_env, 111, {"type": "expense", "amount": 100, "currency": "ILS"}, "دفعة"
        )
        stats = user_stats(db_env, 111)
        assert stats["invoices"]["pending"] == 1
        assert stats["tasks"]["done"] == 1
        assert stats["credit_limits"] == 1
        assert stats["workspace_size"] == 1
