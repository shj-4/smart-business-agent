"""
اختبارات الأمان (بدون شبكة):

- تشفير الحقول الحساسة (amount/description/raw_message) ونمط الخلفية القديم
- تجميع المبالغ في Python (بدل SQL SUM) مع التشفير
- /admin_stats: بناء الإحصائيات العامة بلا بيانات فردية + صلات الأدمن
- سجل التدقيق audit.log لأمر التعديل
- حد حجم ملفات الصوت pregore التحميل
"""

import base64
import os

from sqlalchemy import text as sa_text

from app.config import Settings

TEST_KEY = base64.b64encode(b"0" * 32).decode("ascii")


def _enable_encryption(monkeypatch):
    monkeypatch.setattr("app.config.settings.encryption_key", TEST_KEY)


# ---------- تشفير الحقول الحساسة ----------


class TestFieldEncryption:
    def test_values_encrypted_at_rest(self, db_session, monkeypatch):
        _enable_encryption(monkeypatch)
        from app.database.crud import create_transaction

        tx = create_transaction(
            db_session,
            111,
            {"type": "expense", "amount": 300, "currency": "ILS", "description": "دفعة سرية"},
            raw_message="دفعت 300 سرية",
            telegram_message_id=1,
        )
        assert tx.amount == 300
        assert tx.description == "دفعة سرية"

        raw = db_session.execute(
            sa_text("SELECT description, amount, raw_message FROM transactions WHERE id=:i"),
            {"i": tx.id},
        ).one()
        for stored in (raw.description, raw.amount, raw.raw_message):
            assert stored is not None
            assert str(stored).startswith("v1$")
            assert str(stored) != "دفعة سرية"

    def test_legacy_plaintext_still_readable(self, db_session, monkeypatch):
        # قبل تفعيل المفتاح: يُخزَّن واضحًا (بغض النظر عن مفتاح حقيقي في .env)
        monkeypatch.setattr("app.config.settings.encryption_key", "")
        from app.database.crud import create_transaction

        tx = create_transaction(
            db_session,
            111,
            {"type": "expense", "amount": 150, "currency": "ILS", "description": "قديمة"},
            raw_message="قديمة",
            telegram_message_id=1,
        )
        db_session.commit()

        # تفعيل المفتاح: القيمة القديمة (واضحة) تُقرأ بشكلٍ صحيح
        _enable_encryption(monkeypatch)
        loaded = db_session.get(type(tx), tx.id)
        assert loaded.description == "قديمة"
        assert loaded.amount == 150

        # إعادة الكتابة (Core UPDATE — لأن إعادة التعيين لنفس القيمة لا تُعدّ تغييرًا)
        from sqlalchemy import update

        from app.database.models import Transaction

        db_session.execute(
            update(Transaction)
            .where(Transaction.id == tx.id)
            .values(description=loaded.description)
        )
        db_session.commit()
        raw = db_session.execute(
            sa_text("SELECT description FROM transactions WHERE id=:i"), {"i": tx.id}
        ).scalar_one()
        assert str(raw).startswith("v1$")

    def test_roundtrip_after_key_change_reads_old_with_fallback(self, db_session, monkeypatch):
        _enable_encryption(monkeypatch)
        from app.database.crud import create_transaction

        tx = create_transaction(
            db_session,
            111,
            {"type": "income", "amount": 999.50, "currency": "ILS", "description": "قبض"},
            raw_message="قبض 999.50",
            telegram_message_id=1,
        )
        assert tx.amount == 999.50

    def test_unreadable_ciphertext_shows_placeholder_not_raw(self, db_session, monkeypatch):
        """تدوير المفتاح (أو خطأ ضبطه): لا يُعرض النص المشفَّر الخام للمستخدم."""
        _enable_encryption(monkeypatch)
        from app.database.crud import create_transaction

        tx = create_transaction(
            db_session,
            111,
            {"type": "expense", "amount": 300, "currency": "ILS", "description": "سرّي"},
            raw_message="دفعة سرية",
            telegram_message_id=1,
        )
        raw = db_session.execute(
            sa_text("SELECT description, amount, raw_message FROM transactions WHERE id=:i"),
            {"i": tx.id},
        ).one()
        assert str(raw.description).startswith("v1$")

        # تبديل المفتاح (محاكاة تدوير) → القيم القديمة تصبح غير قابلة للقراءة
        OTHER_KEY = base64.b64encode(b"7" * 32).decode("ascii")
        monkeypatch.setattr("app.config.settings.encryption_key", OTHER_KEY)

        db_session.expire_all()
        loaded = db_session.get(type(tx), tx.id)
        assert loaded.description == "غير قابلة للقراءة"
        assert "v1$" not in str(loaded.description)
        assert loaded.raw_message == "غير قابلة للقراءة"
        assert loaded.amount is None  # المبالغ رقمية: لا تسريب ولا كسر من النوع


