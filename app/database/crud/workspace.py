"""
مساحات العمل المشتركة: التعريف، إنشاء/دعوة/إزالة/نقل الملكية، وفحص الصلاحيات.

الانضمام بموافقة صريحة: `invite_to_workspace` تُنشئ صفًا بحالة "pending"
(دعوة معلّقة) لا يمنح أي وصول؛ فقط `accept_workspace_invite` من الطرف المدعو
يقلبها إلى "active" ويُدمج بيانات الطرفين. الصفوف القديمة (بلا الحقل) تبقى
"active" — الترتيبات القائمة لا تتغير.
"""
from sqlalchemy.orm import Session

from app.database.crud.common import _invalidate_caches
from app.database.models import (
    WorkspaceMember,
)
from app.timeutil import now_utc

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"


def workspace_for_user(db: Session, telegram_user_id: int) -> int | None:
    """معرّف مساحة العمل التي ينتمي إليها المستخدم فعليًا (أو None).

    لا تُحتسب الدعوات المعلّقة: المدعو يبقى فرديًا حتى يقبل صراحةً.
    """
    row = (
        db.query(WorkspaceMember.workspace_id)
        .filter(
            WorkspaceMember.telegram_user_id == telegram_user_id,
            WorkspaceMember.status == STATUS_ACTIVE,
        )
        .first()
    )
    return row[0] if row else None

def workspace_member_ids(db: Session, workspace_id: int) -> set[int]:
    """معرّفات الأعضاء النشطين فقط (لا تشمل الدعوات المعلّقة)."""
    rows = (
        db.query(WorkspaceMember.telegram_user_id)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.status == STATUS_ACTIVE,
        )
        .all()
    )
    return {uid for (uid,) in rows}

def accessible_user_ids(db: Session, telegram_user_id: int) -> set[int]:
    """مجموعة المعرّفات التي يرى المستخدم بياناتها (مشتركة أم فردية).

    فردي: {نفسه} فقط — سلوك اليوم تمامًا.
    عضو مساحة: كل أعضاء المساحة. يعطي "حسابًا مشتركًا" بلا تحرّك بيانات.
    """
    from app.database.crud import workspace_for_user, workspace_member_ids
    wid = workspace_for_user(db, telegram_user_id)
    if wid is None:
        return {telegram_user_id}
    members = workspace_member_ids(db, wid)
    members.add(telegram_user_id)  # أمان إضافي لو لا يرتبط الصف بعد
    return members

def is_workspace_owner(db: Session, telegram_user_id: int) -> bool:
    """هل المستخدم هو مرتكز (مالك) مساحة العمل الحالية؟"""
    from app.database.crud import workspace_for_user
    wid = workspace_for_user(db, telegram_user_id)
    return wid is not None and wid == telegram_user_id

def create_workspace(db: Session, owner_telegram_user_id: int) -> WorkspaceMember:
    """ينشئ مساحة عمل للمستخدم (مرتكزها معرّفه) — idempotent.

    يُستدعى تلقائيًا عند أول طلب إنشاء مشاركة؛ القيم الفردية لا تحتاج أي
    إنشاء (سلوك اليوم بلا مساحات). لو وُجدت دعوة معلّقة عند المستخدم تُلغى
    (إنشاء مساحته الخاصة يعادل رفضها).
    """
    row = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.telegram_user_id == owner_telegram_user_id,
            WorkspaceMember.status == STATUS_ACTIVE,
        )
        .first()
    )
    if row is not None:
        return row
    decline_workspace_invite(db, owner_telegram_user_id)
    row = WorkspaceMember(
        telegram_user_id=owner_telegram_user_id,
        workspace_id=owner_telegram_user_id,
        status=STATUS_ACTIVE,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row

def invite_to_workspace(
    db: Session, owner_telegram_user_id: int, new_telegram_user_id: int
) -> bool:
    """يدعو المالك معرّفًا إلى مساحته (دعوة معلّقة — لا وصول حتى القبول).

    لا يمنح الصف المعلّق أي رؤية للبيانات؛ القبول يتم عبر accept_workspace_invite.
    يرفض دعوة مستخدم هو عضو نشط في مساحة أخرى (لا سحب دون موافقة). يعيد
    False لغير المالك/ذاتي/العضو في مساحة أخرى.
    """
    from app.database.crud import workspace_for_user
    wid = workspace_for_user(db, owner_telegram_user_id)
    if wid != owner_telegram_user_id or new_telegram_user_id == owner_telegram_user_id:
        return False
    current = workspace_for_user(db, new_telegram_user_id)
    if current is not None and current != wid:
        return False
    row = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.telegram_user_id == new_telegram_user_id)
        .first()
    )
    if row is None:
        row = WorkspaceMember(
            telegram_user_id=new_telegram_user_id,
            workspace_id=wid,
            status=STATUS_PENDING,
        )
        db.add(row)
    elif row.status == STATUS_PENDING or row.workspace_id != wid:
        row.workspace_id = wid
        row.status = STATUS_PENDING
    db.commit()
    return True

