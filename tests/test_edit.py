"""
Unit tests لأمر /edit: قائمة السجلات الأخيرة وتحديث الحقول (بلا شبكة).
"""

from app.database.crud import (
    create_note,
    create_task,
    create_transaction,
    get_record_by_id,
    list_recent_records,
    update_note,
    update_task,
    update_transaction,
)
from app.database.models import Transaction

USER_A = 111
USER_B = 222


def _seed_all(db, user=USER_A):
    txn = create_transaction(
        db,
        user,
        {
            "type": "expense",
            "amount": 300,
            "currency": "شيكل",
            "person": "محمد",
            "description": "مواد بناء",
        },
        raw_message="دفعت 300 شيكل لمحمد",
    )
    task = create_task(
        db,
        user,
        {"description": "الاتصال بسامر", "person": "سامر", "date": "2026-09-10 10:00"},
        raw_message="ذكرني أتصل بسامر",
    )
    note = create_note(
        db,
        user,
        {"type": "order", "description": "مستلزمات مكتبية", "person": "شركة الأمل"},
        raw_message="طلبية مستلزمات مكتبية",
    )
    return txn, task, note


class TestListRecentRecords:
    def test_returns_empty_for_new_user(self, db_session):
        assert list_recent_records(db_session, USER_A) == []

    def test_sorted_newest_first(self, db_session):
        _seed_all(db_session)
        records = list_recent_records(db_session, USER_A, limit=10)
        assert len(records) == 3
        kinds = {r["kind"] for r in records}
        assert kinds == {"expense", "task", "order"}
        # أقدمها أولًا في الفرز العكسي (created_at desc)
        assert records[0]["created_at"] > records[-1]["created_at"]

    def test_limit_applies(self, db_session):
        for i in range(5):
            create_transaction(
                db_session,
                USER_A,
                {"type": "expense", "amount": i + 1, "currency": "ILS"},
                raw_message=f"دفعة {i}",
            )
        records = list_recent_records(db_session, USER_A, limit=5)
        assert len(records) == 5

    def test_other_users_records_not_included(self, db_session):
        _seed_all(db_session, user=USER_A)
        _seed_all(db_session, user=USER_B)
        records = list_recent_records(db_session, USER_A, limit=10)
        assert len(records) == 3


class TestGetRecordById:
    def test_returns_correct_record(self, db_session):
        _seed_all(db_session)
        row, model = get_record_by_id(db_session, USER_A, "Transaction", 1)
        assert row is not None
        assert model is Transaction

    def test_rejects_other_users(self, db_session):
        _seed_all(db_session, user=USER_A)
        row, _ = get_record_by_id(db_session, USER_B, "Transaction", 1)
        assert row is None

    def test_invalid_model_returns_none(self, db_session):
        row, model = get_record_by_id(db_session, USER_A, "Ghost", 1)
        assert row is None
        assert model is None


class TestUpdateTransaction:
    def test_updates_simple_fields(self, db_session):
        txn, *_ = _seed_all(db_session)
        updated = update_transaction(
            db_session, txn, {"amount": "350", "description": "مواد محدثة"}
        )
        assert float(updated.amount) == 350.0
        assert updated.description == "مواد محدثة"
        assert updated.updated_at is not None

    def test_updates_currency_normalization(self, db_session):
        txn, *_ = _seed_all(db_session)
        updated = update_transaction(db_session, txn, {"currency": "دولار"})
        assert updated.currency == "USD"

    def test_ignores_unknown_fields(self, db_session):
        txn, *_ = _seed_all(db_session)
        updated = update_transaction(db_session, txn, {"nonexistent_field": "x"})
        assert updated.description == "مواد بناء"


class TestUpdateTask:
    def test_updates_description_and_person(self, db_session):
        _, task, _ = _seed_all(db_session)
        updated = update_task(db_session, task, {"description": "الاتصال بخالد", "person": "خالد"})
        assert updated.description == "الاتصال بخالد"
        assert updated.person == "خالد"
        assert updated.updated_at is not None

    def test_updates_due_date(self, db_session):
        _, task, _ = _seed_all(db_session)
        updated = update_task(db_session, task, {"due_date": "2026-09-15 18:00"})
        assert updated.due_date is not None

    def test_empty_description_kept(self, db_session):
        _, task, _ = _seed_all(db_session)
        updated = update_task(db_session, task, {"description": "   "})
        assert updated.description == "الاتصال بسامر"


class TestUpdateNote:
    def test_updates_description(self, db_session):
        _, _, note = _seed_all(db_session)
        updated = update_note(db_session, note, {"description": "مستلزمات محدثة"})
        assert updated.description == "مستلزمات محدثة"
        assert updated.updated_at is not None

    def test_ignores_invalid_field(self, db_session):
        _, _, note = _seed_all(db_session)
        updated = update_note(db_session, note, {"nonexistent": "x"})
        assert updated.description == "مستلزمات مكتبية"