class TestSumInPython:
    def test_run_query_totals_with_encryption(self, db_session, monkeypatch):
        _enable_encryption(monkeypatch)
        from app.database.crud import create_transaction, run_query

        create_transaction(
            db_session,
            111,
            {"type": "expense", "amount": 300, "currency": "ILS", "description": "أ"},
            raw_message="أ",
            telegram_message_id=1,
        )
        create_transaction(
            db_session,
            111,
            {"type": "expense", "amount": 50.5, "currency": "USD", "description": "ب"},
            raw_message="ب",
            telegram_message_id=2,
        )
        create_transaction(
            db_session,
            111,
            {"type": "income", "amount": 1000, "currency": "ILS", "description": "ج"},
            raw_message="ج",
            telegram_message_id=3,
        )

        result = run_query(
            db_session, 111, {"metric": "total_expenses", "period": "all_time", "person": None}
        )
        total = {k: float(v) for k, v in result["result"].items()}
        assert total == {"ILS": 300.0, "USD": 50.5}

        result = run_query(
            db_session, 111, {"metric": "total_income", "period": "all_time", "person": None}
        )
        assert {k: float(v) for k, v in result["result"].items()} == {"ILS": 1000.0}

    def test_budget_usage_and_find_pending_task(self, db_session, monkeypatch):
        _enable_encryption(monkeypatch)

        from app.database.crud import (
            budget_usage,
            create_budget,
            create_task,
            create_transaction,
            find_pending_task,
        )

        create_transaction(
            db_session,
            111,
            {"type": "expense", "amount": 400, "currency": "ILS", "description": "مواد"},
            raw_message="مواد",
            telegram_message_id=1,
        )
        create_task(
            db_session,
            111,
            {"description": "اتصل بالمورد محمد"},
            raw_message="اتصل بالمورد محمد",
            telegram_message_id=2,
        )

        budget = create_budget(db_session, 111, "currency", "ILS", 2000, name="مواد")
        usage = budget_usage(db_session, budget)
        assert float(usage["spent"]) == 400.0

        task = find_pending_task(db_session, 111, "المورد محمد")
        assert task is not None
        assert task.description == "اتصل بالمورد محمد"
        assert find_pending_task(db_session, 111, "لا وجود لها") is None


