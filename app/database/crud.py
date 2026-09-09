import calendar
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from dateutil import parser as date_parser
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cache import clear as clear_cache
from app.database.models import (
    Budget,
    CorrectionFeedback,
    CreditLimit,
    Invoice,
    Note,
    ReportPref,
    Task,
    Transaction,
    UserPref,
    WorkspaceMember,
)

# ---------- مساحات العمل المشتركة (حساب واحد لأكثر من معرّف) ----------


def workspace_for_user(db: Session, telegram_user_id: int) -> int | None:
    """معرّف مساحة العمل التي ينتمي إليها المستخدم (أو None إن بقي فرديًا)."""
    row = (
        db.query(WorkspaceMember.workspace_id)
        .filter(WorkspaceMember.telegram_user_id == telegram_user_id)
        .first()
    )
    return row[0] if row else None


def workspace_member_ids(db: Session, workspace_id: int) -> set[int]:
    """كل معرّفات الأعضاء داخل مساحة العمل (بما فيها المالك)."""
    rows = (
        db.query(WorkspaceMember.telegram_user_id)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .all()
    )
    return {uid for (uid,) in rows}


def accessible_user_ids(db: Session, telegram_user_id: int) -> set[int]:
    """مجموعة المعرّفات التي يرى المستخدم بياناتها (مشتركة أم فردية).

    فردي: {نفسه} فقط — سلوك اليوم تمامًا.
    عضو مساحة: كل أعضاء المساحة. يعطي "حسابًا مشتركًا" بلا تحرّك بيانات.
    """
    wid = workspace_for_user(db, telegram_user_id)
    if wid is None:
        return {telegram_user_id}
    members = workspace_member_ids(db, wid)
    members.add(telegram_user_id)  # أمان إضافي لو لا يرتبط الصف بعد
    return members


def is_workspace_owner(db: Session, telegram_user_id: int) -> bool:
    """هل المستخدم هو مرتكز (مالك) مساحة العمل الحالية؟"""
    wid = workspace_for_user(db, telegram_user_id)
    return wid is not None and wid == telegram_user_id


def create_workspace(db: Session, owner_telegram_user_id: int) -> WorkspaceMember:
    """ينشئ مساحة عمل للمستخدم (مرتكزها معرّفه) — idempotent.

    يُستدعى تلقائيًا عند أول طلب إنشاء مشاركة؛ القيم الفردية لا تحتاج أي
    إنشاء (سلوك اليوم بلا مساحات).
    """
    row = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.telegram_user_id == owner_telegram_user_id)
        .first()
    )
    if row is not None:
        return row
    row = WorkspaceMember(
        telegram_user_id=owner_telegram_user_id, workspace_id=owner_telegram_user_id
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def invite_to_workspace(
    db: Session, owner_telegram_user_id: int, new_telegram_user_id: int
) -> bool:
    """يدعو المالك معرّفًا إلى مساحته (upsert). يعيد False لغير المالك/ذاتي."""
    wid = workspace_for_user(db, owner_telegram_user_id)
    if wid != owner_telegram_user_id or new_telegram_user_id == owner_telegram_user_id:
        return False
    row = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.telegram_user_id == new_telegram_user_id)
        .first()
    )
    if row is None:
        row = WorkspaceMember(telegram_user_id=new_telegram_user_id, workspace_id=wid)
        db.add(row)
    else:
        row.workspace_id = wid
    db.commit()
    return True


def remove_from_workspace(
    db: Session, owner_telegram_user_id: int, target_telegram_user_id: int
) -> bool:
    """يُخرج المالك عضوًا من مساحته (لا يمكن للمالك إخراج نفسه)."""
    wid = workspace_for_user(db, owner_telegram_user_id)
    if wid != owner_telegram_user_id or target_telegram_user_id == owner_telegram_user_id:
        return False
    row = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.telegram_user_id == target_telegram_user_id)
        .first()
    )
    if row is None or row.workspace_id != wid:
        return False
    db.delete(row)
    db.commit()
    return True


def leave_workspace(db: Session, telegram_user_id: int) -> bool:
    """المستخدم يغادر مساحته الحالية (المالك لا يغادر — لا مكان لمدير مساحته)."""
    wid = workspace_for_user(db, telegram_user_id)
    if wid is None or wid == telegram_user_id:
        return False
    row = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.telegram_user_id == telegram_user_id,
            WorkspaceMember.workspace_id == wid,
        )
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def list_workspace(db: Session, telegram_user_id: int) -> dict | None:
    """معلومات مساحة العمل الحالية: {workspace_id, members: [..], owner: bool} أو None."""
    wid = workspace_for_user(db, telegram_user_id)
    if wid is None:
        return None
    return {
        "workspace_id": wid,
        "owner": wid == telegram_user_id,
        "members": sorted(workspace_member_ids(db, wid)),
    }


def transfer_workspace_ownership(
    db: Session, current_owner: int, new_owner: int
) -> bool:
    """ينقل ملكية المساحة إلى عضو قائم فيها — يصبح هو مرتكزها الجديد.

    مفيد عند التنحي/تسليم الإدارة: كل صفوف المساحة تُعاد توجيهها إلى
    workspace_id == new_owner. يعيد False لغير المالك أو لعضو ليس في المساحة.
    """
    wid = workspace_for_user(db, current_owner)
    if wid != current_owner or new_owner == current_owner:
        return False
    target = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.telegram_user_id == new_owner,
            WorkspaceMember.workspace_id == wid,
        )
        .first()
    )
    if target is None:
        return False
    db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == wid).update(
        {"workspace_id": new_owner}
    )
    db.commit()
    return True


def dissolve_workspace(db: Session, owner: int) -> bool:
    """يفكّ المساحة المشتركة كليًا — كل عضو يعود مستخدمًا فرديًا.

    بيانات الأعضاء (معاملات/مهام/ملاحظات) تبقى موجودة بلا أي فقدان؛ يفقد فقط
    الربط المشترك. يعيد False إن لم يكن المستخدم مالكًا لأي مساحة.
    """
    wid = workspace_for_user(db, owner)
    if wid != owner:
        return False
    deleted = db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == wid).delete()
    db.commit()
    return deleted > 0


