"""
اختبارات encrypt_backfill: يكتشف كل الأعمدة المشفّرة تلقائيًا (بما فيها
amount_in_base_currency) ويغطي كل النماذج (CorrectionFeedback, Invoice)
بدل قائمة Transaction/Task/Note القديمة التي كانت تنسى حقولًا ونماذج.

عبر إدراج صفوف "legacy واضحة" مباشرة بالـ SQL ثم إعادة كتابتها والتحقق أن
التخزين الخام أصبح "v1$..." مشفّرًا فعليًا.
"""

import base64
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database.models import (
    CorrectionFeedback,
    Invoice,
    Note,
    Task,
    Transaction,
)
from app.security import EncryptedNumeric, EncryptedString
from app.security import settings as security_settings
from encrypt_backfill import (
    CONFIG,
    _confirm_key,
    _encrypt_row,
    _encrypted_columns,
)

_VALID_KEY = base64.b64encode(b"0" * 32).decode("ascii")


@pytest.fixture
def enc_key(monkeypatch):
    monkeypatch.setattr(CONFIG, "encryption_key", _VALID_KEY)
    monkeypatch.setattr(security_settings, "encryption_key", _VALID_KEY)


class TestEncryptedColumnDiscovery:
    def test_transaction_discovers_all_encrypted_fields(self):
        assert set(_encrypted_columns(Transaction)) == {
            "description",
            "raw_message",
            "amount",
            "amount_in_base_currency",
        }

    def test_note_fields(self):
        assert set(_encrypted_columns(Note)) == {"description", "raw_message"}

    def test_task_fields(self):
        assert set(_encrypted_columns(Task)) == {"description", "raw_message"}

    def test_correction_feedback_fields(self):
        assert _encrypted_columns(CorrectionFeedback) == ["raw_message"]

    def test_invoice_fields(self):
        assert _encrypted_columns(Invoice) == ["description"]

    def test_every_discovered_column_is_encrypted_type(self):
        for model in (Transaction, Note, Task, CorrectionFeedback, Invoice):
            for name in _encrypted_columns(model):
                assert isinstance(
                    model.__table__.c[name].type, (EncryptedString, EncryptedNumeric)
                )


class TestConfirmKey:
    def test_requires_key(self, monkeypatch):
        monkeypatch.setattr(CONFIG, "encryption_key", "")
        with pytest.raises(SystemExit):
            _confirm_key()

    def test_rejects_invalid_key(self, monkeypatch):
        monkeypatch.setattr(CONFIG, "encryption_key", "abcd")
        with pytest.raises(SystemExit) as excinfo:
            _confirm_key()
        assert "غير صالح" in str(excinfo.value)

    def test_accepts_valid_key(self, monkeypatch):
        monkeypatch.setattr(CONFIG, "encryption_key", _VALID_KEY)
        _confirm_key()


class TestBackfillWritesEncrypted:
    def test_transaction_backfills_every_encrypted_field(self, db_session, enc_key):
        db_session.execute(
            text(
                "INSERT INTO transactions "
                "(telegram_user_id, type, amount, amount_in_base_currency, description, raw_message) "
                "VALUES (:u, 'expense', '50.00', '50.00', 'note_desc', 'raw_msg')"
            ),
            {"u": 111},
        )
        db_session.commit()

        obj = db_session.query(Transaction).filter_by(telegram_user_id=111).one()
        _encrypt_row(db_session, Transaction, obj)
        db_session.commit()

        row = db_session.execute(
            text(
                "SELECT amount, amount_in_base_currency, description, raw_message "
                "FROM transactions WHERE telegram_user_id = 111"
            )
        ).one()
        for value in row:
            assert str(value).startswith("v1$")

        back = db_session.query(Transaction).filter_by(telegram_user_id=111).one()
        assert back.amount == Decimal("50.00")
        assert back.amount_in_base_currency == Decimal("50.00")
        assert back.description == "note_desc"
        assert back.raw_message == "raw_msg"

    def test_remaining_models_backfilled(self, db_session, enc_key):
        db_session.execute(
            text(
                "INSERT INTO notes (telegram_user_id, note_type, description, raw_message) "
                "VALUES (111, 'note', 'ملاحظة', 'raw_note')"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO tasks (telegram_user_id, description, raw_message, priority, status, reminder_sent) "
                "VALUES (111, 'مهمة', 'raw_task', 'normal', 'pending', 0)"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO invoices (telegram_user_id, person, amount, currency, description, status, alerted) "
                "VALUES (111, 'مورّد', 200.00, 'ILS', 'فاتورة آجلة', 'pending', 0)"
            )
        )
        db_session.execute(
            text(
                "INSERT INTO correction_feedback (telegram_user_id, source, raw_message, reviewed) "
                "VALUES (111, 'cancel', 'نص خاطئ', 0)"
            )
        )
        db_session.commit()

        for model in (Note, Task, Invoice, CorrectionFeedback):
            for obj in db_session.query(model).all():
                _encrypt_row(db_session, model, obj)
        db_session.commit()

        note_raw = db_session.execute(
            text("SELECT description, raw_message FROM notes WHERE telegram_user_id = 111")
        ).one()
        assert str(note_raw[0]).startswith("v1$") and str(note_raw[1]).startswith("v1$")

        task_raw = db_session.execute(
            text("SELECT description, raw_message FROM tasks WHERE telegram_user_id = 111")
        ).one()
        assert str(task_raw[0]).startswith("v1$") and str(task_raw[1]).startswith("v1$")

        invoice_raw = db_session.execute(
            text("SELECT description, amount FROM invoices WHERE telegram_user_id = 111")
        ).one()
        assert str(invoice_raw[0]).startswith("v1$")
        assert invoice_raw[1] == 200.00  # الحقل الرقمي الواضح يبقى كما هو

        feedback_raw = db_session.execute(
            text("SELECT raw_message FROM correction_feedback WHERE telegram_user_id = 111")
        ).one()
        assert str(feedback_raw[0]).startswith("v1$")

    def test_null_values_left_untouched(self, db_session, enc_key):
        db_session.execute(
            text(
                "INSERT INTO transactions (telegram_user_id, type, amount, description) "
                "VALUES (111, 'income', '10.00', 'بلا رسالة خام')"
            )
        )
        db_session.commit()

        obj = db_session.query(Transaction).filter_by(telegram_user_id=111).one()
        _encrypt_row(db_session, Transaction, obj)
        db_session.commit()

        row = db_session.execute(
            text(
                "SELECT amount, amount_in_base_currency, description, raw_message "
                "FROM transactions WHERE telegram_user_id = 111"
            )
        ).one()
        assert str(row[0]).startswith("v1$")
        assert row[1] is None  # لا نكتب NULL/لا نخترع قيمة للمفقود
        assert str(row[2]).startswith("v1$")
        assert row[3] is None
