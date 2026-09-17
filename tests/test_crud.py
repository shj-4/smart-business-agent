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
    credit_monthly_reset,
    credit_usage,
    list_credit_limits,
    list_invoices,
    list_orders,
    list_overdue_tasks,
    list_pending_tasks,
    mark_invoice_paid,
    mark_overdue_invoices,
    mark_overdue_tasks,
    merge_person,
    monthly_totals,
    person_debts,
    restore_last_deleted,
    run_query,
    search_records,
    set_credit_limit,
    set_order_status,
    undo_last_record,
)
from app.database.models import Budget, CreditLimit, Invoice, Note, Task, Transaction
from app.timeutil import now_utc, to_local_naive, to_utc_naive

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

    def test_matches_person_via_sql_pushdown(self, db_session):
        # person نص عادي: المطابقة تُنجز في SQL (LIKE) دون لمس الوصف المشفّر
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "ILS", "person": "مورد الألمنيوم"},
            raw_message="دفعة عادية",
        )
        hits, _ = search_records(db_session, USER_A, "ألمنيوم", return_meta=True)
        assert len(hits) == 1
        assert hits[0]["person"] == "مورد الألمنيوم"

    def test_matches_category_via_sql_pushdown(self, db_session):
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 50, "currency": "ILS", "category": "مشتريات"},
            raw_message="دفعة",
        )
        hits, _ = search_records(db_session, USER_A, "مشتر", return_meta=True)
        assert len(hits) == 1
        assert hits[0]["model"] == "Transaction"

    def test_type_label_matches_in_sql(self, db_session):
        # البحث عن تسمية النوع ("مصروف") يعتمد على عمود type الصريح في SQL
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 90, "currency": "ILS", "description": "وقود"},
            raw_message="وقود للمركبة",
        )
        hits, _ = search_records(db_session, USER_A, "مصروف", return_meta=True)
        assert len(hits) == 1
        assert hits[0]["kind"] == "expense"

    def test_person_hits_reach_limit_flag_partial(self, db_session):
        # بلوغ الحد عبر النص العادي يوقف مسح الوصف المشفّر ويُعلن البحث جزئيًا
        for i in range(35):
            create_transaction(
                db_session,
                USER_A,
                {"type": "expense", "amount": 1, "currency": "ILS", "person": f"شخص {i}"},
                raw_message="دفعة",
            )
        hits, meta = search_records(db_session, USER_A, "شخص", return_meta=True, limit=30)
        assert len(hits) == 30
        assert meta["saturated"] is True

    def test_like_wildcards_treated_literally(self, db_session):
        # % و _ في مصطلح البحث لا تتصرف كمحارف بدل داخل LIKE (تهريب)
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 30, "currency": "ILS", "category": "خصم"},
            raw_message="خصم 50% للمورد",
        )
        hits, _ = search_records(db_session, USER_A, "50%", return_meta=True)
        assert len(hits) == 1
        hits2, _ = search_records(db_session, USER_A, "خ_صم", return_meta=True)
        assert hits2 == []

    def test_reads_recent_window_once_when_results_sufficient(self, db_session):
        """عند بلوغ الحد نصيًا تُقرأ نافذة الأحدث مرة واحدة فقط لكل نموذج
        (SELECT واحد على transactions) — لا إعادة قراءة للنافذة نفسها."""
        from sqlalchemy import event as sa_event

        for i in range(30):
            create_transaction(
                db_session,
                USER_A,
                {"type": "expense", "amount": 1, "currency": "ILS", "person": f"مورد {i}"},
                raw_message="دفعة",
            )
        create_note(db_session, USER_A, {"description": "شيء آخر", "note_type": "note"}, raw_message="n")
        create_task(db_session, USER_A, {"description": "شيء آخر"}, raw_message="t")

        counts = {"transactions": 0}

        def _count(conn, cursor, statement, parameters, context, executemany):
            flat = statement.lower().replace("\n", " ")
            if statement.lstrip().upper().startswith("SELECT") and " from transactions " in flat:
                counts["transactions"] += 1

        sa_event.listen(db_session.bind, "before_cursor_execute", _count)
        try:
            hits, meta = search_records(db_session, USER_A, "مورد", return_meta=True, limit=10)
        finally:
            sa_event.remove(db_session.bind, "before_cursor_execute", _count)

        assert len(hits) == 10
        assert meta["saturated"] is True
        assert counts["transactions"] == 1


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

    def test_naive_due_date_treated_as_local_and_stored_utc(self, db_session):
        # نص بلا معلومات منطقة زمنية يُعتبر توقيتًا محليًا (Asia/Gaza) ثم
        # يُخزَّن UTC — كما في parse_date_local لمواعيد المهام — لا حرفيًا.
        inv = self._invoice(db_session, due_date="2026-09-10 10:00")
        expected = to_utc_naive(datetime.fromisoformat("2026-09-10 10:00"))
        assert inv.due_date is not None
        assert inv.due_date == expected
        assert inv.due_date != datetime.fromisoformat("2026-09-10 10:00")

    def test_aware_due_date_converted_to_utc(self, db_session):
        inv = self._invoice(db_session, due_date="2026-09-10 10:00+00:00")
        assert inv.due_date == datetime.fromisoformat("2026-09-10 10:00")
        inv2 = self._invoice(db_session, due_date="2026-09-10 10:00+03:00")
        assert inv2.due_date == datetime.fromisoformat("2026-09-10 07:00")

    def test_naive_datetime_object_treated_as_local(self, db_session):
        inv = self._invoice(db_session, due_date=datetime(2026, 9, 10, 10, 0))
        assert inv.due_date == to_utc_naive(datetime(2026, 9, 10, 10, 0))

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

    def test_list_overdue_display_not_committed(self, db_session):
        """تعليم "متأخرة" في القراءة حالة عرض فقط: commit لاحق على نفس الجلسة
        يجب ألا يُثبّت status="overdue" في القاعدة (التحديث حصرًا في الفحص الدوري)."""
        inv = self._invoice(db_session)
        inv.due_date = now_utc() - timedelta(days=2)
        db_session.commit()

        # تُعرض "متأخرة" للقراءة (السلوك القائم)
        listed = list_invoices(db_session, USER_A)
        assert listed[0].id == inv.id
        assert listed[0].status == "overdue"

        # أي commit لاحق على نفس الجلسة لا يُفلش هذا التعديل "التجميلي"
        db_session.commit()

        fresh = (
            db_session.query(Invoice).filter(Invoice.id == inv.id).first()
        )
        assert fresh.status == "pending"


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

    def test_vat_rate_over_100_rejected_before_db_limit(self, db_session):
        """ما بين 100% وحدّ العمود الأقصى (999.999) مرفوض الآن — الضريبة الحقيقية لا
        تتجاوز 100%، وكان الفحص البرمجي القديم (حتى 1000) يسخّر قيدًا أشد من القاعدة."""
        for rate in (101, 500, Decimal("999.999")):
            tx = create_transaction(
                db_session,
                USER_A,
                {
                    "type": "expense",
                    "amount": 100,
                    "currency": "ILS",
                    "vat_rate": rate,
                    "description": "اختبار حد الضريبة",
                },
                raw_message="اختبار",
            )
            assert tx.vat_rate is None, f"rate={rate} should be rejected"

    def test_vat_rate_at_100_accepted(self, db_session):
        tx = create_transaction(
            db_session,
            USER_A,
            {
                "type": "expense",
                "amount": 200,
                "currency": "ILS",
                "vat_rate": 100,
                "description": "اختبار حد أقصى مقبول",
            },
            raw_message="اختبار",
        )
        assert tx.vat_rate == Decimal("100.000")

    def test_vat_consistent_value_within_tolerance_kept(self, db_session):
        """اختلاف طفيف ضمن التسامح (≈4% أو 0.05) مقبول ولا يُعاد بناؤه."""
        tx = create_transaction(
            db_session,
            USER_A,
            {
                "type": "expense",
                "amount": 117,
                "currency": "ILS",
                "vat_rate": 17,
                "vat_amount": 17.20,
                "description": "فاتورة",
            },
            raw_message="فاتورة",
        )
        assert tx.vat_amount == Decimal("17.20")

    def test_vat_inconsistent_amount_auto_corrected(self, db_session):
        """قيمة خارجة عن الاتساق الرياضي (المبلغ شامل الضريبة) تُصحَّح ذاتيًا —
        لا تُقبل بيانات محاسبية متضاربة بصمت: amount=117/rate=17 ⇒ vat=17 لا 50."""
        tx = create_transaction(
            db_session,
            USER_A,
            {
                "type": "expense",
                "amount": 117,
                "currency": "ILS",
                "vat_rate": 17,
                "vat_amount": 50,
                "description": "فاتورة متناقضة",
            },
            raw_message="فاتورة",
        )
        assert tx.vat_amount == Decimal("17.00")

    def test_vat_zero_rate_forces_zero_amount(self, db_session):
        """نسبة 0% مع مبلغ ضريبة موجب يجعل القيمة المتضاربة تُصحَّح إلى صفر."""
        tx = create_transaction(
            db_session,
            USER_A,
            {
                "type": "expense",
                "amount": 100,
                "currency": "ILS",
                "vat_rate": 0,
                "vat_amount": 25,
                "description": "معفاة",
            },
            raw_message="معفاة",
        )
        assert tx.vat_rate == Decimal("0.000")
        assert tx.vat_amount == Decimal("0.00")

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


