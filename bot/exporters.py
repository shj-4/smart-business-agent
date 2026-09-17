"""
تصدير التقارير كملفات Excel (.xlsx) عبر openpyxl.

تُولّد ملفات Excel مُنسّقة بعنوانين تلقائيين، وصفوف م-sum، وألوان رأسية.
"""

import io
from datetime import datetime
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet
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


def _scope_ids(db: Session, telegram_user_id: int | None) -> list[int]:
    """يعيد معرّفات المستخدمين المسموح تضمينهم في التصدير.

    عند None (لوحة التحكم: تقرير شامل) تعود كل المعرّفات الموجودة في الجداول.
    عند رقم: معرّف المستخدم + أعضاء مساحته عبر accessible_user_ids.
    """
    if telegram_user_id is not None:
        return accessible_user_ids(db, telegram_user_id)
    ids = {
        rid
        for (rid,) in db.query(Transaction.telegram_user_id).all()
        if rid is not None
    }
    ids.update(rid for (rid,) in db.query(Task.telegram_user_id).all() if rid is not None)
    ids.update(rid for (rid,) in db.query(Note.telegram_user_id).all() if rid is not None)
    return list(ids) or [-1]


def _style_header(ws: Worksheet, cols: int) -> None:
    for col in range(1, cols + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER


def _auto_width(ws: Worksheet) -> None:
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, 40)


