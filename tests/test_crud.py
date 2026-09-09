"""
Unit tests لدوال app.database.crud — بلا أي اتصال شبكة.

تعتمد على fixture `db_session` من tests/conftest.py (SQLite في الذاكرة).
"""

from datetime import datetime, timedelta
from decimal import Decimal

from app.database.crud import (
    complete_task,
    create_invoice,
    create_note,
    create_task,
    create_transaction,
    credit_usage,
    list_credit_limits,
    list_invoices,
    list_orders,
    list_pending_tasks,
    mark_invoice_paid,
    mark_overdue_invoices,
    mark_overdue_tasks,
    monthly_totals,
    person_debts,
    restore_last_deleted,
    run_query,
    search_records,
    set_credit_limit,
    set_order_status,
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


# ---------- restore_last_deleted (إعادة/redo) ----------


class TestRestoreLastDeleted:
    def test_none_when_nothing_deleted(self, db_session):
        assert restore_last_deleted(db_session, USER_A) is None

    def test_restores_most_recently_deleted(self, db_session):
        tx = Transaction(
            telegram_user_id=USER_A,
            telegram_message_id=1,
            type="expense",
            amount=Decimal("100"),
            description="دفعة محذوفة",
            created_at=datetime(2026, 1, 1),
            deleted_at=datetime(2026, 1, 3),
        )
        note = Note(
            telegram_user_id=USER_A,
            telegram_message_id=2,
            note_type="note",
            description="ملاحظة محذوفة لاحقًا",
            created_at=datetime(2026, 1, 2),
            deleted_at=datetime(2026, 1, 5),
        )
        db_session.add_all([tx, note])
        db_session.commit()

        restored = restore_last_deleted(db_session, USER_A)
        assert restored["kind"] == "طلبية/ملاحظة"
        assert restored["label"] == "ملاحظة محذوفة لاحقًا"
        assert db_session.query(Note).filter(Note.id == note.id).one().deleted_at is None
        assert (
            db_session.query(Transaction).filter(Transaction.id == tx.id).one().deleted_at
            is not None
        )

        second = restore_last_deleted(db_session, USER_A)
        assert second["kind"] == "معاملة"
        assert db_session.query(Transaction).filter(Transaction.id == tx.id).one().deleted_at is None
        assert restore_last_deleted(db_session, USER_A) is None

    def test_only_scoped_records_restorable(self, db_session):
        tx = Transaction(
            telegram_user_id=USER_B,
            telegram_message_id=1,
            type="expense",
            amount=Decimal("5"),
            description="خاص بآخر",
            created_at=datetime(2026, 1, 1),
            deleted_at=datetime(2026, 1, 4),
        )
        db_session.add(tx)
        db_session.commit()
        assert restore_last_deleted(db_session, USER_A) is None


# ---------- search_records: نافذة البحث والنتائج الجزئية ----------


class TestSearchRecordsWindow:
    def test_matches_and_meta_not_saturated(self, db_session):
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 300, "currency": "ILS", "description": "مواد حديد"},
            raw_message="دفعة مواد حديد",
        )
        hits, meta = search_records(db_session, USER_A, "حديد", return_meta=True)
        assert len(hits) == 1
        assert meta["saturated"] is False

    def test_partial_hint_flag_when_window_full(self, db_session):
        # أكثر من 100 سجل تملأ نافذة البحث → saturated ليُعلَن أن النتائج جزئية
        for i in range(105):
            create_transaction(
                db_session,
                USER_A,
                {
                    "type": "expense",
                    "amount": 1,
                    "currency": "ILS",
                    "description": f"دفعة عامة {i}",
                },
                raw_message=f"دفعة {i}",
            )
        # أقدم سجل (0) خارج نافذة الـ100 الأحدث: لن يظهر، لكن النافذة ممتلئة
        hits, meta = search_records(db_session, USER_A, "دفعة عامة 0", return_meta=True)
        assert meta["saturated"] is True
        assert not hits
        # سجل داخل النافذة يظهر طبيعيًا
        hits2, _ = search_records(db_session, USER_A, "دفعة عامة 6", return_meta=True)
        assert len(hits2) >= 1

    def test_plain_calls_return_list_for_backward_compat(self, db_session):
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 50, "currency": "ILS", "description": "كهرباء"},
            raw_message="فاتورة كهرباء",
        )
        result = search_records(db_session, USER_A, "كهرباء")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["model"] == "Transaction"