# ---------- دمج أسماء الأطراف (#داشبورد) ----------


class TestMergePerson:
    def _seed_source(self, db_session):
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "ILS", "person": "محل النور", "description": "فاتورة"},
            raw_message="t1",
        )
        create_note(
            db_session,
            USER_A,
            {"type": "order", "person": "محل النور", "description": "توصيل"},
            raw_message="n1",
        )
        create_task(
            db_session,
            USER_A,
            {"person": "محل النور", "description": "متابعة", "date": "2026-01-01"},
            raw_message="k1",
        )
        create_invoice(
            db_session,
            USER_A,
            {"person": "محل النور", "amount": 50, "currency": "ILS", "description": "ذمة"},
            raw_message="i1",
        )
        set_credit_limit(db_session, USER_A, "محل النور", 500)

    def test_merge_renames_across_all_models(self, db_session):
        self._seed_source(db_session)
        changed = merge_person(db_session, "محل النور", "محل النهار")

        for model in (Transaction, Note, Task, Invoice):
            assert (
                db_session.query(model).filter(model.person == "محل النور").count() == 0
            ), model.__name__
            assert (
                db_session.query(model).filter(model.person == "محل النهار").count() == 1
            ), model.__name__
        assert [lim.person for lim in list_credit_limits(db_session, USER_A)] == ["محل النهار"]
        assert changed >= 5

    def test_merge_credit_collision_keeps_target(self, db_session):
        set_credit_limit(db_session, USER_A, "أصل", 100)
        set_credit_limit(db_session, USER_A, "هدف", 1000)
        merge_person(db_session, "أصل", "هدف")

        limits = list_credit_limits(db_session, USER_A)
        assert [lim.person for lim in limits] == ["هدف"]
        assert limits[0].limit_amount == Decimal("1000.00")

    def test_merge_renames_person_budget(self, db_session):
        from app.database.crud import create_budget

        create_budget(db_session, USER_A, "person", "مورّد قديم", 500)
        merge_person(db_session, "مورّد قديم", "مورّد جديد")

        budgets = db_session.query(Budget).filter(Budget.scope == "person").all()
        assert [b.person for b in budgets] == ["مورّد جديد"]

    def test_merge_invalidates_caches_for_owners(self, db_session, monkeypatch):
        import app.database.crud.common as common_mod

        emitted = []
        monkeypatch.setattr(common_mod, "emit", lambda name, **kwargs: emitted.append(name))
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 10, "currency": "ILS", "person": "مدين", "description": "x"},
            raw_message="t",
        )
        merge_person(db_session, "مدين", "دائن")

        assert "data_written" in emitted

    def test_merge_noop_without_valid_names(self, db_session):
        assert merge_person(db_session, "  ", "ب") == 0
        assert merge_person(db_session, "أ", "أ") == 0

    def test_merge_invalidates_cache_for_loyalty_only_user(self, db_session, monkeypatch):
        import app.database.crud.common as common_mod
        from app.database.crud import loyalty_add_points

        cleared = []
        monkeypatch.setattr(
            common_mod, "clear_cache", lambda key: cleared.append(key)
        )
        monkeypatch.setattr(common_mod, "emit", lambda name, **kwargs: None)

        loyalty_add_points(db_session, USER_B, "مدين", 10)
        merge_person(db_session, "مدين", "دائن")

        assert any(f"run_query:{USER_B}" in key for key in cleared)

    def test_merge_invalidates_cache_for_bonus_plan_user(self, db_session, monkeypatch):
        import app.database.crud.common as common_mod
        from app.database.crud import create_employee_bonus_plan

        cleared = []
        monkeypatch.setattr(
            common_mod, "clear_cache", lambda key: cleared.append(key)
        )
        monkeypatch.setattr(common_mod, "emit", lambda name, **kwargs: None)

        create_employee_bonus_plan(db_session, USER_B, "مدين", "100")
        merge_person(db_session, "مدين", "دائن")

        assert any(f"run_query:{USER_B}" in key for key in cleared)


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

    def test_usage_receivable_side_for_customer(self, db_session):
        """عميل يزيد رصيده عليك (income > expense): side=receivable ويُقاس بالسقف."""
        set_credit_limit(db_session, USER_A, "سامر", "1000")
        create_transaction(
            db_session, USER_A, {"type": "income", "amount": 900, "currency": "ILS", "person": "سامر"}, "بيع بالأجل"
        )
        row = list_credit_limits(db_session, USER_A)[0]
        usage = credit_usage(db_session, row)
        assert usage["side"] == "receivable"
        assert usage["outstanding"] == Decimal("-900.00")
        assert usage["amount"] == Decimal("900.00")
        assert usage["percent"] == 90.0
        assert usage["over"] is False

    def test_usage_balanced(self, db_session):
        set_credit_limit(db_session, USER_A, "خالد", "1000")
        row = list_credit_limits(db_session, USER_A)[0]
        usage = credit_usage(db_session, row)
        assert usage["side"] == "balanced"
        assert usage["amount"] == Decimal("0.00")
        assert usage["over"] is False

    def test_usage_multi_currency_unifies_to_base(self, db_session, monkeypatch):
        """عدة عملات لا تُخلط في رقم خام — تُوحَّد لعملة الأساس بالمبالغ المثبّتة
        وقت التسجيل (ستور بالتصحيح للتحويل: 100 USD = 375 ILS)."""
        monkeypatch.setattr(
            "app.exchange.convert",
            lambda amount, frm, to: {"result": Decimal("375.00")},
        )
        set_credit_limit(db_session, USER_A, "متعدد", "5000")
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 100, "currency": "USD", "person": "متعدد"}, "دين دولار"
        )
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 100, "currency": "ILS", "person": "متعدد"}, "دين شيقل"
        )
        create_transaction(
            db_session, USER_A, {"type": "income", "amount": 40, "currency": "ILS", "person": "متعدد"}, "سداد جزئي"
        )
        row = list_credit_limits(db_session, USER_A)[0]
        usage = credit_usage(db_session, row)
        assert usage["unified_ok"] is True
        assert usage["by_currency"]["USD"]["expense"] == Decimal("100.00")
        assert usage["by_currency"]["ILS"]["balance"] == Decimal("60.00")
        # مصروف 375+100=475 محوّلًا → ناقص إيراد 40 = 435 بعملة الأساس
        assert usage["outstanding"] == Decimal("435.00")
        assert usage["side"] == "payable"
        assert usage["over"] is False

    def test_usage_multi_currency_unavailable_notifies_none(self, db_session, monkeypatch):
        """تعذّر توحيد عدة عملات (لا أسعار/شبكة) — «غير متاح» بدل رقم مختلط،
        ويمتنع التنبيه بالتجاوز (over=False) كي لا يُنبه رقماً خاطئًا."""
        monkeypatch.setattr("app.exchange.convert", lambda *a, **k: {"result": None})
        monkeypatch.setattr("app.exchange.get_rate", lambda *a, **k: None)
        set_credit_limit(db_session, USER_A, "متعدد", "3000")
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 100, "currency": "USD", "person": "متعدد"}, "دين دولار"
        )
        create_transaction(
            db_session, USER_A, {"type": "income", "amount": 50, "currency": "ILS", "person": "متعدد"}, "سداد"
        )
        row = list_credit_limits(db_session, USER_A)[0]
        usage = credit_usage(db_session, row)
        assert usage["unified_ok"] is False
        assert usage["outstanding"] is None
        assert usage["amount"] is None
        assert usage["percent"] is None
        assert usage["over"] is False
        assert usage["side"] == "unknown"

    def test_monthly_reset_clears_status_from_previous_month(self, db_session):
        row = set_credit_limit(db_session, USER_A, "نادر", "1000")
        row.alerted_status = 2
        row.updated_at = to_utc_naive(datetime(2020, 1, 15))  # شهر سابق
        db_session.commit()

        assert credit_monthly_reset(db_session, row) is True
        assert row.alerted_status == 0
        assert not credit_monthly_reset(db_session, row)  # العلامة تقدّمت للشهر الجاري

    def test_monthly_reset_same_month_no_change(self, db_session):
        row = set_credit_limit(db_session, USER_A, "نادر", "1000")
        row.alerted_status = 2
        row.updated_at = now_utc()  # نفس الشهر
        db_session.commit()

        assert credit_monthly_reset(db_session, row) is False
        assert row.alerted_status == 2

    def test_monthly_reset_falls_back_to_created_at(self, db_session):
        row = CreditLimit(
            telegram_user_id=USER_A,
            person="قديم",
            limit_amount=Decimal("500"),
            alerted_status=2,
            updated_at=None,
            created_at=to_utc_naive(datetime(2019, 5, 1)),
        )
        db_session.add(row)
        db_session.commit()

        assert credit_monthly_reset(db_session, row) is True
        assert row.alerted_status == 0


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

    def test_category_does_not_match_partial(self, db_session):
        """تصنيف «صيانة» لا يلتقط معاملة تصنيفها «صيانة سيارة» — مطابقة تامة كالشخص."""
        from app.database.crud import budget_usage, create_budget

        budget = create_budget(db_session, USER_A, "category", "صيانة", "1000")
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 300, "currency": "ILS", "category": "صيانة"},
            "صيانة مباشرة",
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 200, "currency": "ILS", "category": "صيانة سيارة"},
            "تصنيف يحتوي الكلمة كجزء — يجب ألا يُحتسب",
        )
        assert budget_usage(db_session, budget)["spent"] == Decimal("300.00")


