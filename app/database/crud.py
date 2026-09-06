import re
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func
from dateutil import parser as date_parser
from app.database.models import Transaction, Task, Note

# تطبيع العملة نحو رموز ISO 4217 (مناسبة للشيقل والدولار في فلسطين)
CURRENCY_ALIASES = {
    "شيكل": "ILS", "الشيكل": "ILS", "شواكل": "ILS", "شيقل": "ILS",
    "شياقل": "ILS", "شيقلا": "ILS", "₪": "ILS", "nis": "ILS", "₪:": "ILS",
    "shekel": "ILS", "shekels": "ILS", "ils": "ILS",
    "دولار": "USD", "الدولار": "USD", "دولارات": "USD", "$": "USD",
    "usd": "USD", "dollar": "USD", "dollars": "USD",
    "دينار": "JOD", "الدينار": "JOD", "دنانير": "JOD", "jd": "JOD",
    "يورو": "EUR", "€": "EUR", "euro": "EUR", "eur": "EUR",
}


def normalize_currency(raw: str | None) -> str | None:
    """يحوّل أي صيغة عملة إلى رمز ISO موحّد (ILS لشيقل، USD لدولار...)."""
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "")
    if key in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[key]
    # تطابق جزئي (مثل "شيكل جديد", "دولار امريكي")
    for alias, code in CURRENCY_ALIASES.items():
        if alias in raw:
            return code
    return raw.strip() or None


MAX_DESCRIPTION_LEN = 500  # حد أقصى لطول النصوص الحرة (الوصف/الطلبية/الملاحظة)


def _clean_text(value) -> str | None:
    """ينظّف نصًا حرًا: يقلّص المسافات ويحدّ طوله (أو يعيد None)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:MAX_DESCRIPTION_LEN]


def _clean_person(value) -> str | None:
    """ينظّف حقل الشخص: نص فاضي → None (حتى لا يكسر فلترة person في الاستعلامات)."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _is_duplicate_message(db: Session, model, telegram_user_id: int, telegram_message_id: int | None) -> bool:
    """يتحقق هل تم تسجيل نفس الرسالة مسبقًا (idempotency)."""
    if telegram_message_id is None:
        return False
    return db.query(model).filter(
        model.telegram_user_id == telegram_user_id,
        model.telegram_message_id == telegram_message_id,
    ).first() is not None