def generate_transactions_excel(
    db: Session,
    telegram_user_id: int | None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> io.BytesIO:
    """يولّد ملف Excel يحتوي على المعاملات المالية لفترة محددة."""
    q = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
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


def _write_transactions_sheet(ws: Worksheet, rows: list[Transaction]) -> None:
    """يكتب معاملات مالية في ورقة عمل (العناوين + الصفوف + الإجماليات)."""
    headers = ["التاريخ", "النوع", "المبلغ", "العملة", "الشخص", "التصنيف", "الوصف"]
    ws.append(headers)
    _style_header(ws, len(headers))

    totals: dict[str, list[Decimal]] = {}

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

        cur = r.currency or "غير محددة"
        bucket = totals.setdefault(cur, [Decimal("0"), Decimal("0")])
        if r.type == "expense":
            bucket[0] += amount
        else:
            bucket[1] += amount

    if rows:
        # الإجماليات تُعرض لكل عملة بمبالغها — لا رقم واحد يخلط عملات مختلفة.
        sum_row = ws.max_row + 2
        for i, (cur, (total_expense, total_income)) in enumerate(sorted(totals.items())):
            base_row = sum_row + i * 3
            ws.cell(row=base_row, column=1, value="الإجمالي" if i == 0 else "").font = SUM_FONT
            ws.cell(row=base_row, column=2, value="مصروفات").font = SUM_FONT
            ws.cell(row=base_row, column=3, value=float(total_expense)).font = SUM_FONT
            ws.cell(row=base_row, column=4, value=cur).font = SUM_FONT
            ws.cell(row=base_row + 1, column=2, value="إيرادات").font = SUM_FONT
            ws.cell(row=base_row + 1, column=3, value=float(total_income)).font = SUM_FONT
            ws.cell(row=base_row + 1, column=4, value=cur).font = SUM_FONT
            ws.cell(row=base_row + 2, column=2, value="الصافي").font = SUM_FONT
            ws.cell(
                row=base_row + 2, column=3, value=float(total_income - total_expense)
            ).font = SUM_FONT
            ws.cell(row=base_row + 2, column=4, value=cur).font = SUM_FONT


def generate_tasks_excel(
    db: Session,
    telegram_user_id: int | None,
) -> io.BytesIO:
    """يولّد ملف Excel يحتوي على المهام (pending + overdue + done)."""
    from app.database.crud import mark_overdue_tasks

    if telegram_user_id is not None:
        mark_overdue_tasks(db, telegram_user_id)

    tasks = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
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


def _write_tasks_sheet(ws: Worksheet, tasks: list[Task]) -> None:
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
    telegram_user_id: int | None,
) -> io.BytesIO:
    """يولّد ملف Excel يحتوي على الطلبيات والملاحظات."""
    notes = (
        db.query(Note)
        .filter(
            Note.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
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


def _write_notes_sheet(ws: Worksheet, notes: list[Note]) -> None:
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
    telegram_user_id: int | None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> io.BytesIO:
    """يولّد ملف Excel واحدًا بكل السجلات (معاملات + مهام + طلبيات/ملاحظات) لفترة محددة.

    يُستخدم مع أمر /export. الفترة تنطبق على المعاملات؛ المهام والملاحظات
    تُضمّن كاملة (كلها). العناوين على 3 أوراق.
    """
    from app.database.crud import mark_overdue_tasks

    # المعاملات ضمن الفترة
    tq = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
        Transaction.deleted_at.is_(None),
    )
    if start_utc:
        tq = tq.filter(Transaction.created_at >= start_utc)
    if end_utc:
        tq = tq.filter(Transaction.created_at <= end_utc)
    transactions = tq.order_by(Transaction.created_at.desc()).all()

    if telegram_user_id is not None:
        mark_overdue_tasks(db, telegram_user_id)
    tasks = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.due_date.asc().nulls_last())
        .all()
    )

    notes = (
        db.query(Note)
        .filter(
            Note.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
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


# ---------- تصدير PDF (#35) عبر reportlab ----------


def _pdf_font() -> str:
    """يسجّل خطًا يدعم العربية (TTF من النظام) ويُعيد اسمه، أو Helvetica كخلفية."""
    import os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if _pdf_font.cached:
        return _pdf_font.cached
    candidates = (
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\times.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    chosen = "Helvetica"
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("ArabicImport", path))
                chosen = "ArabicImport"
                break
            except Exception:  # noqa: BLE001
                continue
    _pdf_font.cached = chosen
    return chosen


_pdf_font.cached = None


def _pdf_text(value: str | None, font: str) -> str:
    """يعيد نصًا (قد يكون عربيًا) جاهزًا لعرض صحيح في PDF — تشكيل+اتجاه إن توفرت."""
    text = str(value or "")
    if font == "Helvetica" or not text.strip():
        return text.replace("\n", " ")
    if any(0x0600 <= ord(ch) <= 0x06FF for ch in text):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display

            return get_display(arabic_reshaper.reshape(text))
        except Exception:  # noqa: BLE001
            pass
        return text.replace("\n", " ")
    # اتجاه عكسي يتجنّب كسر النص الإنجليزي مع أرقام
    return text.replace("\n", " ")


def generate_export_pdf(
    db: Session,
    telegram_user_id: int | None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> io.BytesIO:
    """يولّد ملف PDF واحدًا بكل السجلات (معاملات + مهام + طلبيات/ملاحظات).

    يعتمد على reportlab (مكتبة اختيارية تُحمَّل عند الاستدعاء). يعيد BytesIO.
    """
    from datetime import datetime

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    from app.database.crud import mark_overdue_tasks

    _FONT = _pdf_font()

    tq = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
        Transaction.deleted_at.is_(None),
    )
    if start_utc:
        tq = tq.filter(Transaction.created_at >= start_utc)
    if end_utc:
        tq = tq.filter(Transaction.created_at <= end_utc)
    transactions = tq.order_by(Transaction.created_at.desc()).all()

    if telegram_user_id is not None:
        mark_overdue_tasks(db, telegram_user_id)
    tasks = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.due_date.asc().nulls_last())
        .all()
    )
    notes = (
        db.query(Note)
        .filter(
            Note.telegram_user_id.in_(_scope_ids(db, telegram_user_id)),
            Note.deleted_at.is_(None),
        )
        .order_by(Note.created_at.desc())
        .all()
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleRTL", parent=styles["Title"], fontSize=15, fontName=_FONT)
    heading_style = ParagraphStyle(
        "HeadingRTL", parent=styles["Heading2"], fontSize=12, fontName=_FONT
    )
    body_style = ParagraphStyle("BodyRTL", parent=styles["BodyText"], fontSize=9, fontName=_FONT)
    cell_style = ParagraphStyle("CellRTL", parent=styles["BodyText"], fontSize=8, fontName=_FONT)

    def _as_table(
        headers: list[str],
        rows: list[list[str]],
        first_total_idx: int | None = None,
    ) -> Table:
        data = [[Paragraph(_pdf_text(h, _FONT), cell_style) for h in headers]]
        for r in rows:
            data.append([Paragraph(_pdf_text(cell, _FONT), cell_style) for cell in r])
        table = Table(data, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2F5496")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2FA")]),
            ("FONTNAME", (0, 0), (-1, -1), _FONT),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        if first_total_idx is not None:
            style.append(
                ("FONTNAME", (0, first_total_idx), (-1, -1), "Helvetica-Bold" if _FONT == "Helvetica" else _FONT)
            )
        table.setStyle(TableStyle(style))
        return table

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    story = [Paragraph("تقرير النشاط المالي الكامل", title_style), Spacer(1, 2 * mm)]
    story.append(Paragraph(f"تاريخ التوليد: {now_str}", body_style))
    story.append(Spacer(1, 3 * mm))

    story.append(Paragraph("١) المعاملات المالية", heading_style))
    tx_rows = []
    tx_totals: dict[str, list[Decimal]] = {}
    for r in transactions:
        amount = r.amount or Decimal("0")
        local_date = to_local_naive(r.created_at)
        tx_rows.append(
            [
                local_date.strftime("%Y-%m-%d %H:%M") if local_date else "",
                "مصروف" if r.type == "expense" else "إيراد",
                format(float(amount), ".2f"),
                r.currency or "",
                r.person or "",
                r.category or "",
                r.description or "",
            ]
        )
        cur = r.currency or "غير محددة"
        bucket = tx_totals.setdefault(cur, [Decimal("0"), Decimal("0")])
        if r.type == "expense":
            bucket[0] += amount
        else:
            bucket[1] += amount
    tx_headers = ["التاريخ", "النوع", "المبلغ", "العملة", "الشخص", "التصنيف", "الوصف"]
    first_total_idx = None
    if tx_rows:
        # الإجماليات لكل عملة على حدة — لا نجمع عملات مختلفة في رقم واحد.
        first_total_idx = len(tx_rows)
        for cur, (total_expense, total_income) in sorted(tx_totals.items()):
            tx_rows.append(["الإجمالي", "مصروفات", format(float(total_expense), ".2f"), cur, "", "", ""])
            tx_rows.append(["", "إيرادات", format(float(total_income), ".2f"), cur, "", "", ""])
            tx_rows.append(["", "الصافي", format(float(total_income - total_expense), ".2f"), cur, "", "", ""])
    else:
        tx_rows.append(["(لا توجد معاملات)", "", "", "", "", "", ""])
    story.append(_as_table(tx_headers, tx_rows, first_total_idx))
    story.append(Spacer(1, 4 * mm))

    story.append(Paragraph("٢) المهام", heading_style))
    status_labels = {"pending": "قيد الانتظار", "overdue": "متأخرة", "done": "مكتملة"}
    task_rows = [
        [
            status_labels.get(t.status, t.status),
            t.description or "",
            t.person or "",
            to_local_naive(t.due_date).strftime("%Y-%m-%d %H:%M") if t.due_date else "",
        ]
        for t in tasks
    ]
    if not task_rows:
        task_rows.append(["(لا توجد مهام)", "", "", ""])
    story.append(_as_table(["الحالة", "الوصف", "الشخص", "الموعد"], task_rows))
    story.append(Spacer(1, 4 * mm))

    story.append(Paragraph("٣) الطلبيات والملاحظات", heading_style))
    type_labels = {"order": "طلبية", "note": "ملاحظة"}
    note_rows = [
        [
            type_labels.get(n.note_type, n.note_type),
            n.description or "",
            n.person or "",
            n.category or "",
            to_local_naive(n.created_at).strftime("%Y-%m-%d %H:%M") if n.created_at else "",
        ]
        for n in notes
    ]
    if not note_rows:
        note_rows.append(["(لا توجد طلبيات/ملاحظات)", "", "", "", ""])
    story.append(_as_table(["النوع", "الوصف", "الشخص", "التصنيف", "تاريخ الإنشاء"], note_rows))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), rightMargin=8 * mm, leftMargin=8 * mm)
    doc.build(story)
    buf.seek(0)
    return buf
