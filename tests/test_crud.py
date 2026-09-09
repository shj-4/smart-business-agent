"""
Unit tests لدوال app.database.crud — بلا أي اتصال شبكة.

تعتمد على fixture `db_session` من tests/conftest.py (SQLite في الذاكرة).
"""

from datetime import datetime, timedelta
from decimal import Decimal

from app.database.crud import (
    complete_task,
    create_task,
    create_transaction,
    list_pending_tasks,
    mark_overdue_tasks,
    run_query,
    undo_last_record,
)
from app.database.models import Note, Task, Transaction
from app.timeutil import now_utc, to_local_naive

USER_A = 111
USER_B = 222


# ---------- create_transaction ----------


class TestCreateTransaction:
    def test_creates_income_transaction(self, db_session):
        obj = create_transaction(
            db_session,
            USER_A,
            {
                "type": "income",
                "amount": 500,
                "currency": "دولار",
                "person": "أحمد",
                "description": "دفعة من أحمد",
            },
            raw_message="استلمت 500 دولار من أحمد",
            telegram_message_id=1,
        )
        assert obj is not None
        assert obj.type == "income"
        assert float(obj.amount) == 500.0
        assert obj.currency == "USD"
        assert obj.person == "أحمد"
        assert obj.description == "دفعة من أحمد"
        assert obj.deleted_at is None

    def test_currency_normalized_to_ils(self, db_session):
        obj = create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 300, "currency": "شيكل"},
            raw_message="دفعت 300 شيكل",
            telegram_message_id=2,
        )
        assert obj.currency == "ILS"

    def test_duplicate_message_returns_none(self, db_session):
        data = {"type": "expense", "amount": 100}
        first = create_transaction(db_session, USER_A, data, "raw", telegram_message_id=50)
        assert first is not None
        dup = create_transaction(db_session, USER_A, data, "raw", telegram_message_id=50)
        assert dup is None

    def test_invalid_amount_becomes_none(self, db_session):
        obj = create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": "ليس رقمًا"},
            raw_message="دفعت شيئًا",
            telegram_message_id=3,
        )
        assert obj is not None
        assert obj.amount is None

    def test_string_amount_converted_to_decimal(self, db_session):
        obj = create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": "12.5"},
            raw_message="دفعت 12.5",
            telegram_message_id=4,
        )
        assert obj.amount == Decimal("12.50")


# ---------- create_task ----------


class TestCreateTask:
    def test_creates_pending_task(self, db_session):
        task = create_task(
            db_session,
            USER_A,
            {"description": "الاتصال بسامر", "person": "سامر"},
            raw_message="ذكرني أتصل بسامر",
            telegram_message_id=10,
        )
        assert task is not None
        assert task.description == "الاتصال بسامر"
        assert task.person == "سامر"
        assert task.status == "pending"
        assert task.due_date is None

    def test_falls_back_to_raw_message(self, db_session):
        task = create_task(
            db_session,
            USER_A,
            {},
            raw_message="اتصل بالمورد محمد",
            telegram_message_id=11,
        )
        assert task.description == "اتصل بالمورد محمد"

    def test_parses_due_date(self, db_session):
        task = create_task(
            db_session,
            USER_A,
            {"description": "اجتماع", "date": "2026-09-10 10:00"},
            raw_message="اجتماع 2026-09-10 10:00",
            telegram_message_id=12,
        )
        assert task.due_date is not None
        assert task.due_date.year == 2026
        assert task.due_date.month == 9
        assert task.due_date.day == 10

    def test_duplicate_message_returns_none(self, db_session):
        data = {"description": "مهمة مكررة"}
        assert create_task(db_session, USER_A, data, "raw", 20) is not None
        assert create_task(db_session, USER_A, data, "raw", 20) is None


# ---------- mark_overdue_tasks ----------