def _invalidate_caches(db: Session, telegram_user_id: int) -> None:
    """يُستدعى بعد أي كتابة: يمسح الاستعلامات المؤقتة لكل من يرى بيانات هذا المستخدم.

    في مساحة مشتركة تُبنى مفاتيح run_query على accessible_user_ids (كل الأعضاء)،
    لذا يجب إبطال كاش كل الأعضاء لا الكاتب فقط — وإلا بقي عضو آخر يرى إجماليات
    قديمة حتى انتهاء TTL.
    """
    for uid in accessible_user_ids(db, telegram_user_id):
        clear_cache(f"run_query:{uid}")
    from app.admin import clear_admin_cache

    clear_admin_cache()


# تطبيع العملة نحو رموز ISO 4217 (مناسبة للشيقل والدولار في فلسطين)
CURRENCY_ALIASES = {
    "شيكل": "ILS",
    "الشيكل": "ILS",
    "شواكل": "ILS",
    "شيقل": "ILS",
    "شياقل": "ILS",
    "شيقلا": "ILS",
    "₪": "ILS",
    "nis": "ILS",
    "₪:": "ILS",
    "shekel": "ILS",
    "shekels": "ILS",
    "ils": "ILS",
    "دولار": "USD",
    "الدولار": "USD",
    "دولارات": "USD",
    "$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "دينار": "JOD",
    "الدينار": "JOD",
    "دنانير": "JOD",
    "jd": "JOD",
    "يورو": "EUR",
    "€": "EUR",
    "euro": "EUR",
    "eur": "EUR",
}


def normalize_currency(raw: str | None) -> str | None:
    """يحوّل أي صيغة عملة إلى رمز ISO موحّد (ILS لشيقل، USD لدولار...)."""
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "")
    if key in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[key]
    # تطابق جزئي (مثل "شيكل جديد", "دولار امريكي") — بمقارنة غير حساسة لحالة الأحرف
    raw_lower = raw.strip().lower()
    for alias, code in CURRENCY_ALIASES.items():
        if alias in raw_lower:
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


def _to_decimal(value) -> Decimal | None:
    """يحوّل المبلغ إلى Decimal بدقة نقطتين عشريتين (برای تجنّب أخطاء تقريب float).

    الحقل amount معرّف بـ Numeric(12,2) في قاعدة البيانات؛ استخدام float مباشرة
    قد يُدخل قيمًا مثل 0.30000000000000004. نحوّل هنا عبر Decimal مع تقريب مصرفي.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _is_duplicate_message(
    db: Session, model, telegram_user_id: int, telegram_message_id: int | None
) -> bool:
    """يتحقق هل تم تسجيل نفس الرسالة مسبقًا (idempotency)."""
    if telegram_message_id is None:
        return False
    return (
        db.query(model)
        .filter(
            model.telegram_user_id == telegram_user_id,
            model.telegram_message_id == telegram_message_id,
        )
        .first()
        is not None
    )


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


def _normalize_priority(value) -> str:
    """يقنن قيمة الأولوية إلى high|normal|low (الافتراضي normal)."""
    v = (_clean_text(value) or "").lower()
    if v in ("high", "عالية", "عالي", "عالى", "مهم", "عاجل", "مستعجل", "h"):
        return "high"
    if v in ("low", "منخفضة", "منخفض", "ضعيفة", "l", "عادية جدًا"):
        return "low"
    return "normal"


def _normalize_recurrence(value) -> str | None:
    """يقنن قيمة التكرار إلى daily|weekly|monthly أو None."""
    v = (_clean_text(value) or "").lower()
    mapping = {
        "daily": "daily",
        "يومي": "daily",
        "كل يوم": "daily",
        "يوم": "daily",
        "weekly": "weekly",
        "أسبوعي": "weekly",
        "كل اسبوع": "weekly",
        "كل أسبوع": "weekly",
        "اسبوعي": "weekly",
        "اسبوع": "weekly",
        "monthly": "monthly",
        "شهري": "monthly",
        "كل شهر": "monthly",
        "شهر": "monthly",
    }
    return mapping.get(v)


_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}
_MAX_DT = datetime.max


def can_manage_records(db: Session, telegram_user_id: int) -> bool:
    """صلاحيات حذف/تعديل السجلات: الأفراد دائمًا نعم؛ أعضاء مساحة مشتركة —
    المرتكز (المالك) فقط (أعضاء عاديون يسجّلون ويقرؤون لكن لا يمسحون/يعدّلون)."""
    wid = workspace_for_user(db, telegram_user_id)
    if wid is None:
        return True
    return wid == telegram_user_id


def create_task(
    db: Session,
    telegram_user_id: int,
    data: dict,
    raw_message: str,
    telegram_message_id: int | None = None,
) -> Task | None:
    if _is_duplicate_message(db, Task, telegram_user_id, telegram_message_id):
        return None

    due_date = parse_date_local(data.get("date")) if data.get("date") else None

    task = Task(
        telegram_user_id=telegram_user_id,
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
    """فلترة SQL حسب الشخص (بمطابقة جزئية تشبه المعاملات)."""
    if person:
        return Task.person.like(f"%{person}%")
    return None


def _priority_sort(tasks: list[Task]) -> list[Task]:
    """يرتب حسب الأولوية (عالية أولًا) ثم الموعد ثم الأحدث — الترتيب يكون في
    Python لأن الوصف مشفّر ولا يمكن الاعتماد على SQL لفرز الأولوية والموعد معًا."""
    return sorted(
        tasks,
        key=lambda t: (
            _PRIORITY_RANK.get(t.priority or "normal", 1),
            (t.due_date or _MAX_DT),
            -(int(t.created_at.timestamp()) if t.created_at else 0),
        ),
    )


def list_pending_tasks(
    db: Session, telegram_user_id: int, person: str | None = None, limit: int = 50
):
    filters = [
        Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
        Task.status == "pending",
        Task.deleted_at.is_(None),
    ]
    pf = _person_filter(person)
    if pf is not None:
        filters.append(pf)
    tasks = db.query(Task).filter(*filters).all()
    return _priority_sort(tasks)[:limit]


def list_overdue_tasks(
    db: Session, telegram_user_id: int, person: str | None = None, limit: int = 50
):
    # تُرجع المهام المسجَّلة كمتأخرة (status == "overdue") — بعد أن
    # يقوم mark_overdue_tasks بتحديثها. (لا نعتمد على status == "pending"
    # لأنه لا يأتي بالنتائج بعد التحديث.)
    filters = [
        Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
        Task.status == "overdue",
        Task.deleted_at.is_(None),
    ]
    pf = _person_filter(person)
    if pf is not None:
        filters.append(pf)
    tasks = db.query(Task).filter(*filters).all()
    return _priority_sort(tasks)[:limit]


def mark_overdue_tasks(db: Session, telegram_user_id: int) -> int:
    from app.timeutil import now_utc

    now = now_utc()
    updated = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
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
    filters = [
        Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
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
    from app.audit import log_audit
    from app.timeutil import now_utc

    if not can_manage_records(db, telegram_user_id):
        log_audit(telegram_user_id, "denied_task_delete", f"task:{task_id}")
        return None

    task = (
        db.query(Task)
        .filter(
            Task.id == task_id,
            Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
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
    """

    hint = description_hint.strip().lower()
    tasks = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Task.status.in_(["pending", "overdue"]),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.created_at.desc())
        .all()
    )
    for task in tasks:
        desc = (task.description or "").lower()
        if hint and hint in desc:
            return task
    return None