def create_task(db: Session, telegram_user_id: int, data: dict, raw_message: str,
                telegram_message_id: int | None = None) -> Task | None:
    if _is_duplicate_message(db, Task, telegram_user_id, telegram_message_id):
        return None

    due_date = parse_date_local(data.get("date")) if data.get("date") else None

    task = Task(
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        description=_clean_text(data.get("description") or data.get("raw") or raw_message) or "مهمة",
        due_date=due_date,
        person=_clean_person(data.get("person")),
        status="pending",
        raw_message=raw_message,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _person_filter(person: str | None):
    """فلترة SQL حسب الشخص (بمطابقة جزئية تشبه المعاملات)."""
    if person:
        return Task.person.like(f"%{person}%")
    return None


def list_pending_tasks(db: Session, telegram_user_id: int, person: str | None = None):
    filters = [Task.telegram_user_id == telegram_user_id, Task.status == "pending"]
    pf = _person_filter(person)
    if pf is not None:
        filters.append(pf)
    return (
        db.query(Task)
        .filter(*filters)
        .order_by(Task.due_date.asc().nulls_last())
        .all()
    )


def list_overdue_tasks(db: Session, telegram_user_id: int, person: str | None = None):
    # تُرجع المهام المسجَّلة كمتأخرة (status == "overdue") — بعد أن
    # يقوم mark_overdue_tasks بتحديثها. (لا نعتمد على status == "pending"
    # لأنه لا يأتي بالنتائج بعد التحديث.)
    filters = [
        Task.telegram_user_id == telegram_user_id,
        Task.status == "overdue",
    ]
    pf = _person_filter(person)
    if pf is not None:
        filters.append(pf)
    return (
        db.query(Task)
        .filter(*filters)
        .order_by(Task.due_date.asc())
        .all()
    )


def mark_overdue_tasks(db: Session, telegram_user_id: int) -> int:
    from app.timeutil import now_utc

    now = now_utc()
    updated = db.query(Task).filter(
        Task.telegram_user_id == telegram_user_id,
        Task.status == "pending",
        Task.due_date != None,  # noqa: E711
        Task.due_date < now,
    ).update({"status": "overdue"})
    db.commit()
    return updated


def find_pending_task(db: Session, telegram_user_id: int, description_hint: str) -> Task | None:
    return (
        db.query(Task)
        .filter(
            Task.telegram_user_id == telegram_user_id,
            Task.status == "pending",
            Task.description.ilike(f"%{description_hint}%"),
        )
        .order_by(Task.created_at.desc())
        .first()
    )


def complete_task(db: Session, telegram_user_id: int, task_id: int) -> Task | None:
    task = db.query(Task).filter(
        Task.id == task_id,
        Task.telegram_user_id == telegram_user_id,
    ).first()
    if task and task.status == "pending":
        task.status = "done"
        db.commit()
        db.refresh(task)
        return task
    return None


def parse_date_local(date_str: str) -> datetime | None:
    """يحوّل نص تاريخ (صيغة ISO أو صيغة مرنة) إلى datetime بالتوقيت المحلي (UTC).

    يتوافق مع كل إصدارات بايثون: نجرب أولاً صيغة صريحة "YYYY-MM-DD HH:MM[:SS]"
    ثم dateutil المرن. القيمة الناتجة تُعتبر بالتوقيت المحلي وتُرجع كـ UTC.
    """
    from app.timeutil import to_utc_naive

    if not date_str:
        return None
    s = date_str.strip().replace("Z", "+00:00")
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(s, fmt)
            return to_utc_naive(parsed)
        except ValueError:
            continue
    # معالجة أي جزء زمني صريح (+00:00 إلخ)
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        try:
            parsed = date_parser.parse(s)
        except Exception:
            return None
    return to_utc_naive(parsed)


def create_transaction(db: Session, telegram_user_id: int, data: dict, raw_message: str,
                       telegram_message_id: int | None = None) -> Transaction | None:
    if _is_duplicate_message(db, Transaction, telegram_user_id, telegram_message_id):
        return None

    transaction = Transaction(
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        type=data.get("type"),
        amount=data.get("amount"),
        currency=normalize_currency(data.get("currency")),
        person=data.get("person"),
        description=data.get("description"),
        raw_message=raw_message,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    return transaction


def create_note(db: Session, telegram_user_id: int, data: dict, raw_message: str,
                telegram_message_id: int | None = None) -> Note | None:
    """يخزّن الطلبيات والملاحظات (order / note) بدل إضاعتها بصمت."""
    if _is_duplicate_message(db, Note, telegram_user_id, telegram_message_id):
        return None

    note = Note(
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        note_type=data.get("type"),  # order | note
        description=data.get("description") or data.get("raw") or raw_message,
        person=data.get("person"),
        raw_message=raw_message,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def get_period_range(period: str):
    from app.timeutil import now_local, to_utc_naive

    local_now = now_local()

    if period == "today":
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_week":
        local_start = local_now - timedelta(days=local_now.weekday())
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


def run_query(db: Session, telegram_user_id: int, query_details: dict) -> dict:
    from app.timeutil import to_local_naive

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
                "due_date": to_local_naive(t.due_date).strftime("%Y-%m-%d %H:%M") if t.due_date else None,
            }
            for t in tasks
        ]
        return {"metric": metric, "period": period, "person": person, "result": result, "kind": "list"}

    q = db.query(Transaction).filter(Transaction.telegram_user_id == telegram_user_id)

    if start:
        q = q.filter(Transaction.created_at >= start)
    if person:
        q = q.filter(Transaction.person.like(f"%{person}%"))

    if metric == "total_expenses":
        q = q.filter(Transaction.type == "expense")
        rows = q.with_entities(Transaction.currency, func.sum(Transaction.amount)).group_by(Transaction.currency).all()
        total = {row[0] or "غير محددة": row[1] for row in rows}
        return {"metric": metric, "period": period, "person": person, "result": total}

    elif metric == "total_income":
        q = q.filter(Transaction.type == "income")
        rows = q.with_entities(Transaction.currency, func.sum(Transaction.amount)).group_by(Transaction.currency).all()
        total = {row[0] or "غير محددة": row[1] for row in rows}
        return {"metric": metric, "period": period, "person": person, "result": total}

    elif metric == "count_transactions":
        count = q.count()
        return {"metric": metric, "period": period, "person": person, "result": count}

    else:
        return {"metric": metric, "period": period, "person": person, "result": None, "error": "unsupported_metric"}