# ---------- ميزانية الشخص: لا تُخلط العملات معًا ----------


class TestBudgetPersonScopeCurrency:
    def test_person_spent_converted_to_base_currency(self, db_session, monkeypatch):
        """مصاريف شخص بعدة عملات تُجمع بعملة الأساس (لا شيكل+دولار في رقم خام)."""
        from app.database.crud import budget_usage, create_budget

        monkeypatch.setattr(
            "app.exchange.convert",
            lambda amount, frm, to: {"result": Decimal("750.00")},
        )
        budget = create_budget(db_session, USER_A, "person", "خالد", "1000")
        assert budget is not None

        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "ILS", "person": "خالد"},
            "دفعة شيقل",
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "USD", "person": "خالد"},
            "دفعة دولار",
        )
        usage = budget_usage(db_session, budget)
        # 100 ILS مباشرة + 100 USD مثبَّت وقت التسجيل = 750 ILS → الإجمالي 850
        assert usage["spent"] == Decimal("850.00")
        assert usage["percent"] == 85.0
        assert usage["over"] is False

    def test_person_spent_stays_in_single_currency(self, db_session):
        """مصروف واحد بعملة الأساس يُحتسب كما هو (نفس سلوك النطاق بالعملة)."""
        from app.database.crud import budget_usage, create_budget

        budget = create_budget(db_session, USER_A, "person", "سارة", "500")
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 200, "currency": "ILS", "person": "سارة"},
            "دفعة",
        )
        usage = budget_usage(db_session, budget)
        assert usage["spent"] == Decimal("200.00")
        assert usage["percent"] == 40.0

    def test_person_spent_fallback_no_mixing(self, db_session, monkeypatch):
        """تعذّر التوحيد (لا أسعار) — لا يُبنى مجموع خام يخلط العملات؛ عملة الأساس
        فقط (100 ILS) بدل 100 دولار+100 شيكل في رقم واحد."""
        from app.database.crud import budget_usage, create_budget

        monkeypatch.setattr("app.exchange.convert", lambda *a, **k: {"result": None})
        monkeypatch.setattr("app.exchange.get_rate", lambda *a, **k: None)
        budget = create_budget(db_session, USER_A, "person", "ريم", "1000")
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "USD", "person": "ريم"},
            "دفعة دولار",
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "ILS", "person": "ريم"},
            "دفعة شيقل",
        )
        usage = budget_usage(db_session, budget)
        assert usage["spent"] == Decimal("100.00")
        assert usage["percent"] == 10.0
        assert usage["over"] is False


