"""الشركات: إنشاء، عضويات، دعوات، وفحص الصلاحيات."""
import secrets
import threading
import time as _time_mod
from datetime import timedelta

from sqlalchemy.orm import Session

from app.database.models import Company, CompanyMember, InviteLink
from app.permissions import (
    ASSIGNABLE_BY_MANAGER,
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_PERMISSIONS,
    role_has,
)
from app.timeutil import now_utc

# ذاكرة قصيرة لـ company_id لتقليل N+1 (budget_check و credit_check كانا يستعلمان
# company_id_for_user لكل ميزانية/حد — 1000 استعلام متتالٍ). TTL=60ث ويُبطل عند الكتابة.
# محمية بقفل مثل app/cache.py — تُقرأ/تُكتب من asyncio.to_thread (threadpool)
# لذا أي وصول بلا قفل قد يرفع RuntimeError: dictionary changed size during iteration
_lock = threading.RLock()
_CID_CACHE: dict[int, tuple[int | None, float]] = {}
_CID_TTL = 60.0
_MEMBER_CACHE: dict[int, tuple[CompanyMember | None, float]] = {}


def _cid_cache_get(uid: int) -> int | None | object:
    with _lock:
        ent = _CID_CACHE.get(uid)
        if ent is None:
            return None  # sentinel للتمييز بين miss و None
        cid, ts = ent
        if _time_mod.time() - ts < _CID_TTL:
            return cid
        _CID_CACHE.pop(uid, None)
        return None


def _cid_cache_set(uid: int, cid: int | None) -> None:
    with _lock:
        _CID_CACHE[uid] = (cid, _time_mod.time())


def invalidate_company_cache(telegram_user_id: int | None = None) -> None:
    """يبطل ذاكرة الشركة لعضو واحد أو الكل (يُستدعى بعد أي كتابة تغيّر العضوية)."""
    with _lock:
        if telegram_user_id is None:
            _CID_CACHE.clear()
            _MEMBER_CACHE.clear()
        else:
            _CID_CACHE.pop(telegram_user_id, None)
            _MEMBER_CACHE.pop(telegram_user_id, None)


def get_company_member(db: Session, telegram_user_id: int) -> CompanyMember | None:
    # استخدم ذاكرة قصيرة (نفس TTL) — العضوية لا تتغير كل ثانية
    # محمية بقفل مثل app/cache.py
    with _lock:
        ent = _MEMBER_CACHE.get(telegram_user_id)
        if ent is not None:
            cached, ts = ent
            if _time_mod.time() - ts < _CID_TTL:
                # تحقق أن الكائن لا يزال مرتبطًا بجلسة صالحة؟ نعيد نفس الكائن (قراءة فقط)
                return cached
            _MEMBER_CACHE.pop(telegram_user_id, None)
    res = (
        db.query(CompanyMember)
        .filter(CompanyMember.telegram_user_id == telegram_user_id, CompanyMember.status == "active")
        .first()
    )
    with _lock:
        _MEMBER_CACHE[telegram_user_id] = (res, _time_mod.time())
        # _cid_cache_set يجلب القفل داخليًا (RLock يسمح بالتداخل)
        _cid_cache_set(telegram_user_id, res.company_id if res else None)
    return res


def company_id_for_user(db: Session, telegram_user_id: int) -> int | None:
    # جرّب الذاكرة أولًا دون استعلام — محمية بقفل
    with _lock:
        if telegram_user_id in _CID_CACHE:
            ent = _CID_CACHE.get(telegram_user_id)
            if ent is not None:
                cid, ts = ent
                if _time_mod.time() - ts < _CID_TTL:
                    return cid
                _CID_CACHE.pop(telegram_user_id, None)
    m = get_company_member(db, telegram_user_id)
    # get_company_member يحدّث الذاكرة بالفعل
    return m.company_id if m else None


def get_company(db: Session, company_id: int) -> Company | None:
    return db.query(Company).filter(Company.id == company_id).first()


def company_member_ids(db: Session, company_id: int) -> set[int]:
    rows = (
        db.query(CompanyMember.telegram_user_id)
        .filter(CompanyMember.company_id == company_id, CompanyMember.status == "active")
        .all()
    )
    return {uid for (uid,) in rows}


