"""
المهام: إنشاء/استعلام/استحقاق/تكرار، وترتيب حسب الأولوية (LIMIT مُقنَّن).
"""
import calendar
from datetime import datetime, timedelta

from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import log_audit
from app.database.crud.company import company_filter
from app.database.models import (
    Task,
)
from app.normalize import normalize_priority, normalize_recurrence
from app.timeutil import now_utc, to_local_naive, to_utc_naive

_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}

_MAX_DT = datetime.max

_TASK_FETCH_CAP = 500

def _normalize_priority(value) -> str:
    """يقنن قيمة الأولوية إلى high|normal|low (الافتراضي normal)."""
    return normalize_priority(value)

def _normalize_recurrence(value) -> str | None:
    """يقنن قيمة التكرار إلى daily|weekly|monthly أو None."""
    return normalize_recurrence(value)

def create_task(
    db: Session,
    telegram_user_id: int,
    data: dict,
    raw_message: str,
    telegram_message_id: int | None = None,
) -> Task | None:
    from app.database.crud import (
        _clean_person,
        _clean_text,
        _invalidate_caches,
        _is_duplicate_message,
        parse_date_local,
    )

    if _is_duplicate_message(db, Task, telegram_user_id, telegram_message_id):
        return None

    due_date = parse_date_local(data.get("date")) if data.get("date") else None

    from app.database.crud.company import company_id_for_user as _cid_for

    _cid = _cid_for(db, telegram_user_id)
    task = Task(
        telegram_user_id=telegram_user_id,
        company_id=_cid,
        telegram_message_id=telegram_message_id,
        description=_clean_text(data.get("description") or data.get("raw") or raw_message)
        or "مهمة",
        due_date=due_date,
        person=_clean_person(data.get("person")),
        priority=_normalize_priority(data.get("priority")),
        recurrence_rule=_normalize_recurrence(data.get("recurrence")),
        status="pending",
        raw_message=raw_message,
    )
    db.add(task)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(task)
    _invalidate_caches(db, telegram_user_id)
    return task

def _person_filter(person: str | None):
    """فلترة SQL حسب الشخص (مطابقة تامة — لا خلط بأسماء تبدأ بنفس الكلمة)."""
    if person:
        return Task.person == person
    return None

def _priority_sort(tasks: list[Task]) -> list[Task]:
    """يرتب حسب الأولوية (عالية أولًا) ثم الموعد ثم الأحدث — الترتيب يكون في
    Python لأن الوصف مشفّر ولا يمكن الاعتماد على SQL لفرز الأولوية والموعد معًا."""
    from app.database.crud import _MAX_DT, _PRIORITY_RANK
    return sorted(
        tasks,
        key=lambda t: (
            _PRIORITY_RANK.get(t.priority or "normal", 1),
            (t.due_date or _MAX_DT),
            -(int(t.created_at.timestamp()) if t.created_at else 0),
        ),
    )

def _task_order_by():
    """ترتيب SQL مطابق لمقارنة _priority_sort (أولوية ثم موعد مع NULL أخيرًا ثم
    الأحدث) — يضمن أن نافذة `cap` التي تُجلب تُحتوي بالفعل على أفضل المرشحين
    دون تغيير النتيجة النهائية (عدا التعادل غير الحاسم)."""
    return [
        case(
            (Task.priority == "high", 0),
            (Task.priority == "low", 2),
            else_=1,
        ).asc(),
        (Task.due_date.is_(None)).asc(),
        Task.due_date.asc(),
        Task.created_at.desc(),
    ]

def list_pending_tasks(
    db: Session, telegram_user_id: int, person: str | None = None, limit: int = 50
):
    from app.database.crud import (
        _TASK_FETCH_CAP,
        _person_filter,
        _priority_sort,
        _task_order_by,
        accessible_user_ids,
    )

    filters = [
        company_filter(db, telegram_user_id, Task),
        Task.status == "pending",
        Task.deleted_at.is_(None),
    ]
    pf = _person_filter(person)
    if pf is not None:
        filters.append(pf)
    tasks = (
        db.query(Task)
        .filter(*filters)
        .order_by(*_task_order_by())
        .limit(max(limit, _TASK_FETCH_CAP))
        .all()
    )
    return _priority_sort(tasks)[:limit]

