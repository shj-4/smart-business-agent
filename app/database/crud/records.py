"""
العمليات المالية والملاحظات والاستعلام والتاريخ والتعديل والبحث (مع مسار الاستعلام المُجمَّع).
"""
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import timeutil as _timeutil
from app.audit import log_audit
from app.cache import get_or_set
from app.config import settings
from app.database.models import (
    Note,
    Task,
    Transaction,
)
from app.money import _best_effort_base_amount, _sum_amounts_by_currency, _unified_totals_for_rows
from app.timeutil import now_utc, to_local_naive, to_utc_naive


def create_transaction(
    db: Session,
    telegram_user_id: int,
    data: dict,
    raw_message: str,
    telegram_message_id: int | None = None,
) -> Transaction | None:
    from app.database.crud import (
        _clean_person,
        _clean_text,
        _invalidate_caches,
        _is_duplicate_message,
        _to_decimal,
        normalize_currency,
    )

    if _is_duplicate_message(db, Transaction, telegram_user_id, telegram_message_id):
        return None

    amount = _to_decimal(data.get("amount"))
    currency = normalize_currency(data.get("currency"))
    base_amount, base_at = _best_effort_base_amount(amount, currency)

    vat_rate = _to_decimal(data.get("vat_rate"))
    vat_amount = _to_decimal(data.get("vat_amount"))
    if vat_rate is not None and (vat_rate < 0 or vat_rate > 1000):
        vat_rate = None
    if vat_amount is not None and vat_amount < 0:
        vat_amount = None

    transaction = Transaction(
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        type=data.get("type"),
        amount=amount,
        currency=currency,
        person=_clean_person(data.get("person")),
        category=_clean_text(data.get("category")),
        description=_clean_text(data.get("description")),
        raw_message=raw_message,
        amount_in_base_currency=base_amount,
        base_currency_at_creation=base_at,
        vat_rate=vat_rate.quantize(Decimal("0.001")) if vat_rate is not None else None,
        vat_amount=vat_amount.quantize(Decimal("0.01")) if vat_amount is not None else None,
    )
    db.add(transaction)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(transaction)
    _invalidate_caches(db, telegram_user_id)
    return transaction

def create_note(
    db: Session,
    telegram_user_id: int,
    data: dict,
    raw_message: str,
    telegram_message_id: int | None = None,
) -> Note | None:
    """يخزّن الطلبيات والملاحظات (order / note) بدل إضاعتها بصمت."""
    from app.database.crud import (
        _clean_person,
        _clean_text,
        _invalidate_caches,
        _is_duplicate_message,
    )

    if _is_duplicate_message(db, Note, telegram_user_id, telegram_message_id):
        return None

    note = Note(
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        note_type=data.get("type"),  # order | note
        description=_clean_text(data.get("description") or data.get("raw") or raw_message)
        or "ملاحظة",
        person=_clean_person(data.get("person")),
        category=_clean_text(data.get("category")),
        status="open" if data.get("type") == "order" else None,
        raw_message=raw_message,
    )
    db.add(note)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(note)
    _invalidate_caches(db, telegram_user_id)
    return note

def soft_delete_last(db: Session, model, telegram_user_id: int) -> object | None:
    """يرجّع آخر سجل غير محذوف للمستخدم، أو None. (لأمر /undo)"""
    return (
        db.query(model)
        .filter(
            model.telegram_user_id == telegram_user_id,
            model.deleted_at.is_(None),
        )
        .order_by(model.id.desc())
        .first()
    )

def get_period_range(period: str):
    local_now = _timeutil.now_local()

    if period == "today":
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_week":
        # الأسبوع يبدأ من أول يوم قابل للتكوين (افتراضيًا الأحد لفلسطين/السياق العربي)
        fd = _timeutil.first_day_of_week()
        weekday = local_now.weekday()
        local_start = local_now - timedelta(days=(weekday - fd) % 7)
        local_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_month":
        local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_year":
        local_start = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # all_time
        local_start = None

    # القيم المخزنة بصيغة UTC → نحوّل حدود الفترة المحلية إلى UTC للمقارنة الصحيحة
    start = to_utc_naive(local_start) if local_start is not None else None
    return start, None