def complete_task(db: Session, telegram_user_id: int, task_id: int) -> Task | None:
    task = (
        db.query(Task)
        .filter(
            Task.id == task_id,
            Task.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Task.deleted_at.is_(None),
        )
        .first()
    )
    if task and task.status in ("pending", "overdue"):
        task.status = "done"
        db.commit()
        db.refresh(task)
        from app.audit import log_audit

        log_audit(
            telegram_user_id,
            "complete_task",
            f"task:{task_id}",
            detail=(task.description or "")[:80],
        )
        _invalidate_caches(db, telegram_user_id)
        _respawn_recurring_task(db, telegram_user_id, task)
        return task
    return None


def _respawn_recurring_task(db: Session, telegram_user_id: int, done_task: Task) -> None:
    """يعيد جدولة مهمة متكررة: عند إنجازها يُنشئ تكرارًا تاليًا (يوم/أسبوع/شهر).

    التالي يُحسب من الموعد الأصلي للمهمة المنجزة. إذا تعذّر حساب موعد،
    تُهمَل إعادة الجدولة بصمت (المهمة أُنجزت وانتهت).
    """
    rule = getattr(done_task, "recurrence_rule", None)
    due = getattr(done_task, "due_date", None)
    if not rule or not due:
        return
    from datetime import timedelta

    from app.timeutil import to_local_naive, to_utc_naive

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


def _best_effort_base_amount(amount, currency: str | None):
    """يحوّل مبلغًا لعملة الأساس وقت التسجيل (best-effort) — يعيد (المبلغ، العملة) أو (None, None).

    دقة تاريخية: نقوم بمحاولة تحويل واحدة لحظة الإنشاء؛ إن فشلت (لا إنترنت/
    عملة غير معرفة) تُترك القيم None، وفي كل تقرير لاحق تُعاد العملية للباقي
    بأسعار اليوم عبر convert_totals_to_base(stored=...).
    """
    from app.config import settings
    from app.exchange import convert

    base = (settings.base_currency or "").upper().strip()
    if amount is None or not currency or not base or currency.upper() == base:
        return None, None
    try:
        conv = convert(amount, currency, base)
    except Exception:
        return None, None
    result = conv.get("result")
    if result is None:
        return None, None
    return Decimal(str(result)), base


def create_transaction(
    db: Session,
    telegram_user_id: int,
    data: dict,
    raw_message: str,
    telegram_message_id: int | None = None,
) -> Transaction | None:
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


def get_period_range(period: str):
    from app.timeutil import first_day_of_week, now_local, to_utc_naive

    local_now = now_local()

    if period == "today":
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_week":
        # الأسبوع يبدأ من أول يوم قابل للتكوين (افتراضيًا الأحد لفلسطين/السياق العربي)
        fd = first_day_of_week()
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
    from app.timeutil import first_day_of_week, now_local, to_utc_naive

    local_now = now_local()

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
        fd = first_day_of_week()
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


def _sum_amounts_by_currency(rows) -> dict:
    """يجمع مبالغ سجلات (بعد فك التشفير) لكل عملة — يستخدم بدل SQL SUM.

    لأن amount مخزَّن مشفّرًا، تُقرأ الصفوف ويُجمَع في Python.
    """
    total: dict = {}
    for r in rows:
        amt = r.amount
        if amt is None:
            continue
        c = r.currency or "غير محددة"
        total[c] = total.get(c, Decimal("0")) + amt
    return total


def _split_stored_base(rows, base_currency: str) -> tuple[dict, dict]:
    """يقسّم صفوف المعاملات إلى (مبالغ محوَّلة بعملة الأساس وقت التسجيل، مبالغ حيّة).

    العائد: (stored: {currency: Decimal}, live: {currency: Decimal}).
    المبالغ المحوَّلة موثّقة أن base_currency_at_creation == base أي أن قيمها فعلًا
    بعملة الأساس؛ ما عداها يحتاج سعر اليوم لحظة التقارير.
    """
    stored: dict = {}
    live: dict = {}
    for r in rows:
        amt = r.amount
        if amt is None:
            continue
        c = r.currency or "غير محددة"
        if r.base_currency_at_creation == base_currency and r.amount_in_base_currency is not None:
            stored[c] = stored.get(c, Decimal("0")) + Decimal(str(r.amount_in_base_currency))
        else:
            live[c] = live.get(c, Decimal("0")) + amt
    return stored, live


def _unified_totals_for_rows(rows, base_currency: str) -> dict:
    """مجموع موحّد بدقة تاريخية: يفضّل مبالغ سعر الصرف المثبَّت وقت التسجيل.

    يعيد: {"base", "total": Decimal|None, "partial": bool, "from_stored": bool}
    يُرفق كحقل فرعي في نتيجة run_query ويقرؤه تنسيق العرض مباشرة.
    """
    from app.exchange import convert_totals_to_base

    stored, live = _split_stored_base(rows, base_currency)
    conv = convert_totals_to_base(live, base_currency, stored=stored)
    return {
        "base": base_currency,
        "total": conv.get("total"),
        "partial": conv.get("partial", False),
        "from_stored": bool(stored),
    }