# ---------- الفواتير الآجلة (#22) ----------


class TestInvoiceCrud:
    def _invoice(self, db_session, **over):
        data = {
            "person": "مورّد الأثاث",
            "amount": 1200,
            "currency": "ILS",
            "description": "تجهيزات مكتب",
        }
        data.update(over)
        return create_invoice(db_session, USER_A, data, raw_message="فاتورة تجهيزات")

    def test_create_and_list(self, db_session):
        inv = self._invoice(db_session)
        assert inv is not None
        assert inv.status == "pending"
        lst = list_invoices(db_session, USER_A)
        assert len(lst) == 1
        assert lst[0].id == inv.id

    def test_create_rejects_non_positive_amount(self, db_session):
        assert self._invoice(db_session, amount=0) is None
        assert self._invoice(db_session, amount=-5) is None

    def test_mark_paid(self, db_session):
        inv = self._invoice(db_session)
        assert mark_invoice_paid(db_session, USER_A, inv.id) is True
        assert list_invoices(db_session, USER_A)[0].status == "paid"
        # الدفع الثاني مرفوض
        assert mark_invoice_paid(db_session, USER_A, inv.id) is False

    def test_overdue_marking(self, db_session):
        inv = self._invoice(db_session)
        inv.due_date = now_utc() - timedelta(days=2)
        db_session.commit()
        overdue = mark_overdue_invoices(db_session)
        assert [o.id for o in overdue] == [inv.id]
        assert list_invoices(db_session, USER_A, status="overdue")[0].id == inv.id

    def test_scoped_by_user(self, db_session):
        inv = self._invoice(db_session)
        assert mark_invoice_paid(db_session, USER_B, inv.id) is False
        assert list_invoices(db_session, USER_B) == []


# ---------- دورة حياة الطلبيات (#38) ----------


class TestOrderStatus:
    def test_create_order_starts_open(self, db_session):
        note = create_note(
            db_session,
            USER_A,
            {"type": "order", "description": "5 صناديق من المورّد"},
            raw_message="طلبية 5 صناديق",
        )
        assert note.note_type == "order"
        assert note.status == "open"

    def test_plain_note_has_no_status(self, db_session):
        note = create_note(
            db_session,
            USER_A,
            {"type": "note", "description": "اجتماع يوم السبت"},
            raw_message="ملاحظة اجتماع",
        )
        assert note.status is None

    def test_mark_done_and_list(self, db_session):
        note = create_note(
            db_session, USER_A, {"type": "order", "description": "مواد"}, "طلبية مواد"
        )
        assert set_order_status(db_session, USER_A, note.id, "done") is True
        done = list_orders(db_session, USER_A, status="done")
        assert [o.id for o in done] == [note.id]
        assert list_orders(db_session, USER_A, status="open") == []

    def test_rejects_invalid_status_and_foreign_note(self, db_session):
        note = create_note(
            db_session, USER_A, {"type": "order", "description": "مواد"}, "طلبية مواد"
        )
        assert set_order_status(db_session, USER_A, note.id, "bad") is False
        assert set_order_status(db_session, USER_B, note.id, "done") is False
        assert note.status == "open"


# ---------- حقول الضريبة (VAT) (#24) ----------