def _backfill_company_id_for_user(db: Session, telegram_user_id: int, company_id: int) -> int:
    """ينقل السجلات القديمة (company_id IS NULL) للمستخدم إلى شركته الجديدة.

    يُستدعى عند إنشاء شركة (بما فيها "فردي") أو عند الانضمام بموافقة،
    لمنع اختفاء السجلات القديمة من القوائم/التقارير/التصدير/الميزانيات.
    يعيد عدد الصفوف المحدّثة.
    """
    from app.database.models import (
        Budget,
        CreditLimit,
        EmployeeBonusPlan,
        Invoice,
        LoyaltyAccount,
        Note,
        Task,
        Transaction,
    )

    total = 0
    for model in (Transaction, Note, Task, Budget, CreditLimit, Invoice, EmployeeBonusPlan, LoyaltyAccount):
        if not hasattr(model, "company_id") or not hasattr(model, "telegram_user_id"):
            continue
        try:
            updated = (
                db.query(model)
                .filter(model.telegram_user_id == telegram_user_id, model.company_id.is_(None))
                .update({model.company_id: company_id}, synchronize_session=False)
            )
            total += int(updated or 0)
        except Exception:
            # تجاهل أخطاء الجداول التي قد لا تحتوي العمود بعد (قبل الهجرة)
            db.rollback()
    # LoyaltyConfig لا يحوي company_id per-user بل per-telegram_user_id فقط، لا حاجة
    return total


def create_company(
    db: Session,
    owner_id: int,
    name: str,
    description: str | None = None,
    business_type: str | None = None,
    base_currency: str | None = None,
) -> Company | None:
    """ينشئ شركة + عضوية owner. يرفض إن كان المستخدم عضوًا في شركة."""
    from app.database.crud import _clean_text, _invalidate_caches, normalize_currency

    name = _clean_text(name)
    if not name or company_id_for_user(db, owner_id) is not None:
        return None
    # دعوة معلّقة قديمة عند نفس المستخدم تُلغى
    db.query(CompanyMember).filter(
        CompanyMember.telegram_user_id == owner_id,
        CompanyMember.status == "pending",
    ).delete()
    company = Company(
        name=name[:150],
        description=_clean_text(description),
        business_type=business_type,
        base_currency=normalize_currency(base_currency) if base_currency else None,
        owner_telegram_user_id=owner_id,
    )
    db.add(company)
    db.flush()  # للحصول على id
    db.add(CompanyMember(telegram_user_id=owner_id, company_id=company.id, role=ROLE_OWNER, status="active"))
    # نقل السجلات القديمة (NULL) إلى الشركة الجديدة حتى لا تختفي
    try:
        _backfill_company_id_for_user(db, owner_id, company.id)
    except Exception:
        # لا نكسر إنشاء الشركة بسبب فشل النقل
        try:
            db.rollback()
            db.add(company)
            db.add(CompanyMember(telegram_user_id=owner_id, company_id=company.id, role=ROLE_OWNER, status="active"))
            db.commit()
            db.refresh(company)
        except Exception:
            pass
    db.commit()
    db.refresh(company)
    _invalidate_caches(db, owner_id)
    return company


def has_permission(db: Session, telegram_user_id: int, permission: str) -> bool:
    """فرد بلا شركة = كل الصلاحيات على بياناته (سلوك اليوم)."""
    m = get_company_member(db, telegram_user_id)
    if m is None:
        return True
    return role_has(m.role, permission)


def report_scope_ids(db: Session, telegram_user_id: int) -> set[int]:
    """معرّفات التقارير: كامل الشركة لمن عنده report.view_all، وإلا نفسه فقط."""
    from app.database.crud import accessible_user_ids

    if has_permission(db, telegram_user_id, "report.view_all"):
        return accessible_user_ids(db, telegram_user_id)
    return {telegram_user_id}


def report_company_filter(db: Session, telegram_user_id: int, model) -> object:
    """فلتر التقارير: كامل الشركة (company_id) لمن عنده view_all، وإلا سجلاته فقط."""
    if has_permission(db, telegram_user_id, "report.view_all"):
        return company_filter(db, telegram_user_id, model)
    return model.telegram_user_id == telegram_user_id


