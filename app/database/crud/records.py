"""
العمليات المالية والملاحظات والاستعلام والتاريخ والتعديل والبحث (مع مسار الاستعلام المُجمَّع).
"""
import calendar
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import timeutil as _timeutil
from app.audit import log_audit
from app.cache import get_or_set
from app.config import settings
from app.database.crud.company import company_filter
from app.database.models import (
    Budget,
    CreditLimit,
    EmployeeBonusPlan,
    Invoice,
    LoyaltyAccount,
    Note,
    Party,
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
        _to_valid_amount,
        normalize_currency,
    )

    if _is_duplicate_message(db, Transaction, telegram_user_id, telegram_message_id):
        return None

    # العملة الأساسية للشركة (إن وجدت) وإلا العامة
    # لا نحتجز اتصال DB أثناء استدعاء الشبكة (10ث): اقرأ الأساس في جلسة قصيرة منفصلة
    # ثم أغلقها قبل convert (التي قد تضرب الشبكة). هذا يمنع نفاد QueuePool تحت ضغط.
    from app.database.crud.company import get_user_base_currency as _get_base

    try:
        from app.database.db import SessionLocal as _SL
        _tmp = _SL()
        try:
            user_base = _get_base(_tmp, telegram_user_id)
        finally:
            try:
                _tmp.close()
            except Exception:
                pass
    except Exception:
        user_base = _get_base(db, telegram_user_id)
    # العملة المُدخلة أولاً، ثم عملة الشركة كافتراضي — لكن لا تسقط العملات غير المعروفة بصمت
    # (يوان/روبل/BTC/ريال المبهمة...): إذا كانت العملة الخام غير مفهومة، اتركها None ليُسأل المستخدم
    # نتحقق من الحقل الأساسي ومن الحقول المحفوظة currency_raw/_raw_currency التي تضعها طبقات التطبيع
    raw_input = ""
    raw_cur = None
    primary_val = data.get("currency")
    if isinstance(primary_val, str) and primary_val.strip():
        raw_input = primary_val.strip()
        raw_cur = normalize_currency(primary_val)
    elif primary_val is not None and str(primary_val).strip():
        # قيمة غير نصية (نادر) — حوّلها لنص
        raw_input = str(primary_val).strip()
        raw_cur = normalize_currency(raw_input)
    # حقول خام محفوظة من app.validation / app.schemas عند سقوط العملة إلى None
    preserved_raw = None
    for _k in ("currency_raw", "_raw_currency", "raw_currency"):
        _v = data.get(_k)
        if isinstance(_v, str) and _v.strip():
            preserved_raw = _v.strip()
            break
        elif _v is not None and str(_v).strip():
            preserved_raw = str(_v).strip()
            break
    if not raw_input and preserved_raw:
        raw_input = preserved_raw
        raw_cur = normalize_currency(preserved_raw)
    # هل هناك إشارة صريحة أن العملة ناقصة/غير معروفة؟
    has_missing_currency_flag = "currency" in (data.get("missing_fields") or [])
    # تحديد إن كانت العملة غير معروفة: نستخدم القائمة البيضاء الصارمة من app.validation
    # (common.normalize قد يعيد الخام كما هو، لكننا نريد كشف يوان/BTC/IQD خارج القائمة)
    try:
        from app.validation import KNOWN_CURRENCIES as _KNOWN_STRICT
    except Exception:
        _KNOWN_STRICT = frozenset()
    def _is_known_code(code: str | None) -> bool:
        if not code:
            return False
        try:
            return code.upper() in _KNOWN_STRICT
        except Exception:
            return False
    is_unknown = False
    if raw_input:
        # إذا كان الخام موجودًا لكن الكود المطبّع ليس ضمن القائمة البيضاء → غير معروف
        if not _is_known_code(raw_cur):
            # تحقق إضافي: حتى لو أعاد common الخام نفسه (مثل "يوان")، نعتبره غير معروف
            is_unknown = True
        # الحالة الخاصة لـ "ريال" المجردة: common يعيد None صراحةً
        if raw_cur is None:
            is_unknown = True
    if preserved_raw and not _is_known_code(normalize_currency(preserved_raw)):
        # خام محفوظ غير معروف (بعد التطبيع) → بالتأكيد حالة غير معروفة
        is_unknown = True
    if has_missing_currency_flag and not _is_known_code(raw_cur):
        # flag يعني sanitize/schemas كشفت عملة غير مفهومة
        is_unknown = True
    if is_unknown:
        currency = None
    else:
        currency = raw_cur or user_base or (settings.base_currency or "").upper() or None
    # مرر العملة لـ _to_valid_amount لتحديد 3 منازل لـ JOD/KWD/BHD/OMR
    amount = _to_valid_amount(data.get("amount"), currency=currency)
    base_amount, base_at = _best_effort_base_amount(amount, currency, base_currency=user_base)

    vat_rate = _to_decimal(data.get("vat_rate"))
    vat_amount = _to_decimal(data.get("vat_amount"))
    # ضريبة قيمة مضافة حقيقية لا تتجاوز 100% — حد أعلى منطقي يطابق (وأشد من)
    # قيد العمود Numeric(6,3) الذي يقبل حتى 999.999؛ الفحص القديم سمح حتى 1000
    # فيتجاوز الحد التخزيني للعمود ويعطي تعارضًا بين القيد البرمجي وقيد القاعدة.
    if vat_rate is not None and (vat_rate < 0 or vat_rate > 100):
        # نسبة غير منطقية → تُسقط مع مبلغها: لا نُبقي مبلغ ضريبة بلا نسبة صالحة
        # (يطابق "لا تُقبل بيانات محاسبية متضاربة بصمت" الذي يُطبَّق أدناه).
        vat_rate = None
        vat_amount = None
    if vat_amount is not None and vat_amount < 0:
        vat_amount = None
    # اتساق المحاسبة: المبلغ شامل الضريبة، فعلى أي نسبة صحيحة
    # vat_amount = amount × rate / (100 + rate). قيم متضاربة stylان (مستخرَجة
    # بالذكاء الاصطناعي من صور) تُصحَّح تلقائيًا — لا تُقبل بصمت بيانات متضاربة.
    if (
        vat_rate is not None
        and vat_amount is not None
        and amount is not None
        and amount > 0
    ):
        expected = (amount * vat_rate / (Decimal("100") + vat_rate)).quantize(Decimal("0.01"))
        tolerance = abs(expected) * Decimal("0.04") + Decimal("0.05")
        if abs(vat_amount - expected) > tolerance:
            vat_amount = expected

    from app.database.crud.common import parse_date_local
    from app.database.crud.company import company_id_for_user as _cid_for

    _cid = _cid_for(db, telegram_user_id)
    # التاريخ المستخرج من الفاتورة أو "دفعت أمس" — يُستخدم كـ created_at بدل الآن
    # إن لم يُذكر تاريخ، يُترك None ليستخدم now_utc الافتراضي
    created_at = None
    raw_date = data.get("date")
    if isinstance(raw_date, str) and raw_date.strip():
        created_at = parse_date_local(raw_date.strip())
    if created_at is None and isinstance(raw_date, str) and raw_date.strip():
        # محاولة أخيرة عبر AI لتفسير التاريخ العربي الحر (غدًا/أمس)
        try:
            from app.database.crud.common import has_ai_consent
            from app.database.db import SessionLocal as _SLd
            _dbd = _SLd()
            try:
                _consent_d = has_ai_consent(_dbd, telegram_user_id)
            finally:
                _dbd.close()
            if _consent_d:
                import app.ai_service as _ai
                iso = _ai.interpret_arabic_date(raw_date.strip())
                if iso:
                    created_at = parse_date_local(iso)
        except Exception:
            pass
    if created_at is None:
        created_at = now_utc()
    # period close: منع إنشاء معاملة في فترة مغلقة
    try:
        from app.timeutil import to_local_naive

        _mk = to_local_naive(created_at).strftime("%Y-%m")
        if is_period_closed(db, telegram_user_id, _mk):
            raise ValueError("الفترة مغلقة — لا يمكن إضافة معاملة في شهر مغلق.")
    except ValueError:
        raise
    except Exception:
        pass
    # اتجاه الذمة (charge/settlement) حقل صريح مستقل عن النوع (expense/income).
    # الافتراضي "نقدي" (None) ما لم يُذكر "آجل/بالدين" صراحة.
    raw_direction = None
    for _k in ("ledger_kind", "ledger_entry", "debt_direction", "direction", "entry_type"):
        _v = data.get(_k)
        if _v is not None and str(_v).strip():
            raw_direction = _v
            break
    direction_val = None
    if isinstance(raw_direction, str):
        low = raw_direction.strip().lower()
        if low in ("charge", "آجل", "ذمة", "credit", "دين"):
            direction_val = "charge"
        elif low in ("settlement", "تسديد", "تسوية", "payment"):
            direction_val = "settlement"
        elif low in ("none", "null", "cash", "نقدي"):
            direction_val = None
    elif isinstance(raw_direction, bool):
        direction_val = "charge" if raw_direction else None

    # توافق is_credit القديم:
    raw_is_credit = data.get("is_credit")
    if direction_val is None and raw_is_credit is not None:
        if isinstance(raw_is_credit, bool):
            direction_val = "charge" if raw_is_credit else None
        elif isinstance(raw_is_credit, str):
            low = raw_is_credit.strip().lower()
            if low in ("true", "1", "yes", "credit", "آجل", "ذمة"):
                direction_val = "charge"
            elif low in ("false", "0", "no", "cash", "نقدي"):
                direction_val = None
        else:
            direction_val = "charge" if bool(raw_is_credit) else None

    # is_credit للتوافق (True إن كان قيد ذمة charge/settlement)
    is_credit_val = direction_val in ("charge", "settlement")

    # approval: مصروف فوق سقف الدور → pending
    approval_status_val = "approved"
    try:
        from app.database.crud.company import get_company, get_company_member
        m = get_company_member(db, telegram_user_id)
        if m is not None and data.get("type") == "expense" and amount is not None:
            comp = get_company(db, m.company_id) if m.company_id is not None else None
            limit = None
            if comp is not None:
                if m.role == "staff":
                    limit = comp.approval_limit_staff
                elif m.role == "manager":
                    limit = comp.approval_limit_manager
            if limit is not None and amount > limit:
                approval_status_val = "pending"
    except Exception:
        approval_status_val = "approved"

    transaction = Transaction(
        telegram_user_id=telegram_user_id,
        company_id=_cid,
        telegram_message_id=telegram_message_id,
        invoice_id=data.get("invoice_id"),
        type=data.get("type"),
        amount=amount,
        currency=currency,
        person=_clean_person(data.get("person")),
        category=_clean_text(data.get("category")),
        description=_clean_text(data.get("description")),
        raw_message=raw_message,
        created_at=now_utc(),
        is_credit=is_credit_val,
        approval_status=approval_status_val,
        debt_direction=direction_val,
        ledger_entry=direction_val,
        ledger_kind=direction_val,
        occurred_at=created_at,
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
    _accrue_loyalty_after_transaction(db, transaction)
    return transaction


def _accrue_loyalty_after_transaction(db: Session, transaction: Transaction | None) -> None:
    """تراكم تلقائي لنقاط الولاء عند تسجيل مبيع باسم عميل (ميزة حدث البون).

    يُستدعى بعد حفظ المعاملة؛ يفشل بصمت حتى لا يكسر تسجيل المعاملة نفسه
    (نقاط الولاء ترفيهية إضافية — ليست جوهرية لسجل العمليات).
    """
    if transaction is None:
        return
    try:
        from app.database.crud.bonus import accrue_loyalty_for_transaction

        accrue_loyalty_for_transaction(db, transaction)
    except Exception:
        db.rollback()
        return

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

    from app.database.crud.company import company_id_for_user as _cid_for

    _cid2 = _cid_for(db, telegram_user_id)
    # حفظ المبلغ للطلبية في الوصف إن وجد (النموذج بلا عمود amount)
    desc_raw = data.get("description") or data.get("raw") or raw_message
    if data.get("amount") is not None and data.get("type") == "order":
        amt = data.get("amount")
        cur = data.get("currency") or ""
        desc_raw = f"{desc_raw} — {amt} {cur}".strip(" —") if desc_raw else f"{amt} {cur}"
    note = Note(
        telegram_user_id=telegram_user_id,
        company_id=_cid2,
        telegram_message_id=telegram_message_id,
        note_type=data.get("type"),  # order | note
        description=_clean_text(desc_raw) or "ملاحظة",
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
    from app.database.crud.company import company_filter

    return (
        db.query(model)
        .filter(
            company_filter(db, telegram_user_id, model),
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

    يعيد dict: {"current": (start, end), "previous": (start, end),
                "partial": bool, "elapsed_pct": int}
    حيث partial=True إذا كانت الفترة الحالية ما زالت جارية (end = الآن، أي أقصر
    من فترة سابقة كاملة) — تُنبه الواجهة أن المقارنة الناتجة قد تكون مضلِّلة
    (يبدو الإنفاق أقل لمجرد أن الوقت لم يكتمل بعد). elapsed_pct نسبة ما مضى.
    """
    local_now = _timeutil.now_local()

    def _bounds(local_start_dt, local_end_dt):
        return (
            to_utc_naive(local_start_dt.replace(hour=0, minute=0, second=0, microsecond=0)),
            to_utc_naive(local_end_dt),
        )

    def _payload(cur_start, prev_start, total_seconds):
        elapsed = (local_now - cur_start).total_seconds()
        elapsed_pct = min(100, max(0, round(elapsed / total_seconds * 100)))
        return {
            "current": _bounds(cur_start, local_now),
            "previous": _bounds(prev_start, cur_start),
            "partial": elapsed_pct < 100,
            "elapsed_pct": elapsed_pct,
        }

    if period == "today":
        cur_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_start = cur_start - timedelta(days=1)
        return _payload(cur_start, prev_start, 86400)

    if period == "this_week":
        fd = _timeutil.first_day_of_week()
        weekday = local_now.weekday()
        cur_start = local_now - timedelta(days=(weekday - fd) % 7)
        cur_start = cur_start.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_start = cur_start - timedelta(days=7)
        return _payload(cur_start, prev_start, 7 * 86400)

    if period == "this_month":
        cur_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # نهاية الشهر السابق = بداية الشهر الحالي
        if cur_start.month == 1:
            prev_start = cur_start.replace(year=cur_start.year - 1, month=12)
        else:
            prev_start = cur_start.replace(month=cur_start.month - 1)
        days_in_month = calendar.monthrange(local_now.year, local_now.month)[1]
        return _payload(cur_start, prev_start, days_in_month * 86400)

    if period == "this_year":
        cur_start = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_start = cur_start.replace(year=cur_start.year - 1)
        # طول السنة الفعلي بالثواني — monthrange(year, 12)[1] يُعيد 31 (أيام ديسمبر)
        # وكان يجعل partial=False بعد أواخر يناير فيبقى تحذير "السنة الجارية" ميتًا.
        next_jan = cur_start.replace(year=cur_start.year + 1)
        total_seconds = (next_jan - cur_start).total_seconds()
        return _payload(cur_start, prev_start, total_seconds)

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
        list_overdue_tasks,
        list_pending_tasks,
        mark_overdue_tasks,
    )
    from app.database.crud.company import report_company_filter


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

    from app.database.crud.common import _cash_filter, _debt_filter

    base_filter = report_company_filter(db, telegram_user_id, Transaction)
    q = db.query(Transaction).filter(
        base_filter,
        Transaction.deleted_at.is_(None),
    )

    from sqlalchemy import func as _func
    if start:
        q = q.filter(_func.coalesce(Transaction.occurred_at, Transaction.created_at) >= start)
    if person:
        q = q.filter(Transaction.person == person)

    if metric == "total_expenses":
        expense_rows = q.filter(Transaction.type == "expense", _cash_filter(Transaction)).all()
        total = _sum_amounts_by_currency(expense_rows)
        from app.database.crud.company import get_user_base_currency as _get_base

        base_cur = _get_base(db, telegram_user_id)
        return {
            "metric": metric,
            "period": period,
            "person": person,
            "result": total,
            "unified_total": _unified_totals_for_rows(expense_rows, base_cur),
        }

    elif metric == "total_income":
        income_rows = q.filter(Transaction.type == "income", _cash_filter(Transaction)).all()
        total = _sum_amounts_by_currency(income_rows)
        from app.database.crud.company import get_user_base_currency as _get_base

        base_cur = _get_base(db, telegram_user_id)
        return {
            "metric": metric,
            "period": period,
            "person": person,
            "result": total,
            "unified_total": _unified_totals_for_rows(income_rows, base_cur),
        }

    elif metric == "count_transactions":
        count = q.count()
        return {"metric": metric, "period": period, "person": person, "result": count}

    elif metric == "person_balance":
        # رصيد مستحق مع شخص معيّن = إجمالي income (قيمة آجلة/ذمة: ما صار لك في ذمّته) - إجمالي expense (تسديد/دفع نقدي)
        # موجب = يدين لك، سالب = تدين له — موحّد مع person_debts و credit_usage
        # يُحتسب على الذمم فقط (is_credit) — النقدي لشخص لا يُعدّ دينًا
        if not person:
            return {
                "metric": metric,
                "period": period,
                "person": person,
                "result": None,
                "error": "no_person",
            }
        # رصيد الشخص = charges - settlements (settlement يُ flipping type)
        from sqlalchemy import func as _func2

        from app.database.crud.common import normalize_person_for_match as _npm
        debt_rows = db.query(Transaction).filter(
            report_company_filter(db, telegram_user_id, Transaction),
            Transaction.deleted_at.is_(None),
            _debt_filter(Transaction),
        )
        if start:
            debt_rows = debt_rows.filter(_func2.coalesce(Transaction.occurred_at, Transaction.created_at) >= start)
        debt_rows = debt_rows.all()
        debt_rows = [r for r in debt_rows if _npm(r.person) == _npm(person)]

        income_rows = []
        expense_rows = []
        for r in debt_rows:
            # settlement يعكس الاتجاه: income settlement → expense، expense settlement → income
            is_settlement = (r.debt_direction == "settlement" or r.ledger_entry == "settlement" or r.ledger_kind == "settlement")
            eff_type = r.type
            if is_settlement:
                eff_type = "income" if r.type == "expense" else "expense"
            if eff_type == "income":
                income_rows.append(r)
            else:
                expense_rows.append(r)

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
            from app.database.crud.common import _cash_filter as _cf2
            from app.database.crud.company import report_company_filter

            qq = db.query(Transaction).filter(
                report_company_filter(db, telegram_user_id, Transaction),
                Transaction.deleted_at.is_(None),
                Transaction.type == tx_type,
                _cf2(Transaction),
                _func.coalesce(Transaction.occurred_at, Transaction.created_at) >= lo,
                _func.coalesce(Transaction.occurred_at, Transaction.created_at) < hi,
            )
            if person:
                qq = qq.filter(Transaction.person == person)
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
            "partial": ranges.get("partial", False),
            "elapsed_pct": ranges.get("elapsed_pct", 100),
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
    """تراجع/يحذف (soft delete) آخر سجل أضافه المستخدم (عبر /undo)."""
    from app.database.crud import _invalidate_caches, can_manage_records, soft_delete_last


    if not can_manage_records(db, telegram_user_id):
        log_audit(telegram_user_id, "denied_undo", "workspace_role")
        return None

    candidates = []
    for model in (Transaction, Task, Note):
        row = soft_delete_last(db, model, telegram_user_id)
        if row is not None:
            # period close: تخطّي معاملة في فترة مغلقة
            if model is Transaction:
                try:
                    from app.timeutil import to_local_naive

                    dt = row.occurred_at if getattr(row, "occurred_at", None) is not None else row.created_at
                    if dt is not None and is_period_closed(db, telegram_user_id, to_local_naive(dt).strftime("%Y-%m")):
                        # استعادة (soft-delete لم يُطبَّق بعد? soft_delete_last may mark deleted_at? check)
                        continue
                except Exception:
                    pass
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
        telegram_user_id, "soft_delete", f"{model.__name__}:{row.id}"
    )
    _invalidate_caches(db, telegram_user_id)
    return {"kind": kind, "label": label}

def restore_last_deleted(db: Session, telegram_user_id: int) -> dict | None:
    """يستعيد أحدث سجل محذوف (soft-delete) — أمر /redo."""
    from app.database.crud import _invalidate_caches, accessible_user_ids, can_manage_records


    if not can_manage_records(db, telegram_user_id):
        log_audit(telegram_user_id, "denied_redo", "workspace_role")
        return None

    accessible_user_ids(db, telegram_user_id)
    candidates = []
    for model in (Transaction, Task, Note):
        row = (
            db.query(model)
            .filter(
                company_filter(db, telegram_user_id, model),
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
        telegram_user_id, "restore_record", f"{model.__name__}:{row.id}"
    )
    _invalidate_caches(db, telegram_user_id)
    return {"kind": kind, "label": label}

def list_recent_records(db: Session, telegram_user_id: int, limit: int = 10) -> list[dict]:
    """يعرض آخر سجلات المستخدم (معاملات + مهام + ملاحظات) مرتبة بالأحدث.

    يحترم report.view_all: staff يرى سجلاته فقط.
    """
    from app.database.crud.company import report_company_filter


    candidates = []
    for model in (Transaction, Task, Note):
        rows = (
            db.query(model)
            .filter(
                report_company_filter(db, telegram_user_id, model),
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
            company_filter(db, telegram_user_id, model),
            model.deleted_at.is_(None),
        )
        .first()
    )
    return row, model

def delete_record_by_id(
    db: Session, telegram_user_id: int, model_name: str, record_id: int
) -> str | None:
    """Soft-delete لسجل محدد بنوعه (معاملة/مهمة/ملاحظة) بضوابط الأدوار."""
    from app.database.crud import _invalidate_caches, can_manage_records, get_record_by_id


    if not can_manage_records(db, telegram_user_id):
        log_audit(
            telegram_user_id,
            "denied_record_delete",
            f"{model_name}:{record_id}",
            detail="ليس لديك صلاحية الحذف",
        )
        return None
    row, _ = get_record_by_id(db, telegram_user_id, model_name, record_id)
    if row is None:
        return None
    # period close: منع حذف معاملة في فترة مغلقة
    if model_name.lower() in ("transaction", "transactions") and getattr(row, "created_at", None) is not None:
        try:
            from app.timeutil import to_local_naive

            dt = row.occurred_at if getattr(row, "occurred_at", None) is not None else row.created_at
            mk = to_local_naive(dt).strftime("%Y-%m")
            if is_period_closed(db, telegram_user_id, mk):
                return None
        except Exception:
            pass
    label = row.description or getattr(row, "amount", None) or "(بدون وصف)"
    row.deleted_at = now_utc()
    row.updated_at = now_utc()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    log_audit(
        telegram_user_id,
        "record_delete",
        f"{model_name}:{record_id}",
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
    from app.database.crud.company import report_company_filter

    needle = (term or "").strip().lower()
    if not needle:
        return ([], {"per_model": 0, "saturated": False}) if return_meta else []

    per_model = max(limit * 2, 100)
    saturated = False
    kinds = {
        "expense": "مصروف",
        "income": "إيراد",
        "task": "مهمة",
        "order": "طلبية",
        "note": "ملاحظة",
    }
    matched = set()  # (اسم النموذج, id) — منع الازدواج
    matches: list[dict] = []
    models = (Transaction, Task, Note)

    def _plain_match(r, model) -> bool:
        # النص العادي (person/category + تسمية النوع) — بلا قراءة الحقول المشفّرة
        for attr in ("person", "category"):
            if getattr(model, attr, None) is None:
                continue
            if needle in (getattr(r, attr) or "").lower():
                return True
        label = kinds.get(getattr(r, "note_type", None) or getattr(r, "type", None) or "", "")
        return bool(label) and needle in label

    def _encrypted_match(r) -> bool:
        parts = [
            (str(getattr(r, "amount", "") or "")),
            (r.description or ""),
            (r.raw_message or ""),
        ]
        if getattr(r, "note_type", None):
            parts.append(kinds.get(r.note_type, r.note_type))
        if getattr(r, "type", None) in kinds:
            parts.append(kinds[r.type])
        return any(needle in (p or "").lower() for p in parts)

    def _add(model, r):
        key = (model.__name__, r.id)
        if key in matched:
            return
        matched.add(key)
        matches.append(_search_row_result(r, model))

    # نافذة الأحدث تُقرأ مرة واحدة لكل نموذج: يُفحص النص العادي أولًا (بلا فك
    # تشفير)، وإن بلغنا الحد توقفنا مبكرًا. الحقول المشفّرة تُفحص على النافذة
    # نفسها فقط حين تقصر النتائج — لا إعادة قراءة لذات النافذة ثانيةً.
    for model in models:
        if len(matches) >= limit:
            saturated = True
            break
        rows = (
            db.query(model)
            .filter(
                report_company_filter(db, telegram_user_id, model),
                model.deleted_at.is_(None),
            )
            .order_by(model.created_at.desc())
            .limit(per_model)
            .all()
        )
        if len(rows) == per_model:
            saturated = True
        pending = []
        for r in rows:
            if _plain_match(r, model):
                _add(model, r)
            else:
                pending.append(r)
        for r in pending:
            if len(matches) >= limit:
                break
            if _encrypted_match(r):
                _add(model, r)

    matches.sort(key=lambda c: c["created_at"], reverse=True)
    result = matches[:limit]
    if return_meta:
        return result, {"per_model": per_model, "saturated": saturated}
    return result

def update_transaction(db: Session, row: Transaction, fields: dict) -> Transaction:
    """يحدّث حقول محددة في معاملة مالية."""
    try:
        from app.timeutil import to_local_naive
        dt = row.occurred_at if getattr(row, "occurred_at", None) is not None else row.created_at
        if dt is not None:
            mk = to_local_naive(dt).strftime("%Y-%m")
            from app.database.crud.records import is_period_closed
            if is_period_closed(db, row.telegram_user_id, mk):
                raise ValueError("الفترة مغلقة")
    except ValueError:
        raise
    except Exception:
        pass
    from app.database.crud import (
        _clean_person,
        _clean_text,
        _invalidate_caches,
        _to_valid_amount,
        normalize_currency,
    )


    old = {
        key: getattr(row, key, None)
        for key in ("amount", "currency", "person", "category", "description")
    }
    # نحتاج العملة لتحديد دقة 3 منازل لـ JOD/KWD/BHD/OMR
    cur_for_amount = fields.get("currency") or getattr(row, "currency", None)
    # إن كانت العملة الجديدة ستتغير، استخدمها لتحديد الدقة
    if "currency" in fields and fields["currency"]:
        try:
            cur_for_amount = normalize_currency(fields["currency"]) or cur_for_amount
        except Exception:
            pass
    for key in ("amount", "currency", "person", "category", "description"):
        if key in fields and fields[key] is not None:
            if key == "amount":
                new_amount = _to_valid_amount(fields[key], currency=cur_for_amount)
                if new_amount is None:
                    raise ValueError(f"المبلغ غير صالح: {fields[key]!r}")
                row.amount = new_amount
            elif key == "currency":
                raw_cur = fields[key]
                # فارغ → افتراضي عملة الأساس (للمستخدم/الشركة)
                if isinstance(raw_cur, str) and not raw_cur.strip():
                    from app.database.crud.company import get_user_base_currency as _get_base

                    fallback_base = _get_base(db, row.telegram_user_id)
                    row.currency = fallback_base
                    continue
                norm = normalize_currency(raw_cur)
                if not norm:
                    raise ValueError(f"العملة غير صالحة: {fields[key]!r}")
                from app.database.crud.company import get_user_base_currency as _get_base

                fallback_base = _get_base(db, row.telegram_user_id)
                row.currency = norm or fallback_base or fields[key]
            elif key == "category":
                cleaned = _clean_text(fields[key])
                if cleaned is None:
                    raise ValueError("التصنيف غير صالح")
                row.category = cleaned
            elif key == "person":
                cleaned = _clean_person(fields[key])
                if cleaned is None:
                    raise ValueError("الشخص غير صالح")
                row.person = cleaned
            elif key == "description":
                cleaned = _clean_text(fields[key])
                if cleaned is None:
                    raise ValueError("الوصف غير صالح")
                row.description = cleaned
    if "amount" in fields or "currency" in fields:
        # إعادة تثبيت سعر الصرف وحساب VAT: كـ create_transaction
        if row.vat_rate is not None and row.vat_amount is not None and row.amount is not None and row.amount > 0:
            from decimal import Decimal as _D

            expected = (row.amount * row.vat_rate / (_D("100") + row.vat_rate)).quantize(_D("0.01"))
            tolerance = abs(expected) * _D("0.04") + _D("0.05")
            if abs(row.vat_amount - expected) > tolerance:
                row.vat_amount = expected
        base_amount, base_at = _best_effort_base_amount(row.amount, row.currency)
        row.amount_in_base_currency = base_amount
        row.base_currency_at_creation = base_at
    # إعادة فحص سقف الموافقة (approval) عند تغيير المبلغ — منع تجاوز السقف بالتعديل
    if "amount" in fields:
        try:
            from app.database.crud.company import get_company, get_company_member

            m = get_company_member(db, row.telegram_user_id)
            if m is not None and row.type == "expense" and row.amount is not None:
                comp = get_company(db, m.company_id) if m.company_id is not None else None
                limit = None
                if comp is not None:
                    if m.role == "staff":
                        limit = comp.approval_limit_staff
                    elif m.role == "manager":
                        limit = comp.approval_limit_manager
                if limit is not None:
                    row.approval_status = "pending" if row.amount > limit else "approved"
        except Exception:
            pass
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    changed_fields = [k for k in old if getattr(row, k, None) != old[k]]
    log_audit(row.telegram_user_id, "update", f"transaction:{row.id}", detail=f"fields_changed={changed_fields}")
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
                row.description = _clean_text(fields[key])
    row.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    changed_fields = [k for k in old if str(getattr(row, k, None)) != str(old[k])]
    log_audit(row.telegram_user_id, "update", f"note:{row.id}", detail=f"fields_changed={changed_fields}")
    _invalidate_caches(db, row.telegram_user_id)
    return row

_ALL_COMPANIES = object()

def merge_person(db: Session, source: str, target: str, *, company_id=_ALL_COMPANIES) -> int:
    """يدمج اسمين لنفس الطرف عبر كل السجلات ذات الصلة.

    عزل المستأجرين: الدمج الآن معزول حسب الشركة — أطراف بنفس الاسم
    في شركتين مختلفتين لا يُدمجان معًا. إن حُدد company_id يُنفذ الدمج
    لتلك الشركة فقط (حتى لو كانت None → بلا شركة فقط)، وإلا يُنفذ لكل
    شركة على حدة (كل شركة تُعالج مستقلة) لمنع الدمج العابر للمستأجرين
    الذي كان يحدث سابقًا عبر تحديث عام `WHERE person == source` بلا فلتر شركة.

    تمييز "بلا شركة" (NULL) عن "الكل": company_id=None يعني بلا شركة فقط،
    بينما عدم تمرير company_id يعني كل الشركات. هذا يمنع تسريب دمج اسم
    شخصي (company_id NULL) إلى كل الشركات عند عدم إرسال الحقل من القالب
    (القالب الآن يرسل "__none__" للـ NULL).

    بعد الكتابة يُبطَل كاش رؤية كل مستخدم متأثر (ولأعضاء مساحاته عبر
    accessible_user_ids) حتى لا تبقى الأسماء القديمة في ذاكرة run_query.
    يعيد عدد الصفوف المتأثرة.
    """
    from app.database.crud import _clean_person, _invalidate_caches

    # company_id passed as keyword-only already
    source = _clean_person(source)
    target = _clean_person(target)
    if not source or not target or source == target:
        return 0

    # حدد نطاق الشركات المراد معالجتها
    if company_id is not _ALL_COMPANIES:
        company_ids = [company_id]
    else:
        company_ids = []
        for model in (Transaction, Note, Task, Invoice, CreditLimit, Budget, LoyaltyAccount, EmployeeBonusPlan):
            if hasattr(model, "company_id"):
                for (cid,) in db.query(model.company_id).filter(model.person == source, model.company_id.isnot(None)).distinct():
                    if cid not in company_ids:
                        company_ids.append(cid)
                if db.query(model).filter(model.person == source, model.company_id.is_(None)).first() is not None:
                    if None not in company_ids:
                        company_ids.append(None)
        if not company_ids:
            company_ids = [None]

    total_changed = 0
    all_affected: set[int] = set()

    for cid in company_ids:
        affected: set[int] = set()
        for model in (
            Transaction,
            Note,
            Task,
            Invoice,
            CreditLimit,
            Budget,
            LoyaltyAccount,
            EmployeeBonusPlan,
        ):
            if not hasattr(model, "company_id"):
                continue
            q = db.query(model.telegram_user_id).filter(model.person == source)
            if cid is None:
                q = q.filter(model.company_id.is_(None))
            else:
                q = q.filter(model.company_id == cid)
            for (uid,) in q.distinct():
                affected.add(uid)
                all_affected.add(uid)

        changed = 0

        def _resolve_unique_per_user(model, source_uids: set[int], extra=None, _cid=cid) -> None:  # noqa: B023
            nonlocal changed
            if not source_uids:
                return
            q = db.query(model).filter(model.person == source)
            if _cid is None:
                q = q.filter(model.company_id.is_(None))
            else:
                q = q.filter(model.company_id == _cid)
            if extra is not None:
                q = q.filter(extra)
            target_filters = [model.person == target]
            if extra is not None:
                target_filters.append(extra)
            tq = db.query(model.telegram_user_id).filter(*target_filters)
            if _cid is None:
                tq = tq.filter(model.company_id.is_(None))
            else:
                tq = tq.filter(model.company_id == _cid)
            collided = source_uids & {uid for (uid,) in tq.all()}
            if collided:
                dq = db.query(model).filter(model.person == source)
                if _cid is None:
                    dq = dq.filter(model.company_id.is_(None))
                else:
                    dq = dq.filter(model.company_id == _cid)
                if extra is not None:
                    dq = dq.filter(extra)
                changed += dq.filter(model.telegram_user_id.in_(collided)).delete(synchronize_session=False)
            rest = source_uids - collided
            if rest:
                uq = db.query(model).filter(model.person == source)
                if _cid is None:
                    uq = uq.filter(model.company_id.is_(None))
                else:
                    uq = uq.filter(model.company_id == _cid)
                if extra is not None:
                    uq = uq.filter(extra)
                changed += uq.filter(model.telegram_user_id.in_(rest)).update({model.person: target}, synchronize_session=False)

        for model in (Transaction, Note, Task, Invoice, EmployeeBonusPlan):
            q = db.query(model).filter(model.person == source)
            if cid is None:
                q = q.filter(model.company_id.is_(None))
            else:
                q = q.filter(model.company_id == cid)
            changed += q.update({model.person: target}, synchronize_session=False)

        # CreditLimit
        q_ids = db.query(CreditLimit.telegram_user_id).filter(CreditLimit.person == source)
        if cid is None:
            q_ids = q_ids.filter(CreditLimit.company_id.is_(None))
        else:
            q_ids = q_ids.filter(CreditLimit.company_id == cid)
        _resolve_unique_per_user(CreditLimit, {uid for (uid,) in q_ids.all()})

        # Budget
        q_ids = db.query(Budget.telegram_user_id).filter(Budget.person == source, Budget.scope == "person")
        if cid is None:
            q_ids = q_ids.filter(Budget.company_id.is_(None))
        else:
            q_ids = q_ids.filter(Budget.company_id == cid)
        _resolve_unique_per_user(Budget, {uid for (uid,) in q_ids.all()}, extra=Budget.scope == "person")

        # LoyaltyAccount
        q_ids = db.query(LoyaltyAccount.telegram_user_id).filter(LoyaltyAccount.person == source)
        if cid is None:
            q_ids = q_ids.filter(LoyaltyAccount.company_id.is_(None))
        else:
            q_ids = q_ids.filter(LoyaltyAccount.company_id == cid)
        _resolve_unique_per_user(LoyaltyAccount, {uid for (uid,) in q_ids.all()})

        # Party (name field, not person)
        pq = db.query(Party).filter(Party.name == source)
        if cid is None:
            pq = pq.filter(Party.company_id.is_(None))
        else:
            pq = pq.filter(Party.company_id == cid)
        for p in pq.all():
            tq = db.query(Party).filter(Party.name == target, Party.telegram_user_id == p.telegram_user_id)
            if cid is None:
                tq = tq.filter(Party.company_id.is_(None))
            else:
                tq = tq.filter(Party.company_id == cid)
            if tq.first() is not None:
                db.delete(p)
            else:
                p.name = target
            changed += 1

        total_changed += changed

    db.commit()
    for uid in sorted(all_affected):
        _invalidate_caches(db, uid)
    return int(total_changed)


def approve_transaction(db: Session, actor_id: int, transaction_id: int) -> bool:
    """اعتماد عملية مالية معلّقة — لا يعتمدها منشئها."""
    from app.database.crud.company import has_permission

    row = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if row is None or row.approval_status != "pending":
        return False
    if row.telegram_user_id == actor_id:
        return False
    # IDOR: actor must belong to same company as the transaction
    from app.database.crud.company import company_id_for_user
    actor_cid = company_id_for_user(db, actor_id)
    if actor_cid != row.company_id:
        return False
    if not has_permission(db, actor_id, "record.approve") and not has_permission(db, actor_id, "record.edit_any"):
        return False
    row.approval_status = "approved"
    row.approved_by = actor_id
    row.updated_at = now_utc()
    db.commit()
    return True


def is_period_closed(db: Session, telegram_user_id: int, month_key: str) -> bool:
    from app.database.crud.company import company_id_for_user
    from app.database.models import PeriodClose

    cid = company_id_for_user(db, telegram_user_id)
    q = db.query(PeriodClose).filter(PeriodClose.month_key == month_key)
    if cid is None:
        q = q.filter(PeriodClose.telegram_user_id == telegram_user_id, PeriodClose.company_id.is_(None))
    else:
        q = q.filter(PeriodClose.company_id == cid)
    return q.first() is not None


def close_period(db: Session, telegram_user_id: int, month_key: str) -> bool:
    from app.database.crud.company import company_id_for_user, has_permission
    from app.database.models import PeriodClose

    if not has_permission(db, telegram_user_id, "company.manage"):
        return False
    if is_period_closed(db, telegram_user_id, month_key):
        return True
    cid = company_id_for_user(db, telegram_user_id)
    pc = PeriodClose(telegram_user_id=telegram_user_id, company_id=cid, month_key=month_key, closed_by=telegram_user_id)
    db.add(pc)
    db.commit()
    log_audit(telegram_user_id, "close_period", month_key)
    return True


def reopen_period(db: Session, actor_id: int, month_key: str) -> bool:
    from app.database.crud.company import company_id_for_user, has_permission
    from app.database.models import PeriodClose

    if not has_permission(db, actor_id, "company.manage"):
        return False
    cid = company_id_for_user(db, actor_id)
    q = db.query(PeriodClose).filter(PeriodClose.month_key == month_key)
    if cid is None:
        q = q.filter(PeriodClose.telegram_user_id == actor_id, PeriodClose.company_id.is_(None))
    else:
        q = q.filter(PeriodClose.company_id == cid)
    n = q.delete()
    db.commit()
    if n:
        log_audit(actor_id, "reopen_period", month_key)
    return n > 0


def statement_for_person(db: Session, telegram_user_id: int, person: str) -> list[dict]:
    """كشف حساب لطرف: حركات مرتبة زمنيًا مع رصيد متراكم."""
    from app.database.crud.common import _debt_filter, normalize_person_for_match
    from app.database.crud.company import report_company_filter

    rows = (
        db.query(Transaction)
        .filter(
            report_company_filter(db, telegram_user_id, Transaction),
            Transaction.deleted_at.is_(None),
            _debt_filter(Transaction),
        )
        .all()
    )
    rows = [r for r in rows if normalize_person_for_match(r.person) == normalize_person_for_match(person)]
    rows.sort(key=lambda r: (r.occurred_at if getattr(r, "occurred_at", None) is not None else r.created_at) or r.created_at)
    out = []
    bal = Decimal("0")
    for r in rows:
        is_settlement = (r.debt_direction == "settlement" or r.ledger_entry == "settlement" or r.ledger_kind == "settlement")
        eff_type = r.type
        if is_settlement:
            eff_type = "income" if r.type == "expense" else "expense"
        amt = r.amount or Decimal("0")
        if eff_type == "income":
            bal += amt
        else:
            bal -= amt
        dt = r.occurred_at if getattr(r, "occurred_at", None) is not None else r.created_at
        out.append({
            "id": r.id,
            "date": dt,
            "type": r.type,
            "amount": amt,
            "currency": r.currency,
            "balance": bal.quantize(Decimal("0.01")),
            "description": r.description,
        })
    return out


# aliases
statement = statement_for_person
person_statement = statement_for_person


def stock_level(db: Session, product_id: int) -> Decimal:
    """رصيد الصنف = مجموع الحركات (in +, out -, adjustment)."""
    from decimal import Decimal as _D

    from app.database.models import InventoryMovement

    rows = db.query(InventoryMovement).filter(InventoryMovement.product_id == product_id).all()
    total = _D("0")
    for r in rows:
        q = r.quantity or _D("0")
        if r.movement_type == "out":
            total -= q
        elif r.movement_type == "in":
            total += q
        else:  # adjustment sets absolute? treat as delta
            total += q
    return total.quantize(_D("0.001"))