def run_query(db: Session, telegram_user_id: int, query_details: dict) -> dict:
    """تنفيذ استعلام مع تخزين مؤقت قصير (TTL) للاستعلامات التجميعية المتكررة.

    نفس المستخدم يسأل "كم صرفت هذا الشهر" مرارًا خلال الدقيقة → نُعيد النتيجة
    المخزنة بدل إعادة الجمع في Python. تُمسح ذاكرة المستخدم عند أي كتابة.
    """
    metric = query_details.get("metric")
    period = query_details.get("period") or "all_time"
    person = query_details.get("person")

    # استعلامات المهام تُبقي دائمًا قراءة حية (تواريخ الاستحقاق تتغير كل لحظة)
    if metric in ("list_tasks", "list_overdue_tasks"):
        return _run_query_uncached(db, telegram_user_id, query_details)

    from app.cache import get_or_set

    namespace = "run_query"
    key = f"{namespace}:{telegram_user_id}:{metric}:{period}:{person}"
    return get_or_set(key, lambda: _run_query_uncached(db, telegram_user_id, query_details))


def _run_query_uncached(db: Session, telegram_user_id: int, query_details: dict) -> dict:
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
        from app.config import settings

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
        from app.config import settings

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
    from app.timeutil import now_utc

    if not can_manage_records(db, telegram_user_id):
        from app.audit import log_audit

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

    from app.audit import log_audit

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
    from app.audit import log_audit

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
    from app.timeutil import to_local_naive

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
    from app.audit import log_audit

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


def search_records(
    db: Session, telegram_user_id: int, term: str, limit: int = 30, return_meta: bool = False
) -> list[dict] | tuple[list[dict], dict]:
    """بحث نصي بسيط في آخر سجلات المستخدم (شخص/تصنيف/وصف/قيمة/نوع).

    الوصف وقيم المبالغ مشفّرة، لذلك نقرأ عددًا محدودًا من السجلات الحديثة
    ونطابقها في Python (مستوى البيانات الشخصية يكفي أداءً). يعيد نفس بنية
    list_recent_records ليعاد استخدامها في عرض قوائم الأزرار.

    مع return_meta=True يعيد (نتائج, meta) حيث meta يحتوي per_model=حجم النافذة
    وsaturated=True إن امتلأت النافذة (قد توجد سجلات أقدم مطابقة لم تُفحص) —
    ليُبلَّغ المستخدم أن البحث جزئي بدل وهم "لا توجد نتائج".
    """
    from app.timeutil import to_local_naive

    needle = (term or "").strip().lower()
    if not needle:
        return ([], {"per_model": 0, "saturated": False}) if return_meta else []

    accessible = accessible_user_ids(db, telegram_user_id)
    per_model = max(limit * 2, 100)
    matches: list[dict] = []
    saturated = False
    kinds = {
        "expense": "مصروف",
        "income": "إيراد",
        "task": "مهمة",
        "order": "طلبية",
        "note": "ملاحظة",
    }

    for model in (Transaction, Task, Note):
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
                matches.append(
                    {
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
                )
    matches.sort(key=lambda c: c["created_at"], reverse=True)
    result = matches[:limit]
    if return_meta:
        return result, {"per_model": per_model, "saturated": saturated}
    return result


def get_user_lang(db: Session, telegram_user_id: int) -> str:
    """لغة الواجهة المحفوظة للمستخدم (ar افتراضي)."""
    row = db.query(UserPref).filter(UserPref.telegram_user_id == telegram_user_id).first()
    return row.lang if row and row.lang else "ar"


def set_user_lang(db: Session, telegram_user_id: int, lang: str) -> str:
    """يحفظ لغة الواجهة ويعيدها (يقنّن إلى ar/en)."""
    lang = "en" if (lang or "").strip().lower() == "en" else "ar"
    row = db.query(UserPref).filter(UserPref.telegram_user_id == telegram_user_id).first()
    if row is None:
        row = UserPref(telegram_user_id=telegram_user_id, lang=lang)
        db.add(row)
    else:
        row.lang = lang
        row.updated_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return lang


def update_transaction(db: Session, row: Transaction, fields: dict) -> Transaction:
    """يحدّث حقول محددة في معاملة مالية."""
    from app.audit import log_audit
    from app.timeutil import now_utc

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


def update_task(db: Session, row: Task, fields: dict) -> Task:
    """يحدّث حقول محددة في مهمة."""
    from app.audit import log_audit
    from app.timeutil import now_utc

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


def update_note(db: Session, row: Note, fields: dict) -> Note:
    """يحدّث حقول محددة في ملاحظة/طلبية."""
    from app.audit import log_audit
    from app.timeutil import now_utc

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


# ---------- الميزانيات الشهرية ----------


def create_budget(
    db: Session,
    telegram_user_id: int,
    scope: str,
    target: str,
    monthly_limit,
    name: str | None = None,
) -> Budget | None:
    """ينشئ ميزانية شهرية: scope=currency/person/category، target هو العملة/الاسم/التصنيف.

    يعيد None إذا الميزانية موجودة مسبقًا (لكل مستخدم واحد لكل scope/هدف).
    """

    scope = (scope or "").lower().strip()
    currency = normalize_currency(target) if scope == "currency" else None
    person = target.strip() if scope == "person" else None
    category = _clean_text(target) if scope == "category" else None

    if scope == "currency":
        from app.exchange import CURRENCY_NAMES

        if currency is None or currency.upper() not in CURRENCY_NAMES:
            # عملة غير معروفة (normalize_currency تمرر النص كما هو) — رفضها
            # بدل إنشاء ميزانية لن تطابقها أي معاملة أبدًا (تبقى 0% للأبد).
            return None

    if not monthly_limit:
        return None
    try:
        limit = Decimal(str(monthly_limit)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if limit <= 0:
        return None

    if not currency and not person and not category:
        return None

    budget = Budget(
        telegram_user_id=telegram_user_id,
        scope=scope,
        currency=currency,
        person=person,
        category=category,
        name=_clean_text(name),
        monthly_limit=limit,
        month_key=_current_month_key(),
    )
    db.add(budget)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(budget)
    _invalidate_caches(db, telegram_user_id)
    return budget


def list_budgets(db: Session, telegram_user_id: int) -> list[Budget]:
    return (
        db.query(Budget)
        .filter(Budget.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)))
        .order_by(Budget.created_at.asc())
        .all()
    )