def can_edit_record(db: Session, actor_id: int, owner_id: int | None) -> bool:
    """هل يمكن للممثل تعديل سجل يملكه owner_id؟"""
    # إذا كان المستخدم في شركة، نستخدم صلاحيات الشركة الدقيقة
    if company_id_for_user(db, actor_id) is not None:
        if has_permission(db, actor_id, "record.edit_any"):
            return True
        if has_permission(db, actor_id, "record.edit_own") and owner_id is not None and actor_id == owner_id:
            return True
        return False
    # فردي أو مساحة عمل قديمة: سلوك اليوم - المرتكز فقط يعدّل
    from app.database.crud import can_manage_records

    return can_manage_records(db, actor_id)


def company_filter(db: Session, telegram_user_id: int, model) -> object:
    """فلتر توافقي: للشركات يطابق company_id فقط، وللأفراد يطابق telegram_user_id.

    سابقًا كان هناك fallback `OR company_id IS NULL` الذي كان يسرب بيانات قديمة
    (سجلات قبل الانضمام لشركة) إلى أعضاء الشركة الجدد بعد الانضمام — تسريب بيانات.
    الآن للشركات نطابق فقط company_id == cid (لا تسريب)، وللأفراد نطابق
    telegram_user_id عبر accessible_user_ids.
    """
    from app.database.crud import accessible_user_ids

    cid = company_id_for_user(db, telegram_user_id)
    if cid is None:
        return model.telegram_user_id.in_(accessible_user_ids(db, telegram_user_id))
    # شركة: نطابق فقط سجلات الشركة (لا fallback إلى company_id IS NULL)
    return model.company_id == cid


def get_user_base_currency(db: Session, telegram_user_id: int) -> str:
    """يعيد العملة الأساسية للمستخدم: عملة شركته إن وجدت وإلا settings.base_currency."""
    from app.config import settings

    cid = company_id_for_user(db, telegram_user_id)
    if cid is not None:
        comp = get_company(db, cid)
        if comp and comp.base_currency:
            return comp.base_currency.upper().strip()
    return (settings.base_currency or "ILS").upper().strip()


# ---------- الدعوات ----------


def _new_token(kind: str) -> str:
    if kind == "code":
        # 8 أرقام → 100M احتمال (كان 6 أرقام → 1M فقط) + فترة قصيرة
        return f"{secrets.randbelow(10**8):08d}"
    return secrets.token_urlsafe(16)  # 22 حرفًا — يناسب حد start payload (64)


