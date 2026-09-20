"""
اختبار حارس المبلغ — تغطية الفجوة المذكورة في الملاحظة #1:
لم يكن هناك اختبار يتحقق من رفض create_transaction لمبلغ سالب أو صفري.
"""
from decimal import Decimal

from app.database.crud.common import _to_valid_amount
from app.validation import AMOUNT_MAX, clamp_amount, valid_amount


class TestAmountGuard:
    def test_valid_amount_rejects_negative_and_zero(self):
        assert valid_amount(-300) is False
        assert valid_amount(-0.01) is False
        assert valid_amount(0) is False
        assert valid_amount(0.0) is False
        assert clamp_amount(-300) is None
        assert clamp_amount(0) is None

    def test_to_valid_amount_rejects_negative_zero_and_overflow(self):
        assert _to_valid_amount(-500) is None
        assert _to_valid_amount(0) is None
        assert _to_valid_amount("0.00") is None
        assert _to_valid_amount(float(AMOUNT_MAX) * 2) is None
        assert _to_valid_amount(Decimal("-1")) is None
        # صالح
        assert _to_valid_amount(100) == Decimal("100.00")
        assert _to_valid_amount("0.01") == Decimal("0.01")

    def test_create_transaction_stores_none_for_negative(self, db_session):
        from app.database.crud import create_transaction

        tx = create_transaction(
            db_session,
            999,
            {"type": "expense", "amount": -500, "currency": "ILS", "description": "سالب"},
            raw_message="test -500",
            telegram_message_id=9001,
        )
        assert tx is not None
        assert tx.amount is None, "المبلغ السالب يجب أن يُخزن كـ None لا كـ -500"

        tx2 = create_transaction(
            db_session,
            999,
            {"type": "income", "amount": 0, "currency": "ILS"},
            raw_message="test 0",
            telegram_message_id=9002,
        )
        assert tx2.amount is None

        tx3 = create_transaction(
            db_session,
            999,
            {"type": "expense", "amount": float(AMOUNT_MAX) * 2, "currency": "ILS"},
            raw_message="huge",
            telegram_message_id=9003,
        )
        assert tx3.amount is None

    def test_update_transaction_rejects_negative(self, db_session):
        import pytest

        from app.database.crud import create_transaction, update_transaction

        tx = create_transaction(
            db_session,
            999,
            {"type": "expense", "amount": 100, "currency": "ILS"},
            raw_message="orig",
            telegram_message_id=9004,
        )
        assert tx.amount == Decimal("100.00")
        with pytest.raises(ValueError):
            update_transaction(db_session, tx, {"amount": -50})
        db_session.refresh(tx)
        assert tx.amount == Decimal("100.00")  # بقي كما هو ولم يُمسح

        # المبلغ الصفري مرفوض أيضًا
        tx2 = create_transaction(
            db_session,
            999,
            {"type": "expense", "amount": 200, "currency": "ILS"},
            raw_message="orig2",
            telegram_message_id=9005,
        )
        with pytest.raises(ValueError):
            update_transaction(db_session, tx2, {"amount": 0})
        db_session.refresh(tx2)
        assert tx2.amount == Decimal("200.00")

    def test_create_transaction_via_bonus_grant_negative(self, db_session):
        from app.database.crud import record_bonus_grant

        tx = record_bonus_grant(db_session, 999, -500, "ILS", direction="expense", person="موظف")
        # بعد الإصلاح: المبلغ السالب يُرفض قبل إنشاء المعاملة
        assert tx is None

    def test_create_transaction_via_bonus_grant_huge_overflow(self, db_session):
        from app.database.crud import record_bonus_grant
        from app.validation import AMOUNT_MAX

        tx = record_bonus_grant(db_session, 999, float(AMOUNT_MAX) * 2, "ILS", direction="expense", person="موظف")
        assert tx is None

    def test_create_transaction_via_bonus_grant_zero(self, db_session):
        from app.database.crud import record_bonus_grant

        tx = record_bonus_grant(db_session, 999, 0, "ILS", direction="expense", person="موظف")
        assert tx is None