def pending_workspace_invite(db: Session, telegram_user_id: int) -> int | None:
    """معرّف المساحة التي يدعو إليه هذا المستخدم حاليًا (دعوة معلّقة) أو None."""
    row = (
        db.query(WorkspaceMember.workspace_id)
        .filter(
            WorkspaceMember.telegram_user_id == telegram_user_id,
            WorkspaceMember.status == STATUS_PENDING,
        )
        .first()
    )
    return row[0] if row else None

def accept_workspace_invite(
    db: Session, telegram_user_id: int, workspace_id: int
) -> bool:
    """قبول صريح من المدعو → يصير عضوًا نشطًا (بيانات الطرفين تُدمج)."""
    row = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.telegram_user_id == telegram_user_id,
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.status == STATUS_PENDING,
        )
        .first()
    )
    if row is None:
        return False
    row.status = STATUS_ACTIVE
    row.joined_at = now_utc()
    db.commit()
    _invalidate_caches(db, workspace_id)
    return True

def decline_workspace_invite(
    db: Session, telegram_user_id: int, workspace_id: int | None = None
) -> bool:
    """رفض/إلغاء الدعوة المعلّقة — تُحذف من الجدول فلا يبقى أي أثر."""
    q = db.query(WorkspaceMember).filter(
        WorkspaceMember.telegram_user_id == telegram_user_id,
        WorkspaceMember.status == STATUS_PENDING,
    )
    if workspace_id is not None:
        q = q.filter(WorkspaceMember.workspace_id == workspace_id)
    deleted = q.delete()
    db.commit()
    if deleted and workspace_id is not None:
        _invalidate_caches(db, workspace_id)
    return deleted > 0

def remove_from_workspace(
    db: Session, owner_telegram_user_id: int, target_telegram_user_id: int
) -> bool:
    """يُخرج المالك عضوًا من مساحته (لا يمكن للمالك إخراج نفسه)."""
    from app.database.crud import workspace_for_user
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
    members = workspace_member_ids(db, wid)
    db.delete(row)
    db.commit()
    for uid in members:
        _invalidate_caches(db, uid)
    return True

def leave_workspace(db: Session, telegram_user_id: int) -> bool:
    """المستخدم يغادر مساحته الحالية (المالك لا يغادر — لا مكان لمدير مساحته)."""
    from app.database.crud import workspace_for_user
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
    members = workspace_member_ids(db, wid)
    db.delete(row)
    db.commit()
    for uid in members:
        _invalidate_caches(db, uid)
    return True

def list_workspace(db: Session, telegram_user_id: int) -> dict | None:
    """معلومات مساحة العمل الحالية: {workspace_id, members, pending, owner} أو None."""
    from app.database.crud import workspace_for_user, workspace_member_ids
    wid = workspace_for_user(db, telegram_user_id)
    if wid is None:
        return None
    return {
        "workspace_id": wid,
        "owner": wid == telegram_user_id,
        "members": sorted(workspace_member_ids(db, wid)),
        "pending": sorted(
            uid
            for (uid,) in db.query(WorkspaceMember.telegram_user_id)
            .filter(
                WorkspaceMember.workspace_id == wid,
                WorkspaceMember.status == STATUS_PENDING,
            )
            .all()
        ),
    }

def transfer_workspace_ownership(
    db: Session, current_owner: int, new_owner: int
) -> bool:
    """ينقل ملكية المساحة إلى عضو قائم فيها — يصبح هو مرتكزها الجديد.

    مفيد عند التنحي/تسليم الإدارة: كل صفوف المساحة تُعاد توجيهها إلى
    workspace_id == new_owner. يعيد False لغير المالك أو لعضو ليس في المساحة.
    """
    from app.database.crud import workspace_for_user
    wid = workspace_for_user(db, current_owner)
    if wid != current_owner or new_owner == current_owner:
        return False
    target = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.telegram_user_id == new_owner,
            WorkspaceMember.workspace_id == wid,
            WorkspaceMember.status == STATUS_ACTIVE,
        )
        .first()
    )
    if target is None:
        return False
    db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == wid).update(
        {"workspace_id": new_owner}
    )
    db.commit()
    _invalidate_caches(db, new_owner)
    return True

def dissolve_workspace(db: Session, owner: int) -> bool:
    """يفكّ المساحة المشتركة كليًا — كل عضو يعود مستخدمًا فرديًا.

    بيانات الأعضاء (معاملات/مهام/ملاحظات) تبقى موجودة بلا أي فقدان؛ يفقد فقط
    الربط المشترك. يعيد False إن لم يكن المستخدم مالكًا لأي مساحة.
    """
    from app.database.crud import workspace_for_user
    wid = workspace_for_user(db, owner)
    if wid != owner:
        return False
    members = workspace_member_ids(db, wid)
    deleted = db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == wid).delete()
    db.commit()
    for uid in members:
        _invalidate_caches(db, uid)
    return deleted > 0

def can_manage_records(db: Session, telegram_user_id: int) -> bool:
    """صلاحيات حذف/تعديل السجلات: الأفراد دائمًا نعم؛ أعضاء مساحة مشتركة —
    المرتكز (المالك) فقط (أعضاء عاديون يسجّلون ويقرؤون لكن لا يمسحون/يعدّلون)."""
    from app.database.crud import workspace_for_user
    wid = workspace_for_user(db, telegram_user_id)
    if wid is None:
        return True
    return wid == telegram_user_id