def get_budget(db: Session, telegram_user_id: int, budget_id: int) -> Budget | None:
    return (
        db.query(Budget)
        .filter(
            Budget.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Budget.id == budget_id,
        )
        .first()
    )


def delete_budget(db: Session, telegram_user_id: int, budget_id: int) -> bool:
    budget = get_budget(db, telegram_user_id, budget_id)
    if not budget:
        return False
    from app.audit import log_audit

    detail = f"scope={budget.scope} target={budget.person or budget.currency} limit={budget.monthly_limit}"
    db.delete(budget)
    db.commit()
    log_audit(telegram_user_id, "delete_budget", f"budget:{budget_id}", detail=detail)
    _invalidate_caches(db, telegram_user_id)
    return True


def _current_month_key() -> str:
    from app.timeutil import now_local

    return now_local().strftime("%Y-%m")


def budget_usage(db: Session, budget: Budget) -> dict:
    """استهلاك الميزانية هذا الشهر (محليًا).

    يعيد: {spent: Decimal, limit: Decimal, percent: float, over: bool}
    """
    from app.timeutil import now_local, to_utc_naive

    local_now = now_local()
    local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = to_utc_naive(local_start)

    q = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(accessible_user_ids(db, budget.telegram_user_id)),
        Transaction.deleted_at.is_(None),
        Transaction.type == "expense",
        Transaction.created_at >= start,
    )
    if budget.scope == "currency":
        q = q.filter(Transaction.currency == budget.currency)
    elif budget.scope == "category":
        q = q.filter(Transaction.category.like(f"%{budget.category}%"))
    else:
        q = q.filter(Transaction.person.like(f"%{budget.person}%"))

    spent_rows = q.all()
    spent = sum((r.amount for r in spent_rows if r.amount is not None), Decimal("0"))
    spent = spent.quantize(Decimal("0.01"))
    limit = (budget.monthly_limit or Decimal("0")).quantize(Decimal("0.01"))

    percent = float(spent / limit * 100) if limit else 0.0
    return {
        "spent": spent,
        "limit": limit,
        "percent": round(percent, 1),
        "over": spent >= limit,
    }


def budget_monthly_reset(db: Session, budget: Budget) -> bool:
    """يرجّع True إذا تغيّر الشهر ويجب إعادة ضبط حالة التنبيه."""
    current_key = _current_month_key()
    if budget.month_key != current_key:
        budget.month_key = current_key
        budget.alerted_status = 0
        db.commit()
        return True
    return False


# ---------- الديون والأشخاص (#21) ----------


def person_debts(db: Session, telegram_user_id: int) -> list[dict]:
    """رصيد كل شخص بعدة عملات (بماذا تدين له / بماذا يدين لك).

    يعيد قائمة مرتبة بالأحدث: لكل شخص by_currency: {مع الصفوف: income/expense/balance}
    و balance_unified (بالعملة الأساس بدقة تاريخية عبر المبالغ المخزّنة) إن أمكن.
    balance = income - expense: موجب = يدين لك، سالب = تدين له.
    """
    from app.config import settings

    base = (settings.base_currency or "").upper().strip()
    ids = accessible_user_ids(db, telegram_user_id)
    rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(ids),
            Transaction.deleted_at.is_(None),
            Transaction.person.isnot(None),
        )
        .all()
    )

    persons: dict[str, list] = {}
    for r in rows:
        name = (r.person or "").strip()
        if not name:
            continue
        persons.setdefault(name, []).append(r)

    result = []
    for name, pr in persons.items():
        per_cur: dict[str, dict] = {}
        for r in pr:
            c = r.currency or "غير محددة"
            cell = per_cur.setdefault(c, {"income": Decimal("0"), "expense": Decimal("0")})
            if r.amount is not None:
                cell[r.type] = cell.get(r.type, Decimal("0")) + r.amount
        by_currency = {}
        for c, cell in per_cur.items():
            balance = cell["income"] - cell["expense"]
            by_currency[c] = {
                "income": cell["income"].quantize(Decimal("0.01")),
                "expense": cell["expense"].quantize(Decimal("0.01")),
                "balance": balance.quantize(Decimal("0.01")),
            }

        unified_inc = _unified_totals_for_rows(
            [r for r in pr if r.type == "income"], base
        )["total"]
        unified_exp = _unified_totals_for_rows(
            [r for r in pr if r.type == "expense"], base
        )["total"]
        if unified_inc is not None and unified_exp is not None:
            net = unified_inc - unified_exp
        else:
            net = None
        result.append(
            {
                "person": name,
                "by_currency": by_currency,
                "balance_unified": net.quantize(Decimal("0.01")) if net is not None else None,
                "base": base,
                "partial": unified_inc is None or unified_exp is None,
                # اتجاه الدين بلا أرقام: ما إذا كان صافي كل شيء موجبًا (يدين لك)
                "net_positive": net is not None and net >= 0,
                "last_activity": max((r.created_at for r in pr), default=None),
            }
        )

    result.sort(key=lambda d: (d["last_activity"] is not None, d["last_activity"]), reverse=True)
    return result


# ---------- الفواتير الآجلة (#22) ----------


def create_invoice(
    db: Session,
    telegram_user_id: int,
    data: dict,
    raw_message: str | None = None,
) -> Invoice | None:
    """ينشئ فاتورة آجلة؛ amount إلزامي. يعيد None على قيم غير صالحة."""
    from app.config import settings

    amount = _to_decimal(data.get("amount"))
    if amount is None or amount <= 0:
        return None
    currency = normalize_currency(data.get("currency")) or (settings.base_currency or "").upper()
    due_date = data.get("due_date")
    if isinstance(due_date, str) and due_date.strip():
        try:
            due_date = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
        except ValueError:
            due_date = None
        if due_date is not None and due_date.tzinfo is not None:
            from app.timeutil import to_utc_naive

            due_date = to_utc_naive(due_date)
    if isinstance(due_date, datetime) and due_date.tzinfo is not None:
        from app.timeutil import to_utc_naive

        due_date = to_utc_naive(due_date)

    invoice = Invoice(
        telegram_user_id=telegram_user_id,
        person=_clean_person(data.get("person")),
        amount=amount.quantize(Decimal("0.01")),
        currency=currency or None,
        description=_clean_text(data.get("description")) or _clean_text(raw_message),
        due_date=due_date,
        status="pending",
        alerted=False,
    )
    db.add(invoice)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(invoice)
    _invalidate_caches(db, telegram_user_id)
    return invoice