class TestVatFields:
    def test_create_transaction_stores_vat(self, db_session):
        tx = create_transaction(
            db_session,
            USER_A,
            {
                "type": "expense",
                "amount": 117,
                "currency": "ILS",
                "vat_rate": 17,
                "vat_amount": 17,
                "description": "فاتورة كهرباء ضريبية",
            },
            raw_message="فاتورة",
        )
        assert tx.vat_rate == Decimal("17.000")
        assert tx.vat_amount == Decimal("17.00")

    def test_invalid_vat_rate_rejected(self, db_session):
        tx = create_transaction(
            db_session,
            USER_A,
            {
                "type": "expense",
                "amount": 100,
                "currency": "ILS",
                "vat_rate": 5000,
                "vat_amount": -5,
                "description": "اختبار",
            },
            raw_message="اختبار",
        )
        assert tx.vat_rate is None
        assert tx.vat_amount is None

    def test_vat_fields_in_recent_records(self, db_session):
        tx = create_transaction(
            db_session,
            USER_A,
            {
                "type": "expense",
                "amount": 117,
                "currency": "ILS",
                "vat_rate": 17,
                "vat_amount": 17,
                "description": "فاتورة ضريبية",
            },
            raw_message="فاتورة",
        )
        from app.database.crud import list_recent_records

        entry = next(e for e in list_recent_records(db_session, USER_A) if e["id"] == tx.id)
        assert entry["vat_rate"] == Decimal("17.000")
        assert entry["vat_amount"] == Decimal("17.00")


# ---------- الحدود الائتمانية (#26) ----------


class TestCreditLimits:
    def test_set_and_list(self, db_session):
        row = set_credit_limit(db_session, USER_A, "محمد ", "5000")
        assert row is not None
        assert row.person == "محمد"
        assert row.limit_amount == Decimal("5000")
        assert [lim.person for lim in list_credit_limits(db_session, USER_A)] == ["محمد"]

    def test_invalid_limit_rejected(self, db_session):
        assert set_credit_limit(db_session, USER_A, "محمد", "0") is None
        assert set_credit_limit(db_session, USER_A, "محمد", "-5") is None
        assert set_credit_limit(db_session, USER_A, "   ", "500") is None

    def test_usage_outstanding_is_expense_minus_income(self, db_session):
        set_credit_limit(db_session, USER_A, "محمد", "5000")
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 3000, "currency": "ILS", "person": "محمد"}, "دفعة لمحمد"
        )
        create_transaction(
            db_session, USER_A, {"type": "income", "amount": 1000, "currency": "ILS", "person": "محمد"}, "استلام من محمد"
        )
        row = list_credit_limits(db_session, USER_A)[0]
        usage = credit_usage(db_session, row)
        assert usage["outstanding"] == Decimal("2000.00")
        assert usage["percent"] == 40.0
        assert usage["over"] is False

    def test_usage_over_when_exceeds(self, db_session):
        set_credit_limit(db_session, USER_A, "محمد", "1500")
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 2000, "currency": "ILS", "person": "محمد"}, "دفعة"
        )
        row = list_credit_limits(db_session, USER_A)[0]
        assert credit_usage(db_session, row)["over"] is True


# ---------- الديون والأشخاص (#21) ----------


class TestPersonDebts:
    def test_balances_per_person(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "app.exchange.convert",
            lambda amount, frm, to: {"result": Decimal(str(amount)) * Decimal("1")},
        )
        create_transaction(
            db_session, USER_A, {"type": "income", "amount": 500, "currency": "ILS", "person": "سامر"}, "استلام من سامر"
        )
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 300, "currency": "ILS", "person": "سامر"}, "دفعة لسامر"
        )
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 200, "currency": "ILS", "person": "خالد"}, "دفعة لخالد"
        )
        debts = person_debts(db_session, USER_A)
        by_name = {d["person"]: d for d in debts}
        assert by_name["سامر"]["balance_unified"] == Decimal("200.00")  # مدين لك
        assert by_name["خالد"]["balance_unified"] == Decimal("-200.00")  # تدين له
        assert by_name["سامر"]["by_currency"]["ILS"]["income"] == Decimal("500.00")

    def test_empty_when_no_person_rows(self, db_session):
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 100, "currency": "ILS"}, "بدون شخص"
        )
        assert person_debts(db_session, USER_A) == []

    def test_scoped_to_workspace(self, db_session, monkeypatch):
        monkeypatch.setattr("app.exchange.convert", lambda a, f, t: {"result": Decimal("5")})
        create_transaction(
            db_session, USER_B, {"type": "expense", "amount": 100, "currency": "ILS", "person": "خالد"}, "دفعة لخالد"
        )
        assert person_debts(db_session, USER_A) == []