def get_comparison_ranges(period: str) -> dict:
    """حدود الفترة الحالية والسابقة (بصيغة UTC naive) لمقارنة فترات.

    example: period="this_month" → current=[أول الشهر حتى الآن]،
    previous=[أول الشهر الماضي حتى أول الشهر الحالي].
    يعيد dict: {"current": (start, end), "previous": (start, end)}.
    """
    local_now = _timeutil.now_local()

    def _bounds(local_start_dt, local_end_dt):
        return (
            to_utc_naive(local_start_dt.replace(hour=0, minute=0, second=0, microsecond=0)),
            to_utc_naive(local_end_dt),
        )

    if period == "today":
        cur_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_start = cur_start - timedelta(days=1)
        return {
            "current": _bounds(cur_start, local_now),
            "previous": _bounds(prev_start, cur_start),
        }

    if period == "this_week":
        fd = _timeutil.first_day_of_week()
        weekday = local_now.weekday()
        cur_start = local_now - timedelta(days=(weekday - fd) % 7)
        cur_start = cur_start.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_start = cur_start - timedelta(days=7)
        return {
            "current": _bounds(cur_start, local_now),
            "previous": _bounds(prev_start, cur_start),
        }

    if period == "this_month":
        cur_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # نهاية الشهر السابق = بداية الشهر الحالي
        if cur_start.month == 1:
            prev_start = cur_start.replace(year=cur_start.year - 1, month=12)
        else:
            prev_start = cur_start.replace(month=cur_start.month - 1)
        return {
            "current": _bounds(cur_start, local_now),
            "previous": _bounds(prev_start, cur_start),
        }

    if period == "this_year":
        cur_start = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_start = cur_start.replace(year=cur_start.year - 1)
        return {
            "current": _bounds(cur_start, local_now),
            "previous": _bounds(prev_start, cur_start),
        }

    # all_time أو غير معروف: لا مقارنة
    return None

def run_query(db: Session, telegram_user_id: int, query_details: dict) -> dict:
    """تنفيذ استعلام مع تخزين مؤقت قصير (TTL) للاستعلامات التجميعية المتكررة.

    نفس المستخدم يسأل "كم صرفت هذا الشهر" مرارًا خلال الدقيقة → نُعيد النتيجة
    المخزنة بدل إعادة الجمع في Python. تُمسح ذاكرة المستخدم عند أي كتابة.
    """
    from app.database.crud import _run_query_uncached
    metric = query_details.get("metric")
    period = query_details.get("period") or "all_time"
    person = query_details.get("person")

    # استعلامات المهام تُبقي دائمًا قراءة حية (تواريخ الاستحقاق تتغير كل لحظة)
    if metric in ("list_tasks", "list_overdue_tasks"):
        return _run_query_uncached(db, telegram_user_id, query_details)

    namespace = "run_query"
    key = f"{namespace}:{telegram_user_id}:{metric}:{period}:{person}"
    return get_or_set(key, lambda: _run_query_uncached(db, telegram_user_id, query_details))