def list_invoices(
    db: Session, telegram_user_id: int, status: str | None = None, limit: int = 50
) -> list[Invoice]:
    """فواتير المستخدم (كل أعضاء المساحة) مطابقة لحالة اختيارية، الأحدث أولًا."""
    from app.timeutil import now_utc, to_local_naive

    q = db.query(Invoice).filter(
        Invoice.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id))
    )
    if status and status in ("pending", "paid", "overdue"):
        q = q.filter(Invoice.status == status)
    invoices = q.order_by(Invoice.created_at.desc()).limit(limit).all()

    # تعليم المتأخرة (حالة عرض) عند قراءتها — التغيير الفعلي للموديل الوارد في
    # المهمة يبقى في مهمة الفحص الدوري حتى لا نكتب على كل قراءة.
    for inv in invoices:
        if (
            inv.status == "pending"
            and inv.due_date is not None
            and to_local_naive(inv.due_date) < to_local_naive(now_utc())
        ):
            inv.status = "overdue"
    return invoices


def mark_invoice_paid(db: Session, telegram_user_id: int, invoice_id: int) -> bool:
    """سداد فاتورة آجلة — تعيد True عند النجاح."""
    invoice = (
        db.query(Invoice)
        .filter(
            Invoice.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Invoice.id == invoice_id,
        )
        .first()
    )
    if invoice is None or invoice.status == "paid":
        return False
    from app.timeutil import now_utc

    invoice.status = "paid"
    invoice.paid_at = now_utc()
    invoice.alerted = True  # لا مزيد من تنبيهات التأخر
    db.commit()
    _invalidate_caches(db, telegram_user_id)
    return True


def mark_overdue_invoices(db: Session, now_dt=None) -> list[Invoice]:
    """يميّز الفواتير المعلّقة المتأخرة (استحقاقها مضى) — يرجّع المتأخرة حديثًا.

    مستخدم من مهمة الفحص الدوري (reminders)؛ يكتب الحالة للعرض فقط دون إرسال.
    """
    from app.timeutil import now_utc

    now_dt = now_dt or now_utc()
    overdue = []
    rows = (
        db.query(Invoice)
        .filter(Invoice.status == "pending", Invoice.due_date.isnot(None), Invoice.due_date < now_dt)
        .all()
    )
    for inv in rows:
        if inv.status != "overdue":
            inv.status = "overdue"
            overdue.append(inv)
    if overdue:
        db.commit()
        for inv in overdue:
            db.refresh(inv)  # حتى تبقى سماته قابلة للقراءة بعد إغلاق الجلسة
    return overdue


# ---------- دورة حياة الطلبيات (#38) ----------

ORDER_STATUSES = ("open", "done")


def list_orders(
    db: Session, telegram_user_id: int, status: str | None = None, limit: int = 50
) -> list[Note]:
    """الطلبيات (note_type=order) مع فلتر حالة اختياري، الأحدث أولًا."""
    q = db.query(Note).filter(
        Note.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
        Note.deleted_at.is_(None),
        Note.note_type == "order",
    )
    if status and status in ORDER_STATUSES:
        q = q.filter(Note.status == status)
    return q.order_by(Note.created_at.desc()).limit(limit).all()


def set_order_status(db: Session, telegram_user_id: int, note_id: int, status: str) -> bool:
    """يغيّر حالة طلبية (open/done) — يعيد False إن لم توجد طلبية أو حالة غير صالحة."""
    if status not in ORDER_STATUSES:
        return False
    note = (
        db.query(Note)
        .filter(
            Note.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Note.id == note_id,
            Note.note_type == "order",
            Note.deleted_at.is_(None),
        )
        .first()
    )
    if note is None:
        return False
    from app.timeutil import now_utc

    note.status = status
    note.updated_at = now_utc()
    db.commit()
    _invalidate_caches(db, telegram_user_id)
    return True


# ---------- الحدود الائتمانية (#26) ----------


def set_credit_limit(
    db: Session, telegram_user_id: int, person: str, limit_amount,
) -> CreditLimit | None:
    """يحدد/يحدّث سقفًا ائتمانيًا لشخص. يعيد None على قيم غير صالحة."""
    person = _clean_person(person)
    if not person:
        return None
    try:
        limit = Decimal(str(limit_amount)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if limit <= 0:
        return None

    row = (
        db.query(CreditLimit)
        .filter(
            CreditLimit.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            CreditLimit.person == person,
        )
        .first()
    )
    if row is None:
        row = CreditLimit(telegram_user_id=telegram_user_id, person=person, limit_amount=limit)
        db.add(row)
    else:
        row.limit_amount = limit
    from app.timeutil import now_utc

    row.alerted_status = 0  # إعادة تنبيه بحدود جديدة
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    _invalidate_caches(db, telegram_user_id)
    return row


def list_credit_limits(db: Session, telegram_user_id: int) -> list[CreditLimit]:
    return (
        db.query(CreditLimit)
        .filter(CreditLimit.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)))
        .order_by(CreditLimit.created_at.asc())
        .all()
    )


def get_credit_limit(db: Session, telegram_user_id: int, person: str) -> CreditLimit | None:
    return (
        db.query(CreditLimit)
        .filter(
            CreditLimit.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            CreditLimit.person == person,
        )
        .first()
    )