# ---------- دقة أسماء الأشخاص خارج search_records (#3/#4) ----------


class TestPersonMatchPrecision:
    """لا خلط بين أشخاص مختلفين في الحسابات المالية:
    3) % و _ المحارف البدل تهرَّب (للتصنيف في الميزانية).
    4) مطابقة الأشخاص تامة (==) — «علي» لا يطابق «عبدالعلي»."""

    PRECISE = "الرجل_الغامض"
    OTHER = "الرجلXالغامض"  # "_" المطابق لحرف واحد في LIKE غير مهرّب

    def _base_expenses(self, db_session):
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 400, "currency": "ILS", "person": self.PRECISE},
            "دفعة للاسم الحرفي",
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 999, "currency": "ILS", "person": self.OTHER},
            "دفعة للاسم المختلف",
        )

    def test_credit_usage_exact_match(self, db_session):
        self._base_expenses(db_session)
        row = set_credit_limit(db_session, USER_A, self.PRECISE, "1000")
        usage = credit_usage(db_session, row)
        assert usage["outstanding"] == Decimal("400.00")
        assert usage["limit"] == Decimal("1000.00")

    def test_budget_person_exact_match(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "app.exchange.convert",
            lambda amount, frm, to: {"result": Decimal(str(amount))},
        )
        self._base_expenses(db_session)
        from app.database.crud import budget_usage, create_budget

        budget = create_budget(db_session, USER_A, "person", self.PRECISE, "5000")
        assert budget_usage(db_session, budget)["spent"] == Decimal("400.00")

    def test_budget_category_exact_match(self, db_session):
        """مطابقة التصنيف تامة: «خصم_50%» لا يطابق «خصمX50Y»، والمحارف البدل نصّ حرفي."""
        from app.database.crud import budget_usage, create_budget

        budget = create_budget(db_session, USER_A, "category", "خصم_50%", "5000")
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 100, "currency": "ILS", "category": "خصم_50%"},
            "خصم حرفي",
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 200, "currency": "ILS", "category": "خصمX50Y"},
            "تصنيف مختلف",
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 300, "currency": "ILS", "category": "خصم_50%_إضافي"},
            "تصنيف أطول من ميزانية التصنيف",
        )
        assert budget_usage(db_session, budget)["spent"] == Decimal("100.00")

    def test_task_person_filter_exact_match(self, db_session):
        create_task(
            db_session, USER_A, {"description": "مهمة دقيقة", "person": self.PRECISE}, raw_message="t1"
        )
        create_task(
            db_session, USER_A, {"description": "مهمة غامضة", "person": self.OTHER}, raw_message="t2"
        )
        tasks = list_pending_tasks(db_session, USER_A, person=self.PRECISE)
        assert [t.description for t in tasks] == ["مهمة دقيقة"]

    def test_prefix_names_do_not_mix(self, db_session, monkeypatch):
        """«علي» لا يطابق «عبدالعلي» في السقف الائتماني والميزانية والقياس."""
        monkeypatch.setattr(
            "app.exchange.convert",
            lambda amount, frm, to: {"result": Decimal(str(amount))},
        )
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 100, "currency": "ILS", "person": "علي"}, "و"
        )
        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 300, "currency": "ILS", "person": "عبدالعلي"}, "ع"
        )
        row = set_credit_limit(db_session, USER_A, "علي", "500")
        assert credit_usage(db_session, row)["outstanding"] == Decimal("100.00")

        from app.database.crud import budget_usage, create_budget

        budget = create_budget(db_session, USER_A, "person", "علي", "1000")
        assert budget_usage(db_session, budget)["spent"] == Decimal("100.00")

        metric = run_query(
            db_session,
            USER_A,
            {"metric": "total_expenses", "period": "all_time", "person": "علي"},
        )
        assert metric["result"] == {"ILS": Decimal("100.00")}


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

    def test_list_pending_bounded_fetch_keeps_high_priority(self, db_session):
        # أكثر من حد الجلب (500) — مهمة عاجلة قديمة يجب ألا تضيع بسبب LIMIT في SQL.
        # ترتيب SQL يطابق مقارنة _priority_sort، لذا تبقى النتيجة مطابقة للجلب الكامل.
        old_high = create_task(
            db_session,
            USER_A,
            {"description": "عاجلة قديمة", "priority": "high", "date": "2025-01-01 09:00"},
            raw_message="عاجلة قديمة",
        )
        for i in range(520):
            create_task(db_session, USER_A, {"description": f"مهمة {i}"}, raw_message=f"م{i}")
        tasks = list_pending_tasks(db_session, USER_A, limit=10)
        ids = [t.id for t in tasks]
        assert len(tasks) == 10
        assert ids[0] == old_high.id
        assert len(set(ids)) == 10

    def test_list_pending_honors_priority_then_due_then_recent(self, db_session):
        # لا تاريخ موعد → يأتي بعد المهام ذات الموعد داخل نفس الأولوية
        a = create_task(db_session, USER_A, {"description": "بدون موعد"}, raw_message="أ")
        b = create_task(
            db_session, USER_A, {"description": "بموعد أقرب", "date": "2026-09-01 10:00"},
            raw_message="ب",
        )
        c = create_task(
            db_session, USER_A, {"description": "بموعد أبعد", "date": "2026-09-05 10:00"},
            raw_message="ج",
        )
        db_session.flush()
        # استرداد straight من SQL: nulls يجب أن يكون أخيرًا داخل نفس الأولوية
        ids = [t.id for t in list_pending_tasks(db_session, USER_A)]
        assert ids.index(b.id) < ids.index(c.id) < ids.index(a.id)

    def test_list_overdue_bounded_and_sorted(self, db_session):
        for i in range(520):
            create_task(db_session, USER_A, {"description": f"مهمة {i}"}, raw_message=f"م{i}")
        db_session.query(Task).filter(Task.telegram_user_id == USER_A).update(
            {Task.status: "overdue"}, synchronize_session=False
        )
        db_session.commit()
        tasks = list_overdue_tasks(db_session, USER_A, limit=10)
        assert len(tasks) == 10
        assert len({t.id for t in tasks}) == 10

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
        from app.database.crud import (
            accept_workspace_invite,
            can_manage_records,
            create_workspace,
            invite_to_workspace,
        )

        assert can_manage_records(db_session, USER_A) is True
        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        assert can_manage_records(db_session, USER_B) is True  # معلّق — لا يزال فرديًا
        accept_workspace_invite(db_session, USER_B, USER_A)
        assert can_manage_records(db_session, USER_A) is True
        assert can_manage_records(db_session, USER_B) is False

    def test_member_undo_denied_owner_allowed(self, db_session):
        from app.database.crud import (
            accept_workspace_invite,
            create_workspace,
            invite_to_workspace,
            leave_workspace,
        )

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        accept_workspace_invite(db_session, USER_B, USER_A)
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
            accept_workspace_invite,
            create_workspace,
            delete_task_by_id,
            invite_to_workspace,
            list_pending_tasks,
        )

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        accept_workspace_invite(db_session, USER_B, USER_A)
        task = create_task(db_session, USER_B, {"description": "مهمة الشريك"}, raw_message="مهمة")
        assert delete_task_by_id(db_session, USER_B, task.id) is None
        assert list_pending_tasks(db_session, USER_B)[0].id == task.id
        assert delete_task_by_id(db_session, USER_A, task.id) is not None