def _run_query_uncached(db: Session, telegram_user_id: int, query_details: dict) -> dict:
    from app.database.crud import (
        accessible_user_ids,
        list_overdue_tasks,
        list_pending_tasks,
        mark_overdue_tasks,
    )


    metric = query_details.get("metric")
    period = query_details.get("period") or "all_time"
    person = query_details.get("person")

    start, end = get_period_range(period)

    # معالجة الاستعلام عن المهام
    if metric in ("list_tasks", "list_overdue_tasks"):
        mark_overdue_tasks(db, telegram_user_id)
        if metric == "list_tasks":
            tasks = list_pending_tasks(db, telegram_user_id, person)
        else:
            tasks = list_overdue_tasks(db, telegram_user_id, person)
        result = [
            {
                "id": t.id,
                "description": t.description,
                "person": t.person,
                "status": t.status,
                # معروض بالتوقيت المحلي (قيم المخزن UTC)
                "due_date": to_local_naive(t.due_date).strftime("%Y-%m-%d %H:%M")
                if t.due_date
                else None,
            }
            for t in tasks
        ]
        return {
            "metric": metric,
            "period": period,
            "person": person,
            "result": result,
            "kind": "list",
        }

    q = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
        Transaction.deleted_at.is_(None),
    )

    if start:
        q = q.filter(Transaction.created_at >= start)
    if person:
        q = q.filter(Transaction.person.like(f"%{person}%"))

    if metric == "total_expenses":
        expense_rows = q.filter(Transaction.type == "expense").all()
        total = _sum_amounts_by_currency(expense_rows)
        return {
            "metric": metric,
            "period": period,
            "person": person,
            "result": total,
            "unified_total": _unified_totals_for_rows(expense_rows, settings.base_currency),
        }

    elif metric == "total_income":
        income_rows = q.filter(Transaction.type == "income").all()
        total = _sum_amounts_by_currency(income_rows)
        return {
            "metric": metric,
            "period": period,
            "person": person,
            "result": total,
            "unified_total": _unified_totals_for_rows(income_rows, settings.base_currency),
        }

    elif metric == "count_transactions":
        count = q.count()
        return {"metric": metric, "period": period, "person": person, "result": count}

    elif metric == "person_balance":
        # رصيد مستحق مع شخص معيّن = إجمالي ما استلمتُه منه (income) - إجمالي ما دفعتُه له (expense)
        if not person:
            return {
                "metric": metric,
                "period": period,
                "person": person,
                "result": None,
                "error": "no_person",
            }
        q_income = db.query(Transaction).filter(
            Transaction.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Transaction.deleted_at.is_(None),
            Transaction.type == "income",
            Transaction.person.like(f"%{person}%"),
        )
        q_expense = db.query(Transaction).filter(
            Transaction.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Transaction.deleted_at.is_(None),
            Transaction.type == "expense",
            Transaction.person.like(f"%{person}%"),
        )
        if start:
            q_income = q_income.filter(Transaction.created_at >= start)
            q_expense = q_expense.filter(Transaction.created_at >= start)

        income_rows = q_income.all()
        expense_rows = q_expense.all()

        income_by_cur = _sum_amounts_by_currency(income_rows)
        expense_by_cur = _sum_amounts_by_currency(expense_rows)
        all_currencies = set(income_by_cur) | set(expense_by_cur)

        balance = {}
        for cur in all_currencies:
            inc = income_by_cur.get(cur, Decimal("0"))
            exp = expense_by_cur.get(cur, Decimal("0"))
            balance[cur] = {
                "income": inc,
                "expense": exp,
                "balance": (inc - exp).quantize(Decimal("0.01")),
            }
        return {
            "metric": metric,
            "period": period,
            "person": person,
            "result": balance,
            "kind": "balance",
        }

    elif metric == "compare_periods":
        ranges = get_comparison_ranges(period)
        if not ranges or ranges["current"][0] is None:
            return {
                "metric": metric,
                "period": period,
                "person": person,
                "result": None,
                "error": "unsupported_period",
            }

        def _totals_by_currency(lo, hi, tx_type):
            qq = db.query(Transaction).filter(
                Transaction.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
                Transaction.deleted_at.is_(None),
                Transaction.type == tx_type,
                Transaction.created_at >= lo,
                Transaction.created_at < hi,
            )
            if person:
                qq = qq.filter(Transaction.person.like(f"%{person}%"))
            return _sum_amounts_by_currency(qq.all())

        cur_lo, cur_hi = ranges["current"]
        prev_lo, prev_hi = ranges["previous"]
        result = {
            "current": {
                "expense": _totals_by_currency(cur_lo, cur_hi, "expense"),
                "income": _totals_by_currency(cur_lo, cur_hi, "income"),
            },
            "previous": {
                "expense": _totals_by_currency(prev_lo, prev_hi, "expense"),
                "income": _totals_by_currency(prev_lo, prev_hi, "income"),
            },
        }
        return {
            "metric": metric,
            "period": period,
            "person": person,
            "result": result,
            "kind": "comparison",
        }

    else:
        return {
            "metric": metric,
            "period": period,
            "person": person,
            "result": None,
            "error": "unsupported_metric",
        }

def undo_last_record(db: Session, telegram_user_id: int) -> dict | None:
    """تراجع/يحذف (soft delete) آخر سجل أضافه المستخدم (عبر /undo).

    يفحص الجداول الثلاثة (معاملات/مهام/طلبيات&ملاحظات)، يختار الأحدث
    ويثبّت deleted_at عليه — فيختفي من كل الاستعلامات لكن يبقى في DB.

    أمن المساحة: أعضاء عاديون لا يتراجعون (المرتكز أو الأفراد فقط).
    """
    from app.database.crud import _invalidate_caches, can_manage_records, soft_delete_last


    if not can_manage_records(db, telegram_user_id):
        log_audit(telegram_user_id, "denied_undo", "workspace_role")
        return None

    candidates = []
    for model in (Transaction, Task, Note):
        row = soft_delete_last(db, model, telegram_user_id)
        if row is not None:
            candidates.append((row.created_at, model, row))

    if not candidates:
        return None

    # الأحدث إطلاقًا
    _, model, row = max(candidates, key=lambda c: c[0])

    row.deleted_at = now_utc()
    db.commit()
    db.refresh(row)

    if model is Transaction:
        kind = "معاملة"
        label = (row.description or "")[:60]
    elif model is Note:
        kind = "طلبية/ملاحظة"
        label = (row.description or "")[:60]
    else:
        kind = "مهمة"
        label = (row.description or "")[:60]

    log_audit(
        telegram_user_id, "soft_delete", f"{model.__name__}:{row.id}", detail=(label or "")[:80]
    )
    _invalidate_caches(db, telegram_user_id)
    return {"kind": kind, "label": label}