def list_overdue_tasks(
    db: Session, telegram_user_id: int, person: str | None = None, limit: int = 50
):
    # تُرجع المهام المسجَّلة كمتأخرة (status == "overdue") — بعد أن
    # يقوم mark_overdue_tasks بتحديثها. (لا نعتمد على status == "pending"
    # لأنه لا يأتي بالنتائج بعد التحديث.)
    from app.database.crud import (
        _TASK_FETCH_CAP,
        _person_filter,
        _priority_sort,
        _task_order_by,
        accessible_user_ids,
    )

    filters = [
        company_filter(db, telegram_user_id, Task),
        Task.status == "overdue",
        Task.deleted_at.is_(None),
    ]
    pf = _person_filter(person)
    if pf is not None:
        filters.append(pf)
    tasks = (
        db.query(Task)
        .filter(*filters)
        .order_by(*_task_order_by())
        .limit(max(limit, _TASK_FETCH_CAP))
        .all()
    )
    return _priority_sort(tasks)[:limit]

def mark_overdue_tasks(db: Session, telegram_user_id: int) -> int:
    from app.database.crud import accessible_user_ids


    now = now_utc()
    updated = (
        db.query(Task)
        .filter(
            company_filter(db, telegram_user_id, Task),
            Task.status == "pending",
            Task.deleted_at.is_(None),
            Task.due_date != None,  # noqa: E711
            Task.due_date < now,
        )
        .update({"status": "overdue"})
    )
    db.commit()
    return updated

def list_done_tasks(db: Session, telegram_user_id: int, person: str | None = None, limit: int = 50):
    """المهام المنجزة (للتصفح عبر القوائم) — الأحدث أولًا."""
    from app.database.crud import _person_filter, accessible_user_ids

    filters = [
        company_filter(db, telegram_user_id, Task),
        Task.status == "done",
        Task.deleted_at.is_(None),
    ]
    pf = _person_filter(person)
    if pf is not None:
        filters.append(pf)
    return db.query(Task).filter(*filters).order_by(Task.created_at.desc()).limit(limit).all()

def delete_task_by_id(db: Session, telegram_user_id: int, task_id: int) -> Task | None:
    """حذف مهمة محددة (soft delete) — يُستخدم من أزرار قائمة المهام.

    أمن المساحة: أعضاء عاديون لا يحذفون (المرتكز أو الأفراد فقط).
    """
    from app.database.crud import _invalidate_caches, accessible_user_ids, can_manage_records


    if not can_manage_records(db, telegram_user_id):
        log_audit(telegram_user_id, "denied_task_delete", f"task:{task_id}")
        return None

    task = (
        db.query(Task)
        .filter(
            Task.id == task_id,
            company_filter(db, telegram_user_id, Task),
            Task.deleted_at.is_(None),
        )
        .first()
    )
    if task and task.status in ("pending", "overdue"):
        task.deleted_at = now_utc()
        db.commit()
        db.refresh(task)
        log_audit(telegram_user_id, "delete_task", f"task:{task_id}", (task.description or "")[:80])
        _invalidate_caches(db, telegram_user_id)
        return task
    return None

def find_pending_task(db: Session, telegram_user_id: int, description_hint: str) -> Task | None:
    """يبحث عن مهمة معلّقة يطابق وصفها الوصف المقدّم (مطابقة جزئية غير حساسة للحالة).

    لا يمكن استخدام SQL LIKE لأن الوصف مشفّر — نجلب المهام ونطابق في Python.
    يُحدّ الجلب بـ _TASK_FETCH_CAP مثل list_pending_tasks/list_overdue_tasks حتى لا
    يُفك تشفير كامل جدول المهام نصيًا لكل /done أو intent="complete_task"؛ المطابقة
    خارج النافذة تُرفض (يحتاج المستخدم وصفًا أدق).
    """
    from app.database.crud import accessible_user_ids


    hint = description_hint.strip().lower()
    tasks = (
        db.query(Task)
        .filter(
            company_filter(db, telegram_user_id, Task),
            Task.status.in_(["pending", "overdue"]),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.created_at.desc())
        .limit(_TASK_FETCH_CAP)
        .all()
    )
    for task in tasks:
        desc = (task.description or "").lower()
        if hint and hint in desc:
            return task
    return None