# ---------- الميزانية بنطاق التصنيف (#23) ----------


class TestBudgetCategoryScope:
    def test_create_and_usage(self, db_session):
        from app.database.crud import budget_usage, create_budget

        budget = create_budget(db_session, USER_A, "category", "مشتريات", "2000")
        assert budget is not None
        assert budget.category == "مشتريات"

        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 800, "currency": "ILS", "category": "مشتريات"},
            "شراء مواد",
        )
        usage = budget_usage(db_session, budget)
        assert usage["spent"] == Decimal("800.00")
        assert usage["percent"] == 40.0

    def test_duplicate_category_budget_rejected(self, db_session):
        from app.database.crud import create_budget

        assert create_budget(db_session, USER_A, "category", "مشتريات", "2000") is not None
        assert create_budget(db_session, USER_A, "category", "مشتريات", "3000") is None

    def test_empty_category_rejected(self, db_session):
        from app.database.crud import create_budget

        assert create_budget(db_session, USER_A, "category", "   ", "2000") is None


# ---------- سعر الصرف المثبَّت وقت التسجيل (#25) ----------


class TestStoredBaseAmount:
    def test_create_transaction_stores_fixed_base_amount(self, db_session, monkeypatch):
        """عملية بعملة غير الأساس تُحفَظ بمقدارها بعملة الأساس وقت التسجيل."""
        monkeypatch.setattr(
            "app.exchange.convert",
            lambda amount, frm, to: {"result": Decimal("750.00")},
        )
        tx = create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "USD", "description": "مواد"},
            raw_message="دفعة",
        )
        assert tx.amount_in_base_currency == Decimal("750.00")
        assert tx.base_currency_at_creation == "ILS"

    def test_same_currency_not_stored(self, db_session, monkeypatch):
        """عملية بعملة الأساس نفسها لا تُحوَّل (لا داعي) وتُترك فارغة."""
        monkeypatch.setattr("app.exchange.convert", lambda *a, **k: {"result": Decimal("999")})
        tx = create_transaction(
            db_session,
            USER_A,
            {"type": "income", "amount": 100, "currency": "ILS", "description": "قبض"},
            raw_message="استلام",
        )
        assert tx.amount_in_base_currency is None
        assert tx.base_currency_at_creation is None

    def test_run_query_unified_total_uses_stored(self, db_session, monkeypatch):
        """unified_total في run_query يجمع المبالغ المخزّنة ولا يلمس الشبكة."""
        monkeypatch.setattr(
            "app.exchange.convert",
            lambda amount, frm, to: {"result": Decimal(str(amount)) * Decimal("7.50")},
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "USD", "description": "مشتريات"},
            raw_message="د1",
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 50, "currency": "USD", "description": "نقل"},
            raw_message="د2",
        )
        res = run_query(db_session, USER_A, {"metric": "total_expenses", "period": "all_time"})
        assert res["unified_total"]["total"] == Decimal("1125.00")
        assert res["unified_total"]["from_stored"] is True
        assert res["result"] == {"USD": Decimal("150.00")}

    def test_monthly_totals_include_stored(self, db_session, monkeypatch):
        """monthly_totals(include_stored=True) يحمل المبالغ المثبّتة للرسم الدقيق."""
        monkeypatch.setattr(
            "app.exchange.convert",
            lambda amount, frm, to: {"result": Decimal(str(amount)) * Decimal("3.70")},
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "income", "amount": 200, "currency": "USD", "description": "بيع"},
            raw_message="ر1",
        )
        months = monthly_totals(db_session, USER_A, months=1, include_stored=True)
        entry = months[-1]
        assert entry["stored_base"] == "ILS"
        assert entry["stored"]["USD"]["income"] == Decimal("740.00")


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