def restore_last_deleted(db: Session, telegram_user_id: int) -> dict | None:
    """يستعيد أحدث سجل محذوف (soft-delete) — أمر /redo (عكس /undo).

    يبحث عن أحدث deleted_at بين المعاملات/المهام/الملاحظات المحذوفة في نطاق
    المستخدم ويزيله — فيعود السجل للظهور في كل الاستعلامات.
    """
    from app.database.crud import _invalidate_caches, accessible_user_ids, can_manage_records


    if not can_manage_records(db, telegram_user_id):
        log_audit(telegram_user_id, "denied_redo", "workspace_role")
        return None

    accessible = accessible_user_ids(db, telegram_user_id)
    candidates = []
    for model in (Transaction, Task, Note):
        row = (
            db.query(model)
            .filter(
                model.telegram_user_id.in_(accessible),
                model.deleted_at.isnot(None),
            )
            .order_by(model.deleted_at.desc())
            .first()
        )
        if row is not None:
            candidates.append((row.deleted_at, model, row))

    if not candidates:
        return None

    _, model, row = max(candidates, key=lambda c: c[0])
    row.deleted_at = None
    db.commit()
    db.refresh(row)

    if model is Transaction:
        kind = "معاملة"
    elif model is Note:
        kind = "طلبية/ملاحظة"
    else:
        kind = "مهمة"
    label = (row.description or "")[:60]

    log_audit(
        telegram_user_id, "restore_record", f"{model.__name__}:{row.id}", detail=(label or "")[:80]
    )
    _invalidate_caches(db, telegram_user_id)
    return {"kind": kind, "label": label}

def list_recent_records(db: Session, telegram_user_id: int, limit: int = 10) -> list[dict]:
    """يعرض آخر سجلات المستخدم (معاملات + مهام + ملاحظات) مرتبة بالأحدث."""
    from app.database.crud import accessible_user_ids


    candidates = []
    for model in (Transaction, Task, Note):
        rows = (
            db.query(model)
            .filter(
                model.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
                model.deleted_at.is_(None),
            )
            .order_by(model.created_at.desc())
            .limit(limit)
            .all()
        )
        for r in rows:
            local_dt = to_local_naive(r.created_at)
            date_str = local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else ""

            if model is Transaction:
                kind = "expense" if r.type == "expense" else "income"
                label = f"{r.description or ''}"
                if r.amount:
                    label = f"{r.amount} {r.currency or ''} - {label}"
                elif not label:
                    label = "(بدون وصف)"
                extra = r.person or ""
            elif model is Task:
                kind = "task"
                label = r.description or "(بدون وصف)"
                extra = r.person or ""
            else:
                kind = r.note_type or "note"
                label = r.description or "(بدون وصف)"
                extra = r.person or ""

            entry = {
                "id": r.id,
                "model": model.__name__,
                "kind": kind,
                "label": label[:80],
                "person": extra,
                "date": date_str,
                "created_at": r.created_at,
            }
            if model is Transaction:
                entry["vat_rate"] = r.vat_rate
                entry["vat_amount"] = r.vat_amount
            elif getattr(r, "note_type", None) == "order":
                entry["status"] = r.status or "open"
            candidates.append(entry)

    candidates.sort(key=lambda c: c["created_at"], reverse=True)
    return candidates[:limit]

def get_record_by_id(db: Session, telegram_user_id: int, model_name: str, record_id: int):
    """يجلب سجلًا محددًا بالـ ID والنوع وملكية المستخدم."""
    from app.database.crud import accessible_user_ids

    model_map = {"Transaction": Transaction, "Task": Task, "Note": Note}
    model = model_map.get(model_name)
    if model is None:
        return None, None
    row = (
        db.query(model)
        .filter(
            model.id == record_id,
            model.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            model.deleted_at.is_(None),
        )
        .first()
    )
    return row, model