def credit_usage(db: Session, limit_row: CreditLimit) -> dict:
    """استخدام السقف الائتماني لشخص: outstanding = المصروفات - الإيرادات.

    موجب = الدين عليك لهذا الشخص؛ سلبي = يستفيد هو منك. يعيد:
    {outstanding, limit, percent, over} حيث percent نسبة الدين الفعلي (الموجب فقط).
    """
    rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(accessible_user_ids(db, limit_row.telegram_user_id)),
            Transaction.deleted_at.is_(None),
            Transaction.person.like(f"%{limit_row.person}%"),
        )
        .all()
    )
    income = sum((r.amount for r in rows if r.amount is not None and r.type == "income"), Decimal("0"))
    expense = sum(
        (r.amount for r in rows if r.amount is not None and r.type == "expense"), Decimal("0")
    )
    outstanding = expense - income
    total = outstanding.quantize(Decimal("0.01"))
    limit = (limit_row.limit_amount or Decimal("0")).quantize(Decimal("0.01"))
    percent = float(total / limit * 100) if limit and total > 0 else 0.0
    return {
        "outstanding": total,
        "limit": limit,
        "percent": round(percent, 1),
        "over": total >= limit,
    }


# ---------- تفضيلات التقارير الدورية ----------


def get_report_pref(db: Session, telegram_user_id: int) -> ReportPref | None:
    return db.query(ReportPref).filter(ReportPref.telegram_user_id == telegram_user_id).first()


def set_report_frequency(
    db: Session, telegram_user_id: int, frequency: str, deliver_time: str | None = None
) -> ReportPref:
    """يضبط تفضيل التقارير الدورية للمستخدم (إنشاء/تحديث)."""
    from app.timeutil import now_utc

    pref = get_report_pref(db, telegram_user_id)
    if pref is None:
        pref = ReportPref(telegram_user_id=telegram_user_id, frequency=frequency)
        db.add(pref)
    pref.frequency = frequency
    if deliver_time:
        pref.deliver_time = deliver_time
    pref.updated_at = now_utc()
    db.commit()
    db.refresh(pref)
    return pref


def list_report_prefs(db: Session) -> list[ReportPref]:
    """كل المستخدمين الذين فعّلوا التقارير الدورية (frequency != off)."""
    return (
        db.query(ReportPref)
        .filter(ReportPref.frequency != "off")
        .order_by(ReportPref.telegram_user_id.asc())
        .all()
    )


def mark_report_sent(db: Session, pref: ReportPref) -> None:
    """يسجّل وقت إرسال آخر تقرير دوري (لمنع التكرار)."""
    from app.timeutil import now_utc

    pref.last_sent_at = now_utc()
    db.commit()


# ---------- إحصاءات الرسوم البيانية ----------


def monthly_totals(
    db: Session, telegram_user_id: int, months: int = 6, include_stored: bool = False
) -> list[dict]:
    """إجمالي المصروفات والإيرادات لكل شهر من آخر N أشهر (بالتوقيت المحلي).

    يعيد قائمة مرتبة زمنيًا: [{year, month, label, expense: Decimal, income: Decimal, key: "YYYY-MM"}]
    القيم الخام بعملاتها الأصلية (تُوحَّد عند الرسم).

    include_stored=True يضيف لكل شهر stored: {currency: {expense, income}} بمقدار
    المبالغ المحوَّلة بعملة الأساس لحظة التسجيل (struct الدقة التاريخية)، مع
    stored_base: العملة الأساس المعتمدة — تُستخدم في الرسم إن طابقت العملة المطلوبة.
    """
    from app.config import settings
    from app.timeutil import now_local, to_local_naive, to_utc_naive

    months = max(1, int(months))
    local_now = now_local()
    # نبدأ من أول الشهر الحالي ونرجع months × 30 يوم تقريبًا لتغطية شهور كاملة
    months_labels = []
    y, m = local_now.year, local_now.month
    for _ in range(months):
        months_labels.append((y, m))
        if m == 1:
            y, m = y - 1, 12
        else:
            m -= 1
    months_labels.reverse()

    start_local = months_labels[0]
    start = to_utc_naive(
        datetime(
            start_local[0],
            start_local[1],
            1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    )

    base_at_call = (settings.base_currency or "").upper().strip()
    cols = [
        Transaction.type,
        Transaction.currency,
        Transaction.created_at,
        Transaction.amount,
        Transaction.amount_in_base_currency,
        Transaction.base_currency_at_creation,
    ]
    # نجلب الصفوف ونجمّعها في Python بالتوقيت المحلي (لأن SQLite بلا منطقة زمنية)
    rows = (
        db.query(*cols)
        .filter(
            Transaction.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id)),
            Transaction.deleted_at.is_(None),
            Transaction.created_at >= start,
        )
        .all()
    )

    # تجميع (شهر، عملة) لكل نوع
    agg = {}  # month_key -> {currency: {expense: Decimal, income: Decimal}}
    stored_agg = {}  # month_key -> {currency: {expense/income بعملة الأساس}}
    for (
        tx_type,
        currency,
        created_at,
        amount,
        amount_in_base,
        base_at_creation,
    ) in rows:
        local_dt = to_local_naive(created_at)
        key = local_dt.strftime("%Y-%m")
        cur_name = currency or "غير محددة"
        per_cur = agg.setdefault(
            key, {}
        ).setdefault(cur_name, {"expense": Decimal("0"), "income": Decimal("0")})
        per_cur[tx_type] = per_cur.get(tx_type, Decimal("0")) + (amount or Decimal("0"))
        if (
            include_stored
            and base_at_creation == base_at_call
            and amount_in_base is not None
        ):
            stored_cur = stored_agg.setdefault(
                key, {}
            ).setdefault(cur_name, {"expense": Decimal("0"), "income": Decimal("0")})
            stored_cur[tx_type] = stored_cur.get(tx_type, Decimal("0")) + Decimal(
                str(amount_in_base)
            )

    out = []
    for y_local, m_local in months_labels:
        key = f"{y_local:04d}-{m_local:02d}"
        cur_data = agg.get(key, {})
        entry = {
            "month_key": key,
            "label": f"{m_local:02d}/{y_local}",
            "by_currency": cur_data or {},
        }
        if include_stored:
            entry["stored"] = stored_agg.get(key, {})
            entry["stored_base"] = base_at_call
        out.append(entry)
    return out


def user_ids_with_data(db: Session) -> list[int]:
    """كل المستخدمين الذين لديهم أي بيانات (معاملات/مهام/ملاحظات)."""
    ids = set()
    for model in (Transaction, Task, Note):
        rows = db.query(model.telegram_user_id).filter(model.deleted_at.is_(None)).distinct().all()
        ids.update(uid for (uid,) in rows)
    return sorted(ids)