class TestCompleteTaskRace:
    """سباق التزامن على إنجاز مهمة متكررة — يجب ألا يُولَّد سوى تكرار واحد."""

    def test_concurrent_completions_spawn_single_recurrence(self, tmp_path):
        """طلبان متزامنان لإنجاز نفس المهمة يسفران عن تكرار واحد (لا مكرَّرين)."""
        import threading

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database.db import Base

        engine = create_engine(
            f"sqlite:///{str(tmp_path / 'race.db')}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        seed = Session()
        task = create_task(
            seed,
            USER_A,
            {"description": "جرد المخزن", "recurrence": "daily", "date": "2099-01-01 10:00"},
            raw_message="كل يوم جرد المخزن",
        )
        tid = task.id
        seed.close()

        barrier = threading.Barrier(2)
        results = []

        def _attempt():
            db = Session()
            barrier.wait(timeout=10)
            try:
                results.append(complete_task(db, USER_A, tid))
            finally:
                db.close()

        threads = [threading.Thread(target=_attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        check = Session()
        try:
            assert check.query(Task).filter(Task.id == tid).first().status == "done"
            respawn = (
                check.query(Task)
                .filter(
                    Task.id != tid,
                    Task.recurrence_rule == "daily",
                    Task.status == "pending",
                    Task.deleted_at.is_(None),
                )
                .count()
            )
            assert respawn == 1, "سباق الإنجاز ولّد مهمة متكررة مكرَّرة"
            assert sum(1 for r in results if r is not None) == 1
        finally:
            check.close()
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_second_completion_no_duplicate_spawn(self, db_session):
        """إنجاز ثانٍ لنفس المهمة (بعد أن أُنجزت) لا يُعيد توليد التكرار."""
        task = create_task(
            db_session,
            USER_A,
            {"description": "جرد المخزن", "recurrence": "daily", "date": "2099-01-01 10:00"},
            raw_message="كل يوم جرد المخزن",
        )
        assert complete_task(db_session, USER_A, task.id) is not None
        assert complete_task(db_session, USER_A, task.id) is None
        spawns = (
            db_session.query(Task)
            .filter(
                Task.id != task.id,
                Task.recurrence_rule == "daily",
                Task.status == "pending",
                Task.deleted_at.is_(None),
            )
            .count()
        )
        assert spawns == 1