def create_invite(
    db: Session,
    creator_id: int,
    role: str,
    kind: str = "link",
    max_uses: int = 1,
    ttl_hours: int | None = 72,
) -> InviteLink | None:
    m = get_company_member(db, creator_id)
    if m is None or not role_has(m.role, "members.manage"):
        return None
    if role not in ROLE_PERMISSIONS or role == ROLE_OWNER:
        return None
    if m.role == ROLE_MANAGER and role not in ASSIGNABLE_BY_MANAGER:
        return None
    if kind == "code":
        ttl_hours, max_uses = 1, 1  # الكود 8 أرقام/ساعة واحدة فقط — كان 24 ساعة وتناقض الرسالة
    for _ in range(5):  # إعادة المحاولة عند تصادم الكود
        token = _new_token(kind)
        if not db.query(InviteLink).filter(InviteLink.token == token).first():
            break
    else:
        return None
    try:
        from app.security import hash_token
        token_hash = hash_token(token)
    except Exception:
        token_hash = None
    inv = InviteLink(
        company_id=m.company_id,
        token=token,
        token_hash=token_hash,
        kind=kind,
        role=role,
        max_uses=max(1, int(max_uses)),
        expires_at=(now_utc() + timedelta(hours=ttl_hours)) if ttl_hours else None,
        created_by=creator_id,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def redeem_invite(
    db: Session, telegram_user_id: int, token: str, display_name: str | None = None
) -> tuple[bool, str | Company]:
    """يستهلك دعوة. يعيد (True, Company) أو (False, رسالة_خطأ).

    حدّ موحّد للمحاولات داخل الدالة نفسها — يحمي كل مسارات /join و onboarding
    و /start inv_... من التخمين (كان الحماية في /join فقط)."""
    from app.database.crud import _invalidate_caches

    def _clean_invite_token(t: str | None) -> str:
        if not t:
            return ""
        try:
            from app.rtl import strip_bidi_marks as _strip
        except Exception:
            import re as _re
            _bidi_re = _re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
            def _strip(x: str) -> str:  # type: ignore
                return _bidi_re.sub("", x)
        cleaned = _strip(str(t))
        cleaned = cleaned.strip().strip("`").strip()
        cleaned = "".join(cleaned.split())
        return cleaned

    # تنظيف الرمز من RLM/LRM قبل أي معالجة (نسخ من backticks يحملها)
    token = _clean_invite_token(token)

    # حدّ موحّد: فحص الحظر و is_rate_limited قبل أي معالجة
    try:
        from bot.ratelimit import is_join_blocked, is_rate_limited, register_join_denied
    except Exception:
        def is_join_blocked(uid):
            return False
        def is_rate_limited(uid):
            return False
        def register_join_denied(uid):
            return 0

    if is_join_blocked(telegram_user_id):
        return False, "تم حظرك مؤقتًا لكثرة المحاولات. حاول بعد 15 دقيقة."
    if is_rate_limited(telegram_user_id):
        return False, "أرسلت الكثير من المحاولات، انتظر قليلاً."

    if company_id_for_user(db, telegram_user_id) is not None:
        return False, "أنت عضو في شركة بالفعل."
    # تحقق عبر الهاش أولًا (لا يُخزن الخام طويلًا في المستقبل) ثم fallback للخام للتوافق
    try:
        from app.security import hash_token
        h = hash_token(token)
        inv = db.query(InviteLink).filter((InviteLink.token_hash == h) | (InviteLink.token == token)).first()
    except Exception:
        inv = db.query(InviteLink).filter(InviteLink.token == token).first()
    if inv is None or inv.revoked:
        try:
            register_join_denied(telegram_user_id)
        except Exception:
            pass
        return False, "الدعوة غير صالحة."
    if inv.expires_at and inv.expires_at < now_utc():
        try:
            register_join_denied(telegram_user_id)
        except Exception:
            pass
        return False, "انتهت صلاحية الدعوة."
    # تحديث ذرّي: يمنع تجاوز max_uses عند ضغطين متزامنين
    updated = (
        db.query(InviteLink).filter(InviteLink.id == inv.id, InviteLink.uses < InviteLink.max_uses).update({InviteLink.uses: InviteLink.uses + 1})
    )
    if updated != 1:
        db.rollback()
        return False, "استُنفدت استخدامات الدعوة."
    db.query(CompanyMember).filter(
        CompanyMember.telegram_user_id == telegram_user_id,
        CompanyMember.status == "pending",
    ).delete()
    db.add(
        CompanyMember(
            telegram_user_id=telegram_user_id,
            company_id=inv.company_id,
            role=inv.role,
            status="active",
            display_name=display_name,
            invited_by=inv.created_by,
        )
    )
    # نقل سجلاته القديمة (company_id IS NULL) إلى الشركة الجديدة
    try:
        _backfill_company_id_for_user(db, telegram_user_id, inv.company_id)
    except Exception:
        # لا نكسر الانضمام بسبب فشل النقل
        pass
    db.commit()
    _invalidate_caches(db, telegram_user_id)
    return True, get_company(db, inv.company_id)


def revoke_invite(db: Session, actor_id: int, invite_id: int) -> bool:
    m = get_company_member(db, actor_id)
    if m is None or not role_has(m.role, "members.manage"):
        return False
    n = (db.query(InviteLink).filter(InviteLink.id == invite_id, InviteLink.company_id == m.company_id).update({InviteLink.revoked: True}))
    db.commit()
    return n > 0


def set_member_role(db: Session, actor_id: int, target_id: int, role: str) -> bool:
    actor = get_company_member(db, actor_id)
    target = get_company_member(db, target_id)
    if not actor or not target or actor.company_id != target.company_id:
        return False
    if not role_has(actor.role, "members.manage") or target.role == ROLE_OWNER:
        return False
    if role not in ROLE_PERMISSIONS or role == ROLE_OWNER:
        return False
    if actor.role == ROLE_MANAGER and (role not in ASSIGNABLE_BY_MANAGER or target.role == ROLE_MANAGER):
        return False
    target.role = role
    db.commit()
    return True


def remove_company_member(db: Session, actor_id: int, target_id: int) -> bool:
    from app.database.crud import _invalidate_caches

    actor = get_company_member(db, actor_id)
    target = get_company_member(db, target_id)
    if not actor or not target or actor.company_id != target.company_id:
        return False
    if target.role == ROLE_OWNER or actor_id == target_id:
        return False
    if not role_has(actor.role, "members.manage"):
        return False
    if actor.role == ROLE_MANAGER and target.role == ROLE_MANAGER:
        return False
    members = company_member_ids(db, actor.company_id)
    db.delete(target)
    db.commit()
    for uid in members:
        _invalidate_caches(db, uid)
    return True


def update_company(
    db: Session, actor_id: int, company_id: int, name: str | None = None, description: str | None = None
) -> Company | None:
    """يحدّث اسم/وصف الشركة — يحتاج company.manage (المالك فقط حالياً)."""
    from app.database.crud import _clean_text

    m = get_company_member(db, actor_id)
    if m is None or m.company_id != company_id or not role_has(m.role, "company.manage"):
        return None
    comp = get_company(db, company_id)
    if comp is None:
        return None
    if name is not None:
        cleaned = _clean_text(name)
        if not cleaned:
            return None
        comp.name = cleaned[:150]
    if description is not None:
        # الوصف قد يكون فارغًا لمسحه
        cleaned_desc = _clean_text(description)
        comp.description = cleaned_desc
    from app.timeutil import now_utc

    comp.updated_at = now_utc()
    db.commit()
    db.refresh(comp)
    return comp


def transfer_company_ownership(db: Session, current_owner: int, new_owner: int) -> bool:
    """ينقل ملكية الشركة إلى عضو آخر — يحتاج company.manage."""
    actor = get_company_member(db, current_owner)
    target = get_company_member(db, new_owner)
    if not actor or not target or actor.company_id != target.company_id:
        return False
    if actor.role != ROLE_OWNER or target.role == ROLE_OWNER:
        return False
    # غيّر دور المالك القديم إلى manager
    comp = get_company(db, actor.company_id)
    if comp is None:
        return False
    actor.role = ROLE_MANAGER
    target.role = ROLE_OWNER
    comp.owner_telegram_user_id = new_owner
    from app.timeutil import now_utc

    comp.updated_at = now_utc()
    db.commit()
    from app.database.crud import _invalidate_caches

    for uid in company_member_ids(db, comp.id):
        _invalidate_caches(db, uid)
    return True


# ---------- توسع مستقبلي: شخص بأكثر من شركة ----------
# لتعدد الشركات للشخص الواحد:
# 1) غيّر PK لـ CompanyMember إلى (telegram_user_id, company_id) بدل telegram_user_id وحده
# 2) أضف active_company_id إلى UserPref (الشركة النشطة حالياً)
# 3) عدّل company_id_for_user لتقرأ active_company_id من UserPref بدل السطر الأول
# باقي الكود لا يتغير لأنه كله يمر من company_id_for_user / company_filter
# ---------------------------------------------------------------


def delete_company(db: Session, actor_id: int, company_id: int, confirm: bool = False) -> bool:
    """حذف الشركة — يتطلب تأكيد مزدوج. الحذف soft عبر حذف الأعضاء أولاً.

    الخطوة الأولى (confirm=False) تتحقق من الصلاحية فقط.
    الخطوة الثانية (confirm=True) تحذف فعليًا الشركة وكل عضوياتها ودعواتها.
    السجلات المالية تبقى ب company_id للمراجعة (لا تحذف).
    """
    m = get_company_member(db, actor_id)
    if m is None or m.company_id != company_id or not role_has(m.role, "company.manage"):
        return False
    if m.role != ROLE_OWNER:
        return False
    if not confirm:
        return False
    # حذف الدعوات
    db.query(InviteLink).filter(InviteLink.company_id == company_id).delete()
    # حذف العضويات
    members = company_member_ids(db, company_id)
    db.query(CompanyMember).filter(CompanyMember.company_id == company_id).delete()
    # حذف الشركة
    comp = get_company(db, company_id)
    if comp:
        db.delete(comp)
    db.commit()
    from app.database.crud import _invalidate_caches

    for uid in members:
        _invalidate_caches(db, uid)
    _invalidate_caches(db, actor_id)
    return True