# ---------- سجل التصحيحات (تحليل خاطئ — مراجعة يدوية دورية) ----------


def record_correction_feedback(
    db: Session,
    telegram_user_id: int,
    raw_message: str | None,
    source: str,
    data_type: str | None = None,
) -> CorrectionFeedback | None:
    """يسجّل رسالة كان تحليلها خاطئًا (إلغاء / رفض تأكيد) لمراجعتها يدويًا.

    source: "cancel" (ألغى المستخدم أثناء الجمع) أو "reject_confirm" (رفض
    شاشة التأكيد). النص يُخزَّن مشفّرًا؛ لا يُمسح من الجدول تلقائيًا.
    """
    if not raw_message or not raw_message.strip():
        return None
    fb = CorrectionFeedback(
        telegram_user_id=telegram_user_id,
        source=source,
        raw_message=raw_message,
        data_type=(data_type or None),
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    from app.admin import clear_admin_cache

    clear_admin_cache()  # عدد "بانتظار المراجعة" في admin_stats يتغيّر
    return fb


def list_correction_feedback(
    db: Session,
    only_unreviewed: bool = True,
    limit: int = 50,
) -> list[CorrectionFeedback]:
    """يعيد سجلات التحليل الخاطئ (الأحدث أولًا) للمراجعة اليدوية."""
    q = db.query(CorrectionFeedback)
    if only_unreviewed:
        q = q.filter(CorrectionFeedback.reviewed.is_(False))
    return q.order_by(CorrectionFeedback.created_at.desc()).limit(limit).all()


def mark_correction_reviewed(db: Session, feedback_id: int) -> bool:
    """يعلّم سجلًا كمراجَع يدويًا (لم يعد يظهر في القوائم)."""
    row = db.query(CorrectionFeedback).filter(CorrectionFeedback.id == feedback_id).first()
    if row is None:
        return False
    row.reviewed = True
    db.commit()
    from app.admin import clear_admin_cache

    clear_admin_cache()
    return True


# ---------- إحصائيات الاستخدام للمستخدم (/stats) ----------


def db_size_bytes() -> int | None:
    """حجم ملف قاعدة البيانات (بايت) لـ SQLite، أو None لغيره/تعذّر القراءة."""
    import os

    from app.database.db import DATABASE_URL

    if not DATABASE_URL.startswith("sqlite"):
        return None
    path = DATABASE_URL.replace("sqlite:///", "", 1)
    if not path or path == ":memory:":
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def user_stats(db: Session, telegram_user_id: int) -> dict:
    """إحصائيات مخطط الاستخدام حسب مساحة عمل المستخدم — بلا أي شبكة."""
    from app.timeutil import now_utc, to_local_naive

    uids = accessible_user_ids(db, telegram_user_id)
    empty_uids = uids if uids else {-1}

    def _counts(model, **filters) -> int:
        q = db.query(model).filter(model.telegram_user_id.in_(empty_uids))
        for col, value in filters.items():
            q = q.filter(getattr(model, col) == value)
        return q.count()

    tx_base = db.query(Transaction).filter(
        Transaction.telegram_user_id.in_(empty_uids),
        Transaction.deleted_at.is_(None),
    )
    expense_count = tx_base.filter(Transaction.type == "expense").count()
    total_tx = tx_base.count()
    income_count = total_tx - expense_count

    def _sum(rows) -> dict:
        agg = {}
        for amount, currency in rows:
            cur = currency or "غير محددة"
            agg[cur] = agg.get(cur, Decimal("0")) + Decimal(str(amount or 0))
        return agg

    expenses_rows = [
        r
        for r in db.query(Transaction.amount, Transaction.currency)
        .filter(
            Transaction.telegram_user_id.in_(empty_uids),
            Transaction.deleted_at.is_(None),
            Transaction.type == "expense",
        )
        .all()
    ]
    incomes_rows = [
        r
        for r in db.query(Transaction.amount, Transaction.currency)
        .filter(
            Transaction.telegram_user_id.in_(empty_uids),
            Transaction.deleted_at.is_(None),
            Transaction.type == "income",
        )
        .all()
    ]

    # ساعة الذروة خلال آخر 90 يومًا (بالتوقيت المحلي)
    cutoff = now_utc() - timedelta(days=90)
    hour_clock = {}
    for (created_at,) in (
        db.query(Transaction.created_at)
        .filter(
            Transaction.telegram_user_id.in_(empty_uids),
            Transaction.deleted_at.is_(None),
            Transaction.created_at >= cutoff,
        )
        .all()
    ):
        local_dt = to_local_naive(created_at)
        if local_dt is not None:
            h = local_dt.hour
            hour_clock[h] = hour_clock.get(h, 0) + 1
    peak_hour = max(hour_clock, key=hour_clock.get) if hour_clock else None

    month_entries = monthly_totals(db, telegram_user_id, months=1, include_stored=True)
    current_month = month_entries[-1] if month_entries else {}

    return {
        "workspace_size": len(uids),
        "transactions": {
            "total": total_tx,
            "expense": expense_count,
            "income": income_count,
        },
        "expenses_by_currency": _sum(expenses_rows),
        "incomes_by_currency": _sum(incomes_rows),
        "orders": {
            "open": _counts(Note, note_type="order", status="open", deleted_at=None),
            "done": _counts(Note, note_type="order", status="done", deleted_at=None),
        },
        "notes": _counts(Note, deleted_at=None),
        "invoices": {
            "total": _counts(Invoice),
            "pending": _counts(Invoice, status="pending"),
            "paid": _counts(Invoice, status="paid"),
            "overdue": _counts(Invoice, status="overdue"),
        },
        "tasks": {
            "pending": _counts(Task, status="pending", deleted_at=None),
            "done": _counts(Task, status="done", deleted_at=None),
        },
        "budgets": _counts(Budget),
        "credit_limits": _counts(CreditLimit),
        "current_month": current_month,
        "peak_hour_local": peak_hour,
        "peak_activity": hour_clock.get(peak_hour, 0) if peak_hour is not None else 0,
        "db_size_bytes": db_size_bytes(),
    }
