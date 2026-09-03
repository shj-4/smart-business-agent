from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func
from dateutil import parser as date_parser
from app.database.models import Transaction, Task


def create_task(db: Session, telegram_user_id: int, data: dict, raw_message: str) -> Task:
    from app.timeutil import to_utc_naive

    description = data.get("description") or data.get("raw") or raw_message
    due_date_str = data.get("date")
    due_date = None
    if due_date_str:
        # Gemini يُرجع التواريخ بالتوقيت المحلي؛ نخزّنها كـ UTC للمقارنة الموحّدة
        try:
            due_date = datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
            if due_date.tzinfo is None:
                # بلا منطقة زمنية صريحة → نعتبرها بالتوقيت المحلي (فلسطين)
                due_date = to_utc_naive(due_date)
        except Exception:
            try:
                parsed = date_parser.parse(due_date_str)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=None)
                due_date = to_utc_naive(parsed)
            except Exception:
                due_date = None

    task = Task(
        telegram_user_id=telegram_user_id,
        description=description,
        due_date=due_date,
        person=data.get("person"),
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


def create_transaction(db: Session, telegram_user_id: int, data: dict, raw_message: str) -> Transaction:
    transaction = Transaction(
        telegram_user_id=telegram_user_id,
        type=data.get("type"),
        amount=data.get("amount"),
        currency=data.get("currency"),
        person=data.get("person"),
        description=data.get("description"),
        raw_message=raw_message,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    return transaction


def get_monthly_total(db: Session, telegram_user_id: int, transaction_type: str) -> dict:
    from app.timeutil import now_local, to_utc_naive
    from dateutil.relativedelta import relativedelta

    local_now = now_local()
    # حدود الشهر الميلادي حسب التوقيت المحلي (وليس UTC) لتفادي انزياح الشهر
    local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    local_end = (local_start + relativedelta(months=1))

    start_utc = to_utc_naive(local_start)
    end_utc = to_utc_naive(local_end)

    rows = (
        db.query(Transaction.currency, func.sum(Transaction.amount))
        .filter(
            Transaction.telegram_user_id == telegram_user_id,
            Transaction.type == transaction_type,
            Transaction.created_at >= start_utc,
            Transaction.created_at < end_utc,
        )
        .group_by(Transaction.currency)
        .all()
    )
    return {row[0] or "غير محددة": row[1] for row in rows}


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