def delete_record_by_id(
    db: Session, telegram_user_id: int, model_name: str, record_id: int
) -> str | None:
    """Soft-delete لسجل محدد بنوعه (معاملة/مهمة/ملاحظة) بضوابط الأدوار.

    الأفراد يمسحون سجلاتهم؛ أعضاء المساحة المشتركة يمسحها المرتكز (المالك) فقط —
    وأي رفض يُسجَّل في سجل التدقيق. ترجع تسمية السجل المحذوف أو None.
    """
    from app.database.crud import _invalidate_caches, can_manage_records, get_record_by_id


    if not can_manage_records(db, telegram_user_id):
        log_audit(
            telegram_user_id,
            "denied_record_delete",
            f"{model_name}:{record_id}",
            detail="عضو في مساحة مشتركة وليس المرتكز",
        )
        return None
    row, _ = get_record_by_id(db, telegram_user_id, model_name, record_id)
    if row is None:
        return None
    label = row.description or getattr(row, "amount", None) or "(بدون وصف)"
    row.deleted_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    log_audit(
        telegram_user_id,
        "record_delete",
        f"{model_name}:{record_id}",
        detail=str(label)[:80],
    )
    _invalidate_caches(db, telegram_user_id)
    return str(label)

def _search_row_result(r, model) -> dict:
    """يبني عنصر نتيجة بحث من سجل مطابق (النص المشفّر يُقرأ هنا فقط عند الحاجة)."""

    local_dt = to_local_naive(r.created_at)
    model_name = model.__name__
    if model is Transaction:
        kind = "expense" if r.type == "expense" else "income"
        label = f"{r.amount} {r.currency or ''} - {r.description or ''}".strip(" -")
        extra = r.person or ""
    elif model is Task:
        kind = "task"
        label = r.description or "(بدون وصف)"
        extra = r.person or ""
    else:
        kind = r.note_type or "note"
        label = r.description or "(بدون وصف)"
        extra = r.person or ""
    return {
        "id": r.id,
        "model": model_name,
        "kind": kind,
        "label": label[:80],
        "person": extra,
        "date": local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else "",
        "created_at": r.created_at,
        "vat_rate": r.vat_rate if model is Transaction else None,
        "vat_amount": r.vat_amount if model is Transaction else None,
        "status": (r.status or "open")
        if getattr(r, "note_type", None) == "order"
        else None,
    }

