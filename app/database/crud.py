from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func
from dateutil import parser as date_parser
from app.database.models import Transaction, Task


def create_task(db: Session, telegram_user_id: int, data: dict, raw_message: str) -> Task:
    description = data.get("description") or data.get("raw") or raw_message
    due_date_str = data.get("date")
    due_date = None
    if due_date_str:
        # GitHub: أولاً جرب ISO، ثم dateutil المرن
        try:
            due_date = datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
            # ننشئ datetime بدون tz لتوحيد المقارنة
            if due_date.tzinfo is not None:
                due_date = due_date.replace(tzinfo=None)
        except Exception:
            try:
                due_date = date_parser.parse(due_date_str)
                if due_date.tzinfo is not None:
                    due_date = due_date.replace(tzinfo=None)
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


def list_pending_tasks(db: Session, telegram_user_id: int):
    return (
        db.query(Task)
        .filter(Task.telegram_user_id == telegram_user_id, Task.status == "pending")
        .order_by(Task.due_date.asc().nulls_last())
        .all()
    )


def list_overdue_tasks(db: Session, telegram_user_id: int):
    now = datetime.utcnow()
    return (
        db.query(Task)
        .filter(
            Task.telegram_user_id == telegram_user_id,
            Task.status == "pending",
            Task.due_date != None,  # noqa: E711
            Task.due_date < now,
        )
        .order_by(Task.due_date.asc())
        .all()
    )


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


def get_monthly_total(db: Session, telegram_user_id: int, transaction_type: str) -> float:
    from datetime import datetime
    from sqlalchemy import func, extract

    now = datetime.utcnow()
    total = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.telegram_user_id == telegram_user_id,
            Transaction.type == transaction_type,
            extract("year", Transaction.created_at) == now.year,
            extract("month", Transaction.created_at) == now.month,
        )
        .scalar()
    )
    return total or 0.0


def get_period_range(period: str):
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # all_time
        start = None
    return start, now


def run_query(db: Session, telegram_user_id: int, query_details: dict) -> dict:
    metric = query_details.get("metric")
    period = query_details.get("period") or "all_time"
    person = query_details.get("person")

    start, end = get_period_range(period)

    # معالجة الاستعلام عن المهام
    if metric in ("list_tasks", "list_overdue_tasks"):
        if metric == "list_tasks":
            tasks = list_pending_tasks(db, telegram_user_id)
        else:
            tasks = list_overdue_tasks(db, telegram_user_id)
        if person:
            tasks = [t for t in tasks if t.person and person in t.person]
        result = [
            {
                "id": t.id,
                "description": t.description,
                "person": t.person,
                "status": t.status,
                "due_date": t.due_date.strftime("%Y-%m-%d %H:%M") if t.due_date else None,
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
        total = q.with_entities(func.sum(Transaction.amount)).scalar() or 0
        return {"metric": metric, "period": period, "person": person, "result": total}

    elif metric == "total_income":
        q = q.filter(Transaction.type == "income")
        total = q.with_entities(func.sum(Transaction.amount)).scalar() or 0
        return {"metric": metric, "period": period, "person": person, "result": total}

    elif metric == "count_transactions":
        count = q.count()
        return {"metric": metric, "period": period, "person": person, "result": count}

    else:
        return {"metric": metric, "period": period, "person": person, "result": None, "error": "unsupported_metric"}