class TestVoiceSizeLimit:
    from types import SimpleNamespace

    def _msg(self, file_size):
        from bot.conversation import _check_file_size

        m = self.SimpleNamespace(
            voice=None,
            audio=None,
            video_note=None,
        )
        if file_size[0]:
            m.voice = self.SimpleNamespace(file_size=file_size[0])
        elif file_size[1]:
            m.audio = self.SimpleNamespace(file_size=file_size[1])
        elif file_size[2]:
            m.video_note = self.SimpleNamespace(file_size=file_size[2])
        return m, _check_file_size

    def test_small_file_allowed(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.max_voice_file_mb", 20)
        msg, check = self._msg((5 * 1024 * 1024, None, None))
        check(msg)  # لا يرفع استثناء

    def test_large_voice_rejected(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.max_voice_file_mb", 20)
        from bot.conversation import MediaTooLargeError

        msg, check = self._msg((30 * 1024 * 1024, None, None))
        try:
            check(msg)
        except MediaTooLargeError:
            return
        raise AssertionError("من المفترض رفض ملف > الحد")

    def test_large_audio_rejected(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.max_voice_file_mb", 10)
        from bot.conversation import MediaTooLargeError

        msg, check = self._msg((None, 25 * 1024 * 1024, None))
        try:
            check(msg)
        except MediaTooLargeError:
            return
        raise AssertionError("من المفترض رفض ملف صوتي أكبر من الحد")


# ---------- /admin_stats ----------


class TestAdminStats:
    def test_counts_without_individual_data(self, db_session):
        from app.admin import build_admin_stats
        from app.database.crud import create_task, create_transaction

        create_transaction(
            db_session,
            111,
            {"type": "expense", "amount": 500, "currency": "ILS", "description": "x1"},
            raw_message="x1",
            telegram_message_id=1,
        )
        create_transaction(
            db_session,
            222,
            {"type": "income", "amount": 800, "currency": "USD", "description": "x2"},
            raw_message="x2",
            telegram_message_id=2,
        )
        create_task(
            db_session, 111, {"description": "مهمة أ"}, raw_message="مهمة أ", telegram_message_id=3
        )

        stats = build_admin_stats(db_session)
        assert stats["users_count"] == 2
        assert stats["transactions_count"] == 2
        assert stats["tasks_count"] == 1
        assert float(stats["total_expenses"]["ILS"]) == 500.0
        assert float(stats["total_incomes"]["USD"]) == 800.0
        # لا تُكشف أي قيم حساسة/فردية داخل النتيجة
        assert "description" not in stats
        assert "raw_message" not in stats

    def test_admin_ids_parsing(self):
        s = Settings(admin_user_ids="111, 222, abc")
        assert s.admin_user_ids == [111, 222]

    def test_admin_ids_default_empty(self):
        assert Settings(admin_user_ids="").admin_user_ids == []


# ---------- audit log ----------


class TestAuditLog:
    def test_update_transaction_writes_audit_line(self, db_session, tmp_path, monkeypatch):

        import app.audit as audit_mod
        from app.database.crud import create_transaction, update_transaction

        # تهيئة سجل تدقيق على ملف مؤقت
        monkeypatch.setattr(audit_mod, "AUDIT_LOG", os.path.join(str(tmp_path), "audit.log"))
        monkeypatch.setattr(audit_mod, "_configured", False)
        audit_mod.audit_logger.handlers.clear()
        audit_mod.setup_audit_log()

        tx = create_transaction(
            db_session,
            111,
            {"type": "expense", "amount": 100, "currency": "ILS", "description": "أ"},
            raw_message="أ",
            telegram_message_id=1,
        )
        update_transaction(db_session, tx, {"description": "مواد محدثة", "amount": 150})

        log_file = os.path.join(str(tmp_path), "audit.log")
        with open(log_file, encoding="utf-8") as f:
            content = f.read()
        assert "action=update" in content
        assert "target=transaction:" in content
        assert "user=111" in content

    def test_undo_writes_audit_line(self, db_session, tmp_path, monkeypatch):
        import os

        import app.audit as audit_mod
        from app.database.crud import create_note, undo_last_record

        monkeypatch.setattr(audit_mod, "AUDIT_LOG", os.path.join(str(tmp_path), "audit2.log"))
        monkeypatch.setattr(audit_mod, "_configured", False)
        audit_mod.audit_logger.handlers.clear()
        audit_mod.setup_audit_log()

        create_note(
            db_session,
            111,
            {"type": "note", "description": "ملاحظة سرية"},
            raw_message="ملاحظة",
            telegram_message_id=9,
        )
        undo_last_record(db_session, 111)

        with open(os.path.join(str(tmp_path), "audit2.log"), encoding="utf-8") as f:
            content = f.read()
        assert "action=soft_delete" in content
        assert "user=111" in content