def _add_task(db, user_id, *, due, description="مهمة", status="pending", msg_id=None):
    task = Task(
        telegram_user_id=user_id,
        telegram_message_id=msg_id,
        description=description,
        due_date=due,
        status=status,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


class TestMarkOverdueTasks:
    def test_past_due_task_becomes_overdue(self, db_session):
        _add_task(db_session, USER_A, due=now_utc() - timedelta(days=1), msg_id=1)
        updated = mark_overdue_tasks(db_session, USER_A)
        assert updated == 1
        statuses = [t.status for t in db_session.query(Task).all()]
        assert statuses == ["overdue"]

    def test_future_due_task_stays_pending(self, db_session):
        _add_task(db_session, USER_A, due=now_utc() + timedelta(days=1), msg_id=2)
        assert mark_overdue_tasks(db_session, USER_A) == 0
        assert db_session.query(Task).one().status == "pending"

    def test_done_task_untouched(self, db_session):
        _add_task(db_session, USER_A, due=now_utc() - timedelta(days=1), status="done", msg_id=3)
        assert mark_overdue_tasks(db_session, USER_A) == 0
        assert db_session.query(Task).one().status == "done"

    def test_other_user_untouched(self, db_session):
        _add_task(db_session, USER_A, due=now_utc() - timedelta(days=1), msg_id=4)
        assert mark_overdue_tasks(db_session, USER_B) == 0
        assert db_session.query(Task).one().status == "pending"


# ---------- run_query ----------


def _add_transaction(
    db,
    user_id,
    *,
    type,
    amount,
    currency=None,
    person=None,
    description=None,
    msg_id=None,
    created_at=None,
):
    tx = Transaction(
        telegram_user_id=user_id,
        telegram_message_id=msg_id,
        type=type,
        amount=Decimal(str(amount)),
        currency=currency,
        person=person,
        description=description,
        raw_message="raw",
        created_at=created_at or now_utc(),
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


class TestRunQuery:
    def test_total_expenses_grouped_by_currency(self, db_session):
        _add_transaction(db_session, USER_A, type="expense", amount=300, currency="ILS", msg_id=1)
        _add_transaction(db_session, USER_A, type="expense", amount=50, currency="USD", msg_id=2)
        _add_transaction(db_session, USER_A, type="income", amount=1000, currency="ILS", msg_id=3)

        result = run_query(
            db_session,
            USER_A,
            {"metric": "total_expenses", "period": "all_time", "person": None},
        )
        assert result["metric"] == "total_expenses"
        total = {k: float(v) for k, v in result["result"].items()}
        assert total == {"ILS": 300.0, "USD": 50.0}

    def test_total_income(self, db_session):
        _add_transaction(db_session, USER_A, type="income", amount=900, currency="ILS", msg_id=1)
        _add_transaction(db_session, USER_A, type="expense", amount=900, currency="ILS", msg_id=2)

        result = run_query(
            db_session,
            USER_A,
            {"metric": "total_income", "period": "all_time", "person": None},
        )
        assert {k: float(v) for k, v in result["result"].items()} == {"ILS": 900.0}

    def test_total_respects_person_filter(self, db_session):
        _add_transaction(
            db_session, USER_A, type="expense", amount=100, currency="ILS", person="محمد", msg_id=1
        )
        _add_transaction(
            db_session, USER_A, type="expense", amount=200, currency="ILS", person="أحمد", msg_id=2
        )

        result = run_query(
            db_session,
            USER_A,
            {"metric": "total_expenses", "period": "all_time", "person": "محمد"},
        )
        assert {k: float(v) for k, v in result["result"].items()} == {"ILS": 100.0}

    def test_count_transactions(self, db_session):
        _add_transaction(db_session, USER_A, type="expense", amount=100, msg_id=1)
        _add_transaction(db_session, USER_A, type="income", amount=200, msg_id=2)
        _add_transaction(db_session, USER_B, type="expense", amount=300, msg_id=3)

        result = run_query(
            db_session,
            USER_A,
            {"metric": "count_transactions", "period": "all_time", "person": None},
        )
        assert result["result"] == 2

    def test_list_tasks_excludes_overdue_and_done(self, db_session):
        _add_task(db_session, USER_A, due=None, description="معلقة", msg_id=1)
        _add_task(
            db_session, USER_A, due=now_utc() - timedelta(days=1), description="متأخرة", msg_id=2
        )
        _add_task(db_session, USER_A, due=None, description="منجزة", status="done", msg_id=3)

        result = run_query(
            db_session,
            USER_A,
            {"metric": "list_tasks", "period": "all_time", "person": None},
        )
        descriptions = [row["description"] for row in result["result"]]
        assert descriptions == ["معلقة"]

    def test_list_overdue_tasks_marks_then_returns(self, db_session):
        _add_task(
            db_session,
            USER_A,
            due=now_utc() - timedelta(days=2),
            description="فات موعدها",
            msg_id=1,
        )
        _add_task(db_session, USER_A, due=None, description="لا موعد", msg_id=2)

        result = run_query(
            db_session,
            USER_A,
            {"metric": "list_overdue_tasks", "period": "all_time", "person": None},
        )
        descriptions = [row["description"] for row in result["result"]]
        assert descriptions == ["فات موعدها"]

    def test_unsupported_metric(self, db_session):
        result = run_query(
            db_session,
            USER_A,
            {"metric": "nonsense", "period": "all_time", "person": None},
        )
        assert result["error"] == "unsupported_metric"


# ---------- undo_last_record ----------


class TestUndoLastRecord:
    def test_none_when_no_records(self, db_session):
        assert undo_last_record(db_session, USER_A) is None

    def test_undo_latest_note_then_transaction(self, db_session):
        tx = Transaction(
            telegram_user_id=USER_A,
            telegram_message_id=1,
            type="expense",
            amount=Decimal("100"),
            description="دفعة",
            created_at=datetime(2026, 1, 1),
        )
        note = Note(
            telegram_user_id=USER_A,
            telegram_message_id=2,
            note_type="note",
            description="ملاحظة لاحقة",
            created_at=datetime(2026, 1, 2),
        )
        db_session.add_all([tx, note])
        db_session.commit()

        first = undo_last_record(db_session, USER_A)
        assert first["kind"] == "طلبية/ملاحظة"
        assert first["label"] == "ملاحظة لاحقة"
        # الملاحظة اختفت من استعلامات live لكن بقت في الجدول (soft delete)
        assert db_session.query(Note).filter(Note.id == note.id).one().deleted_at is not None
        assert (
            db_session.query(Transaction).filter(Transaction.id == tx.id).one().deleted_at is None
        )

        second = undo_last_record(db_session, USER_A)
        assert second["kind"] == "معاملة"
        assert (
            db_session.query(Transaction).filter(Transaction.id == tx.id).one().deleted_at
            is not None
        )

        assert undo_last_record(db_session, USER_A) is None

    def test_only_own_records_are_undoable(self, db_session):
        tx = Transaction(
            telegram_user_id=USER_B,
            telegram_message_id=1,
            type="expense",
            amount=Decimal("100"),
            description="دفعة لشخص آخر",
            created_at=datetime(2026, 1, 1),
        )
        db_session.add(tx)
        db_session.commit()

        assert undo_last_record(db_session, USER_A) is None


# ---------- أولويات وتكرار المهام ----------


class TestTaskPriorityRecurrence:
    def test_create_task_normalizes_arabic_values(self, db_session):
        task = create_task(
            db_session,
            USER_A,
            {"description": "الاتصال بالمورد", "priority": "عاجل", "recurrence": "كل أسبوع"},
            raw_message="ذكرني كل أسبوع أتصل بالمورد",
        )
        assert task.priority == "high"
        assert task.recurrence_rule == "weekly"

    def test_create_task_defaults(self, db_session):
        task = create_task(db_session, USER_A, {"description": "مهمة عادية"}, raw_message="مهمة")
        assert task.priority == "normal"
        assert task.recurrence_rule is None

    def test_list_pending_sorts_high_first(self, db_session):
        n = create_task(db_session, USER_A, {"description": "عادية"}, raw_message="مهمة عادية")
        h = create_task(
            db_session, USER_A, {"description": "عاجلة", "priority": "high"}, raw_message="عاجلة"
        )
        low = create_task(
            db_session, USER_A, {"description": "خفيفة", "priority": "low"}, raw_message="خفيفة"
        )
        ids = [t.id for t in list_pending_tasks(db_session, USER_A)]
        assert ids == [h.id, n.id, low.id]

    def test_complete_task_respawns_recurring(self, db_session):
        task = create_task(
            db_session,
            USER_A,
            {
                "description": "الاتصال بالمورد",
                "recurrence": "weekly",
                "date": "2026-09-01 10:00",
            },
            raw_message="كل أسبوع اتصل بالمورد",
        )
        done = complete_task(db_session, USER_A, task.id)
        assert done.status == "done"
        regenerated = (
            db_session.query(Task)
            .filter(
                Task.status == "pending",
                Task.deleted_at.is_(None),
                Task.recurrence_rule == "weekly",
            )
            .all()
        )
        assert len(regenerated) == 1
        spawned = regenerated[0]
        assert spawned.id != task.id
        assert (spawned.due_date - task.due_date).days == 7
        assert spawned.priority == task.priority
        assert complete_task(db_session, USER_A, spawned.id) is not None

    def test_complete_non_recurring_no_respawn(self, db_session):
        task = create_task(
            db_session,
            USER_A,
            {"description": "مرة واحدة", "date": "2026-09-01 10:00"},
            raw_message="مرة واحدة",
        )
        complete_task(db_session, USER_A, task.id)
        assert (
            db_session.query(Task)
            .filter(Task.status == "pending", Task.deleted_at.is_(None))
            .count()
            == 0
        )

    def test_complete_monthly_respawn_clamps_to_shorter_month(self, db_session):
        """مهمة شهرية في 31 يناير → فبراير أقصر: يجب تقييد اليوم لآخر يوم صالح
        (28 في 2026) بدل طرح ValueError بعد إثبات الإنجاز."""
        task = create_task(
            db_session,
            USER_A,
            {"description": "فاتورة شهرية", "recurrence": "monthly", "date": "2026-01-31 10:00"},
            raw_message="فاتورة كل شهر 31",
        )
        done = complete_task(db_session, USER_A, task.id)
        assert done.status == "done"
        spawned = (
            db_session.query(Task)
            .filter(Task.status == "pending", Task.deleted_at.is_(None))
            .one()
        )
        local_due = to_local_naive(spawned.due_date)
        assert local_due.year == 2026 and local_due.month == 2 and local_due.day == 28

    def test_complete_monthly_respawn_across_dec_jan(self, db_session):
        task = create_task(
            db_session,
            USER_A,
            {"description": "اشتراك سنوي", "recurrence": "monthly", "date": "2026-12-31 09:00"},
            raw_message="اشتراك كل شهر آخر يوم",
        )
        done = complete_task(db_session, USER_A, task.id)
        assert done.status == "done"
        spawned = (
            db_session.query(Task)
            .filter(Task.status == "pending", Task.deleted_at.is_(None))
            .one()
        )
        local_due = to_local_naive(spawned.due_date)
        assert local_due.year == 2027 and local_due.month == 1 and local_due.day == 31

    def test_complete_monthly_respawn_leap_year(self, db_session):
        task = create_task(
            db_session,
            USER_A,
            {"description": "مراجعة شهرية", "recurrence": "monthly", "date": "2024-01-31 08:00"},
            raw_message="كل شهر مراجعة",
        )
        complete_task(db_session, USER_A, task.id)
        spawned = (
            db_session.query(Task)
            .filter(Task.status == "pending", Task.deleted_at.is_(None))
            .one()
        )
        local_due = to_local_naive(spawned.due_date)
        assert local_due.year == 2024 and local_due.month == 2 and local_due.day == 29


# ---------- صلاحيات المساحة المشتركة (مرتكز فقط يحذف/يعدّل) ----------


class TestWorkspaceRoleGates:
    def test_can_manage_records_individual_and_owner(self, db_session):
        from app.database.crud import can_manage_records, create_workspace, invite_to_workspace

        assert can_manage_records(db_session, USER_A) is True
        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        assert can_manage_records(db_session, USER_A) is True
        assert can_manage_records(db_session, USER_B) is False

    def test_member_undo_denied_owner_allowed(self, db_session):
        from app.database.crud import (
            create_workspace,
            invite_to_workspace,
            leave_workspace,
        )

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        create_transaction(
            db_session,
            USER_B,
            {"amount": 50, "type": "expense", "description": "دفعة شريك"},
            raw_message="50 شيكل",
        )
        # العضو داخل المساحة لا يستطيع التراجع حتى عن سجله
        assert undo_last_record(db_session, USER_B) is None
        # بعد الخروج تعود له الصلاحية كفرد
        leave_workspace(db_session, USER_B)
        assert undo_last_record(db_session, USER_B) is not None

    def test_member_task_delete_denied(self, db_session):
        from app.database.crud import (
            create_workspace,
            delete_task_by_id,
            invite_to_workspace,
            list_pending_tasks,
        )

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        task = create_task(db_session, USER_B, {"description": "مهمة الشريك"}, raw_message="مهمة")
        assert delete_task_by_id(db_session, USER_B, task.id) is None
        assert list_pending_tasks(db_session, USER_B)[0].id == task.id
        assert delete_task_by_id(db_session, USER_A, task.id) is not None