def search_records(
    db: Session, telegram_user_id: int, term: str, limit: int = 30, return_meta: bool = False
) -> list[dict] | tuple[list[dict], dict]:
    """بحث نصي بسيط في آخر سجلات المستخدم (شخص/تصنيف/وصف/قيمة/نوع).

    الوصف وقيم المبالغ مشفّرة (EncryptedString/EncryptedNumeric) فلا تُطابَق في
    SQL. لذلك يُدفع النص العادي (person/category وتسمية النوع) أولًا إلى SQL
    عبر LIKE بعد تهريب محارف النمط، فتعود النتيجة الأشيع بلا فك تشفير؛ ولا
    يُنزل إلى فك تشفير الوصف ومطابقته في Python إلا عندما تقصر النتائج عن
    الحد المطلوب (نافذة الأحدث نفسها التي كانت تُفحص سابقًا بالكامل).

    يعيد نفس بنية list_recent_records ليعاد استخدامها في عرض قوائم الأزرار.

    مع return_meta=True يعيد (نتائج, meta) حيث meta يحتوي per_model=حجم النافذة
    وsaturated=True إن امتلأت النافذة أو أوقفنا البحث عند بلوغ الحد (قد توجد
    سجلات أقدم/مطابقة أخرى لم تُفحص) — ليُبلَّغ المستخدم أن البحث جزئي.
    """
    from app.database.crud import _like_escape, accessible_user_ids

    needle = (term or "").strip().lower()
    if not needle:
        return ([], {"per_model": 0, "saturated": False}) if return_meta else []

    accessible = accessible_user_ids(db, telegram_user_id)
    per_model = max(limit * 2, 100)
    saturated = False
    kinds = {
        "expense": "مصروف",
        "income": "إيراد",
        "task": "مهمة",
        "order": "طلبية",
        "note": "ملاحظة",
    }
    pattern = f"%{_like_escape(needle)}%"
    matched = set()  # (اسم النموذج, id) — منع الازدواج بين مرحلتَي الاستعلام
    matches: list[dict] = []
    models = (Transaction, Task, Note)

    def _plain_conds(model) -> list:
        conds = []
        for attr in ("person", "category"):
            col = getattr(model, attr, None)
            if col is not None:
                conds.append(func.lower(col).like(pattern, escape="\\"))
        label_col = getattr(model, "note_type", None) or getattr(model, "type", None)
        if label_col is not None:
            for code, label in kinds.items():
                if needle in label:
                    conds.append(label_col == code)
        return conds

    # 1) مطابقة النص العادي داخل نافذة الأحدث — بلا فك تشفير
    for model in models:
        conds = _plain_conds(model)
        if not conds:
            continue
        rows = (
            db.query(model)
            .filter(
                model.telegram_user_id.in_(accessible),
                model.deleted_at.is_(None),
                or_(*conds),
            )
            .order_by(model.created_at.desc())
            .limit(per_model)
            .all()
        )
        if len(rows) == per_model:
            saturated = True
        for r in rows:
            matched.add((model.__name__, r.id))
            matches.append(_search_row_result(r, model))

    # 2) عند الحاجة فقط: فحص نافذة الأحدث في Python للمطابقة على الحقول المشفّرة
    if len(matches) < limit:
        for model in models:
            rows = (
                db.query(model)
                .filter(
                    model.telegram_user_id.in_(accessible),
                    model.deleted_at.is_(None),
                )
                .order_by(model.created_at.desc())
                .limit(per_model)
                .all()
            )
            if len(rows) == per_model:
                saturated = True
            for r in rows:
                if (model.__name__, r.id) in matched:
                    continue
                haystack_parts = [
                    (r.person or ""),
                    (getattr(r, "category", "") or ""),
                    (str(getattr(r, "amount", "") or "")),
                    (r.description or ""),
                    (r.raw_message or ""),
                ]
                if getattr(r, "note_type", None):
                    haystack_parts.append(kinds.get(r.note_type, r.note_type))
                if getattr(r, "type", None) in kinds:
                    haystack_parts.append(kinds[r.type])
                if any(needle in (part or "").lower() for part in haystack_parts):
                    matches.append(_search_row_result(r, model))
    else:
        # بلوغ الحد عبر النص العادي دون مسح الوصف المشفّر → النتائج جزئية
        saturated = True

    matches.sort(key=lambda c: c["created_at"], reverse=True)
    result = matches[:limit]
    if return_meta:
        return result, {"per_model": per_model, "saturated": saturated}
    return result

def update_transaction(db: Session, row: Transaction, fields: dict) -> Transaction:
    """يحدّث حقول محددة في معاملة مالية."""
    from app.database.crud import (
        _clean_person,
        _clean_text,
        _invalidate_caches,
        _to_decimal,
        normalize_currency,
    )


    old = {
        key: getattr(row, key, None)
        for key in ("amount", "currency", "person", "category", "description")
    }
    for key in ("amount", "currency", "person", "category", "description"):
        if key in fields and fields[key] is not None:
            if key == "amount":
                row.amount = _to_decimal(fields[key])
            elif key == "currency":
                row.currency = normalize_currency(fields[key]) or fields[key]
            elif key == "category":
                row.category = _clean_text(fields[key])
            elif key == "person":
                row.person = _clean_person(fields[key])
            elif key == "description":
                row.description = _clean_text(fields[key])
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    changes = {
        k: {"old": str(old[k])[:80], "new": str(getattr(row, k))[:80]}
        for k in old
        if getattr(row, k, None) != old[k]
    }
    log_audit(row.telegram_user_id, "update", f"transaction:{row.id}", detail=f"changes={changes}")
    _invalidate_caches(db, row.telegram_user_id)
    return row

def update_note(db: Session, row: Note, fields: dict) -> Note:
    """يحدّث حقول محددة في ملاحظة/طلبية."""
    from app.database.crud import _clean_person, _clean_text, _invalidate_caches


    old = {key: getattr(row, key, None) for key in ("description", "category", "person")}
    for key in ("description", "category", "person"):
        if key in fields and fields[key] is not None:
            if key == "category":
                row.category = _clean_text(fields[key])
            elif key == "person":
                row.person = _clean_person(fields[key])
            elif key == "description":
                row.description = _clean_text(fields[key]) or row.description
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    changes = {
        k: {"old": str(old[k])[:80], "new": str(getattr(row, k))[:80]}
        for k in old
        if str(getattr(row, k, None)) != str(old[k])
    }
    log_audit(row.telegram_user_id, "update", f"note:{row.id}", detail=f"changes={changes}")
    _invalidate_caches(db, row.telegram_user_id)
    return row
