"""
Unit tests لتصدير Excel (bot/exporters) — يتأكد أن الملفات صالحة CSV/xlsx.
"""

from io import BytesIO

from openpyxl import load_workbook

from app.database.crud import create_note, create_task, create_transaction
from bot.exporters import (
    generate_export_pdf,
    generate_notes_excel,
    generate_tasks_excel,
    generate_transactions_excel,
)

USER_A = 111


class TestTransactionsExcel:
    def test_empty_db_produces_valid_workbook(self, db_session):
        buf = generate_transactions_excel(db_session, USER_A)
        assert isinstance(buf, BytesIO)
        buf.seek(0)
        wb = load_workbook(buf)
        assert wb.sheetnames == ["المعاملات المالية"]
        ws = wb.active
        # رأس + لا صفوف بيانات (بدون مبالغ) — في حالت الفارغة لا صف إجمالي
        assert ws.max_row >= 1

    def test_generates_rows_with_totals(self, db_session):
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 300, "currency": "ILS", "description": "مواد"},
            raw_message="دفعة 300",
        )
        create_transaction(
            db_session,
            USER_A,
            {"type": "income", "amount": 500, "currency": "USD", "description": "قبض"},
            raw_message="استلام 500",
        )
        buf = generate_transactions_excel(db_session, USER_A)
        buf.seek(0)
        wb = load_workbook(buf)
        ws = wb.active
        rows_all = list(ws.iter_rows(values_only=True))
        # الإجماليات تُعرض لكل عملة على حدة — لا نخلط 300 شيكل مع 500 دولار
        assert any(v == "الإجمالي" for v in (row[0] for row in rows_all))
        net_by_cur = {
            row[3]: float(row[2])
            for row in rows_all
            if row and len(row) >= 4 and row[1] == "الصافي" and row[3]
        }
        assert net_by_cur == {"ILS": -300.0, "USD": 500.0}


class TestTasksExcel:
    def test_valid_workbook_with_statuses(self, db_session):
        create_task(
            db_session,
            USER_A,
            {"description": "مهمة أ", "date": "2026-09-20 12:00"},
            raw_message="مهمة أ",
        )
        buf = generate_tasks_excel(db_session, USER_A)
        buf.seek(0)
        wb = load_workbook(buf)
        ws = wb.active
        values = [row[0] for row in ws.iter_rows(values_only=True)]
        # header + صف المهمة
        assert any(v == "قيد الانتظار" for v in values)


class TestNotesExcel:
    def test_valid_workbook_with_notes(self, db_session):
        create_note(
            db_session,
            USER_A,
            {"type": "order", "description": "طلب أسمنت", "person": "محمد"},
            raw_message="طلبية أسمنت",
        )
        buf = generate_notes_excel(db_session, USER_A)
        buf.seek(0)
        wb = load_workbook(buf)
        ws = wb.active
        values = [row[0] for row in ws.iter_rows(values_only=True)]
        assert any(v == "طلبية" for v in values)


class TestExportPdf:
    def test_empty_db_produces_valid_pdf(self, db_session):
        buf = generate_export_pdf(db_session, USER_A)
        assert isinstance(buf, BytesIO)
        buf.seek(0)
        head = buf.read(5)
        assert head == b"%PDF-"
        assert len(buf.getvalue()) > 1000

    def test_pdf_includes_seeded_rows(self, db_session):
        create_transaction(
            db_session,
            USER_A,
            {"type": "expense", "amount": 300, "currency": "ILS", "description": "مواد"},
            raw_message="دفعة 300",
        )
        create_task(
            db_session,
            USER_A,
            {"description": "مهمة أ", "date": "2026-09-20 12:00"},
            raw_message="مهمة أ",
        )
        create_note(
            db_session,
            USER_A,
            {"type": "order", "description": "طلب أسمنت", "person": "محمد"},
            raw_message="طلبية أسمنت",
        )
        buf = generate_export_pdf(db_session, USER_A)
        assert len(buf.getvalue()) > 1500
