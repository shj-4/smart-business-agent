"""
تصدير التقارير كملفات Excel (.xlsx) عبر openpyxl.

تُولّد ملفات Excel مُنسّقة بعنوانين تلقائيين، وصفوف م-sum، وألوان رأسية.
"""

import io
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from sqlalchemy.orm import Session

from app.database.crud import accessible_user_ids
from app.database.models import Note, Task, Transaction
from app.timeutil import to_local_naive

HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
SUM_FONT = Font(bold=True, size=11)
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)


def _style_header(ws, cols: int):
    for col in range(1, cols + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER


def _auto_width(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, 40)


def generate_transactions_excel(
    db: Session,
    telegram_user_id: int,
    start_utc=None,
    end_utc=None,
) -> io.BytesIO:
    """يولّد ملف Excel يحتوي على المعاملات المالية لفترة محددة."""
    q = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
        Transaction.deleted_at.is_(None),
    )
    if start_utc:
        q = q.filter(Transaction.created_at >= start_utc)
    if end_utc:
        q = q.filter(Transaction.created_at <= end_utc)

    rows = q.order_by(Transaction.created_at.desc()).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "المعاملات المالية"
    _write_transactions_sheet(ws, rows)
    _auto_width(ws)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _write_transactions_sheet(ws, rows):
    """يكتب معاملات مالية في ورقة عمل (العناوين + الصفوف + الإجماليات)."""
    headers = ["التاريخ", "النوع", "المبلغ", "العملة", "الشخص", "التصنيف", "الوصف"]
    ws.append(headers)
    _style_header(ws, len(headers))

    total_expense = Decimal("0")
    total_income = Decimal("0")

    for r in rows:
        local_date = to_local_naive(r.created_at)
        date_str = local_date.strftime("%Y-%m-%d %H:%M") if local_date else ""
        type_label = "مصروف" if r.type == "expense" else "إيراد"
        amount = r.amount or Decimal("0")

        ws.append(
            [
                date_str,
                type_label,
                float(amount),
                r.currency or "",
                r.person or "",
                r.category or "",
                r.description or "",
            ]
        )

        row_idx = ws.max_row
        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=col_idx).border = THIN_BORDER

        if r.type == "expense":
            total_expense += amount
        else:
            total_income += amount

    if rows:
        sum_row = ws.max_row + 2
        ws.cell(row=sum_row, column=1, value="الإجمالي").font = SUM_FONT
        ws.cell(row=sum_row, column=2, value="مصروفات").font = SUM_FONT
        ws.cell(row=sum_row, column=3, value=float(total_expense)).font = SUM_FONT
        ws.cell(row=sum_row + 1, column=2, value="إيرادات").font = SUM_FONT
        ws.cell(row=sum_row + 1, column=3, value=float(total_income)).font = SUM_FONT
        ws.cell(row=sum_row + 2, column=2, value="الصافي").font = SUM_FONT
        ws.cell(
            row=sum_row + 2, column=3, value=float(total_income - total_expense)
        ).font = SUM_FONT


def generate_tasks_excel(
    db: Session,
    telegram_user_id: int,
) -> io.BytesIO:
    """يولّد ملف Excel يحتوي على المهام (pending + overdue + done)."""
    from app.database.crud import mark_overdue_tasks

    mark_overdue_tasks(db, telegram_user_id)

    tasks = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.due_date.asc().nulls_last())
        .all()
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "المهام"
    _write_tasks_sheet(ws, tasks)
    _auto_width(ws)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _write_tasks_sheet(ws, tasks):
    """يكتب المهام في ورقة عمل."""
    headers = ["الحالة", "الوصف", "الشخص", "الموعد", "تاريخ الإنشاء"]
    ws.append(headers)
    _style_header(ws, len(headers))

    STATUS_LABELS = {"pending": "قيد الانتظار", "overdue": "متأخرة", "done": "مكتملة"}

    for t in tasks:
        due_str = ""
        if t.due_date:
            due_str = to_local_naive(t.due_date).strftime("%Y-%m-%d %H:%M")
        created_str = (
            to_local_naive(t.created_at).strftime("%Y-%m-%d %H:%M") if t.created_at else ""
        )

        ws.append(
            [
                STATUS_LABELS.get(t.status, t.status),
                t.description or "",
                t.person or "",
                due_str,
                created_str,
            ]
        )
        row_idx = ws.max_row
        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=col_idx).border = THIN_BORDER


def generate_notes_excel(
    db: Session,
    telegram_user_id: int,
) -> io.BytesIO:
    """يولّد ملف Excel يحتوي على الطلبيات والملاحظات."""
    notes = (
        db.query(Note)
        .filter(
            Note.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Note.deleted_at.is_(None),
        )
        .order_by(Note.created_at.desc())
        .all()
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "الطلبيات والملاحظات"
    _write_notes_sheet(ws, notes)
    _auto_width(ws)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _write_notes_sheet(ws, notes):
    """يكتب الطلبيات والملاحظات في ورقة عمل."""
    headers = ["النوع", "الوصف", "الشخص", "التصنيف", "تاريخ الإنشاء"]
    ws.append(headers)
    _style_header(ws, len(headers))

    TYPE_LABELS = {"order": "طلبية", "note": "ملاحظة"}

    for n in notes:
        created_str = (
            to_local_naive(n.created_at).strftime("%Y-%m-%d %H:%M") if n.created_at else ""
        )
        ws.append(
            [
                TYPE_LABELS.get(n.note_type, n.note_type),
                n.description or "",
                n.person or "",
                n.category or "",
                created_str,
            ]
        )
        row_idx = ws.max_row
        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=col_idx).border = THIN_BORDER


def generate_export_excel(
    db: Session,
    telegram_user_id: int,
    start_utc=None,
    end_utc=None,
) -> io.BytesIO:
    """يولّد ملف Excel واحدًا بكل السجلات (معاملات + مهام + طلبيات/ملاحظات) لفترة محددة.

    يُستخدم مع أمر /export. الفترة تنطبق على المعاملات؛ المهام والملاحظات
    تُضمّن كاملة (كلها). العناوين على 3 أوراق.
    """
    from app.database.crud import mark_overdue_tasks

    # المعاملات ضمن الفترة
    tq = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
        Transaction.deleted_at.is_(None),
    )
    if start_utc:
        tq = tq.filter(Transaction.created_at >= start_utc)
    if end_utc:
        tq = tq.filter(Transaction.created_at <= end_utc)
    transactions = tq.order_by(Transaction.created_at.desc()).all()

    mark_overdue_tasks(db, telegram_user_id)
    tasks = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.due_date.asc().nulls_last())
        .all()
    )

    notes = (
        db.query(Note)
        .filter(
            Note.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Note.deleted_at.is_(None),
        )
        .order_by(Note.created_at.desc())
        .all()
    )

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "المعاملات المالية"
    _write_transactions_sheet(ws1, transactions)

    ws2 = wb.create_sheet("المهام")
    _write_tasks_sheet(ws2, tasks)

    ws3 = wb.create_sheet("الطلبيات والملاحظات")
    _write_notes_sheet(ws3, notes)

    for ws in (ws1, ws2, ws3):
        _auto_width(ws)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