def complete_task(db: Session, telegram_user_id: int, task_id: int) -> Task | None:
    from app.database.crud import _invalidate_caches, accessible_user_ids

    task = (
        db.query(Task)
        .filter(
            Task.id == task_id,
            company_filter(db, telegram_user_id, Task),
            Task.deleted_at.is_(None),
        )
        .first()
    )
    if not task or task.status not in ("pending", "overdue"):
        return None

    # تحديث شرطي ذرّي بدل "اقرأ ثم ثبّت": طلبان متزامنان قد يقرآن status ==
    # pending كلاهما قبل إثبات أيٍّ منهما. التحديث هنا لا يطبَّق إلا إذا كان
    # السطر ما زال pending/overdue (rowcount) — الرابح فقط يتقدم لإعادة توليد
    # المهمة المتكررة، والخاسر يعود None فيتجنب مكرَّرين بدل واحد (لا حاجة
    # لقفل FOR UPDATE: التحديث الشرطي نفسه يحمي في SQLite وMySQL).
    updated = (
        db.query(Task)
        .filter(
            Task.id == task_id,
            company_filter(db, telegram_user_id, Task),
            Task.status.in_(["pending", "overdue"]),
            Task.deleted_at.is_(None),
        )
        .update({Task.status: "done", Task.updated_at: now_utc()})
    )
    db.commit()
    if updated != 1:
        db.rollback()
        return None
    db.refresh(task)

    log_audit(
        telegram_user_id,
        "complete_task",
        f"task:{task_id}",
        detail=(task.description or "")[:80],
    )
    _invalidate_caches(db, telegram_user_id)
    _respawn_recurring_task(db, telegram_user_id, task)
    return task

def _respawn_recurring_task(db: Session, telegram_user_id: int, done_task: Task) -> None:
    """يعيد جدولة مهمة متكررة: عند إنجازها يُنشئ تكرارًا تاليًا (يوم/أسبوع/شهر).

    التالي يُحسب من الموعد الأصلي للمهمة المنجزة. إذا تعذّر حساب موعد،
    تُهمَل إعادة الجدولة بصمت (المهمة أُنجزت وانتهت).
    """
    from app.database.crud import _invalidate_caches

    rule = getattr(done_task, "recurrence_rule", None)
    due = getattr(done_task, "due_date", None)
    if not rule or not due:
        return

    try:
        local_due = to_local_naive(due)
        if rule == "daily":
            next_due = local_due + timedelta(days=1)
        elif rule == "weekly":
            next_due = local_due + timedelta(days=7)
        elif rule == "monthly":
            year = local_due.year + (1 if local_due.month == 12 else 0)
            month = 1 if local_due.month == 12 else local_due.month + 1
            # موعد شهري في يوم 29/30/31 مقابل شهر أقصر: نقيّد اليوم لآخر يوم
            # صالح في الشهر الهدف بدل ValueError (يلتف على إثبات الإنجاز).
            last_day = calendar.monthrange(year, month)[1]
            next_due = local_due.replace(
                year=year, month=month, day=min(local_due.day, last_day)
            )
        else:
            return
    except ValueError:
        # تعذّر حساب الموعد التالي — تُهمَل إعادة الجدولة بأمان (المهمة أُنجزت).
        return

    spawn = Task(
        telegram_user_id=telegram_user_id,
        company_id=getattr(done_task, "company_id", None),
        description=done_task.description,
        due_date=to_utc_naive(next_due),
        person=done_task.person,
        priority=done_task.priority or "normal",
        recurrence_rule=rule,
        status="pending",
        raw_message=done_task.raw_message,
        reminder_sent=False,
    )
    db.add(spawn)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return
    _invalidate_caches(db, telegram_user_id)

def update_task(db: Session, row: Task, fields: dict[str, str]) -> Task:
    """يحدّث حقول محددة في مهمة."""
    from app.database.crud import _clean_person, _clean_text, _invalidate_caches, parse_date_local


    old = {
        key: getattr(row, key, None) for key in ("description", "person", "due_date", "priority")
    }
    for key in ("description", "person", "due_date", "priority"):
        if key in fields and fields[key] is not None:
            if key == "due_date":
                due_value = fields[key]
                row.due_date = (
                    due_value
                    if isinstance(due_value, datetime)
                    else parse_date_local(due_value)
                )
            elif key == "person":
                row.person = _clean_person(fields[key])
            elif key == "description":
                row.description = _clean_text(fields[key]) or row.description
            elif key == "priority":
                row.priority = _normalize_priority(fields[key])
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    changes = {
        k: {"old": str(old[k])[:80], "new": str(getattr(row, k))[:80]}
        for k in old
        if str(getattr(row, k, None)) != str(old[k])
    }
    log_audit(row.telegram_user_id, "update", f"task:{row.id}", detail=f"changes={changes}")
    _invalidate_caches(db, row.telegram_user_id)
    return row
