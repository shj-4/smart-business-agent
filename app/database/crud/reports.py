"""
التقارير الشهرية، إعدادات التقارير، الإحصائيات، التنبؤ الخطي، انحراف التوقعات.
"""
import calendar
import os
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.database.db import DATABASE_URL
from app.database.models import (
    Budget,
    CreditLimit,
    Invoice,
    Note,
    ReportPref,
    Task,
    Transaction,
)
from app.exchange import convert_totals_to_base
from app.timeutil import now_local, now_utc, to_local_naive, to_utc_naive


def get_report_pref(db: Session, telegram_user_id: int) -> ReportPref | None:
    return db.query(ReportPref).filter(ReportPref.telegram_user_id == telegram_user_id).first()

def set_report_frequency(
    db: Session, telegram_user_id: int, frequency: str, deliver_time: str | None = None
) -> ReportPref:
    """يضبط تفضيل التقارير الدورية للمستخدم (إنشاء/تحديث)."""

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

    pref.last_sent_at = now_utc()
    db.commit()

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
    from app.database.crud import accessible_user_ids


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

def db_size_bytes() -> int | None:
    """حجم ملف قاعدة البيانات (بايت) لـ SQLite، أو None لغيره/تعذّر القراءة."""
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
    from app.database.crud import accessible_user_ids


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

def kpi_dashboard(db: Session, telegram_user_id: int) -> dict:
    """لوحة مؤشرات أداء مختصرة (KPI) — الشهر الحالي مقابل السابق + مهام/فواتير/ميزانيات.

    لا شبكة حتمية: المبالغ المثبّتة بعملة الأساس وقت التسجيل تُفضَّل للتوحيد
    (convert_totals_to_base بأسلوب stored)؛ ما لا يملك مبلغًا مخزَّنًا يُمتحن
    بسعر اليوم عبر المصدر المشترك. أي فشل تحويل يجعل الحقل None بدل رقم ناقص.
    يعيد dict يُنسَّق في العرض، والشهر الحالي دائمًا موجود حتى لو كان خاويًا.
    """
    from app.database.crud import (
        _unified_totals_for_rows,
        accessible_user_ids,
        budget_usage,
        list_budgets,
        person_debts,
    )

    base = (settings.base_currency or "").upper().strip()
    ids = accessible_user_ids(db, telegram_user_id) or {-1}

    entries = monthly_totals(db, telegram_user_id, months=2, include_stored=True)
    current = entries[-1]
    previous = entries[-2] if len(entries) > 1 else {}

    def _month_unified(entry: dict, kind: str) -> Decimal | None:
        by = entry.get("by_currency") or {}
        stored = entry.get("stored") or {}
        totals = {
            c: v.get(kind) for c, v in by.items() if v.get(kind) is not None
        }
        stored_map = {
            c: v.get(kind) for c, v in stored.items() if v.get(kind) is not None
        }
        if not totals and not stored_map:
            return Decimal("0.00")
        conv = convert_totals_to_base(totals, base, stored=stored_map)
        return conv.get("total")

    cur_exp = _month_unified(current, "expense")
    cur_inc = _month_unified(current, "income")
    prev_exp = _month_unified(previous, "expense")
    prev_inc = _month_unified(previous, "income")

    def _delta(cur: Decimal | None, prev: Decimal | None) -> float | None:
        if cur is None or prev is None or prev == 0:
            return None
        return round(float((cur - prev) / prev * 100), 1)

    task_q = db.query(Task).filter(
        Task.telegram_user_id.in_(ids), Task.deleted_at.is_(None)
    )
    tasks_pending = task_q.filter(Task.status == "pending").count()
    tasks_overdue = task_q.filter(Task.status == "overdue").count()

    inv_q = db.query(Invoice).filter(Invoice.telegram_user_id.in_(ids))
    inv_pending = inv_q.filter(Invoice.status == "pending").count()
    inv_overdue = inv_q.filter(Invoice.status == "overdue").count()

    orders_open = (
        db.query(Note)
        .filter(
            Note.telegram_user_id.in_(ids),
            Note.deleted_at.is_(None),
            Note.note_type == "order",
            Note.status == "open",
        )
        .count()
    )

    budgets = list_budgets(db, telegram_user_id)
    budgets_over = sum(1 for b in budgets if budget_usage(db, b).get("over"))
    budgets_near = sum(
        1
        for b in budgets
        if (u := budget_usage(db, b)).get("percent") >= 80 and not u.get("over")
    )

    debts_net = None
    for d in person_debts(db, telegram_user_id):
        bal = d.get("balance_unified")
        if bal is None:
            continue
        debts_net = (debts_net or Decimal("0")) + bal
    if debts_net is not None:
        debts_net = debts_net.quantize(Decimal("0.01"))

    local_now = now_local()
    month_start = to_utc_naive(
        local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    )
    cat_rows = (
        db.query(Transaction)
        .filter(
            Transaction.telegram_user_id.in_(ids),
            Transaction.deleted_at.is_(None),
            Transaction.type == "expense",
            Transaction.created_at >= month_start,
        )
        .all()
    )
    cat_groups: dict[str, list] = {}
    for r in cat_rows:
        cat_groups.setdefault(r.category or "بدون تصنيف", []).append(r)
    top_categories = []
    for cat, rows in cat_groups.items():
        unified = _unified_totals_for_rows(rows, base).get("total")
        if unified is None:
            unified = sum(
                (r.amount for r in rows if r.amount is not None), Decimal("0")
            )
        top_categories.append(
            {
                "category": cat,
                "amount": unified.quantize(Decimal("0.01")) if unified else Decimal("0.00"),
                "count": len(rows),
            }
        )
    top_categories.sort(key=lambda x: x["amount"], reverse=True)

    return {
        "as_of": local_now.strftime("%Y-%m-%d"),
        "base": base,
        "workspace_size": len(ids),
        "current_month": {
            "label": current.get("label", ""),
            "expense": cur_exp,
            "income": cur_inc,
            "net": (
                (cur_inc - cur_exp).quantize(Decimal("0.01"))
                if cur_inc is not None and cur_exp is not None
                else None
            ),
        },
        "prev_month": {
            "label": previous.get("label", ""),
            "expense": prev_exp,
            "income": prev_inc,
        },
        "expense_vs_prev_pct": _delta(cur_exp, prev_exp),
        "income_vs_prev_pct": _delta(cur_inc, prev_inc),
        "tasks": {"pending": tasks_pending, "overdue": tasks_overdue},
        "invoices": {"pending": inv_pending, "overdue": inv_overdue},
        "orders_open": orders_open,
        "budgets": {
            "total": len(budgets),
            "over": budgets_over,
            "near": budgets_near,
        },
        "debts_net": debts_net,
        "top_categories": top_categories[:5],
    }


def _linear_forecast(values: list[Decimal], steps: int = 1) -> list[Decimal | None]:
    """تنبؤ خطي بسيط (انحدار على آخر قيم) مع قصّ عند الصفر — بلا أي شبكة.

    يعيد None عند نقص البيانات (أقل من نقطتين). يُستخدم للتقريب الإرشادي فقط.
    """
    series = [float(v or 0) for v in values]
    n = len(series)
    if n < 2:
        return [None] * steps
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    den = sum((x - mean_x) ** 2 for x in xs)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, series, strict=True))
    slope = num / den if den else 0.0
    intercept = mean_y - slope * mean_x
    return [max(Decimal("0.00"), Decimal(str(round(intercept + slope * (n + i), 2)))) for i in range(steps)]

def forecast_totals(
    db: Session,
    telegram_user_id: int,
    months: int = 3,
    history: int = 6,
) -> dict:
    """تنبؤ مصاريف/إيرادات الأشهر القادمة لكل عملة (انحدار خطي على آخر قيم)."""

    months = max(1, min(int(months), 12))
    history = max(2, int(history))
    base = (settings.base_currency or "").upper().strip()
    entries = monthly_totals(db, telegram_user_id, months=history, include_stored=True)
    if not entries:
        return {"base": base, "history": history, "months": [], "last_key": None}

    currencies = sorted({c for e in entries for c in e.get("by_currency", {})})
    if not currencies:
        return {"base": base, "history": history, "months": [], "last_key": None}
    exp = {c: [] for c in currencies}
    inc = {c: [] for c in currencies}
    for e in entries:
        byc = e.get("by_currency") or {}
        for c in currencies:
            cell = byc.get(c) or {}
            exp[c].append(cell.get("expense") or Decimal("0"))
            inc[c].append(cell.get("income") or Decimal("0"))

    last_key = entries[-1]["month_key"]  # "YYYY-MM"
    year, month = (int(last_key[:4]), int(last_key[5:7]))
    local_now = now_local()

    def _advance(y, m, step):
        idx = y * 12 + (m - 1) + step
        return idx // 12, idx % 12 + 1

    predicted = []
    cur_exp = {c: list(exp[c]) for c in currencies}
    cur_inc = {c: list(inc[c]) for c in currencies}
    for i in range(months):
        y, m = _advance(year, month, i + 1)
        pred_exp = {}
        pred_inc = {}
        for c in currencies:
            pe = _linear_forecast(cur_exp[c][-history:], 1)[0] or Decimal("0.00")
            pi = _linear_forecast(cur_inc[c][-history:], 1)[0] or Decimal("0.00")
            pred_exp[c] = pe
            pred_inc[c] = pi
            cur_exp[c].append(pe)
            cur_inc[c].append(pi)
        predicted.append(
            {
                "month_key": f"{y:04d}-{m:02d}",
                "label": f"{m:02d}/{y}",
                "expense": pred_exp,
                "income": pred_inc,
                "unified_expense": None,
                "unified_income": None,
            }
        )

    # المجموع الموحّد للمقارنة إن أمكن (بلا شبكة حين تكون العملة هي الأساس)
    for p in predicted:
        p["unified_expense"] = _unified_amount_safe(p["expense"], base)
        p["unified_income"] = _unified_amount_safe(p["income"], base)
    return {
        "base": base,
        "history": history,
        "last_key": last_key,
        "as_of": local_now.strftime("%Y-%m-%d"),
        "months": predicted,
    }

def _unified_amount_safe(totals: dict, base: str) -> Decimal | None:
    """يعيد المجموع الموحّد لعملة الأساس أو None عند تعذّر التحويل (شبكة/لا عملات)."""

    if not totals:
        return Decimal("0.00")
    try:
        conv = convert_totals_to_base(totals, base)
    except Exception:  # noqa: BLE001
        return None
    return conv.get("total")

def deviation_summary(
    db: Session,
    telegram_user_id: int,
    threshold_pct: float = 30.0,
) -> dict:
    """مقارنة شهرية: إنفاق/إيراد الشهر الحالي مقابل متوسط آخر 3 أشهر لكل عملة.

    يعيد: {as_of, threshold_pct, deviations: [{currency, kind, current, average, pct}],
           month_partial, month_elapsed_pct}
    حيث pct نسبة الانحراف (موجب/سالب) — يُنتظر من المتصل فلترة الأهم.
    month_partial=True إذا كان الشهر الحالي لم يكتمل بعد (نُقارن جزءًا بأشهر
    كاملة) — تُنبه الواجهة أن القارنة تقريبية وقد تكون مضلِّلة.
    """
    local_now = now_local()
    days_in_month = calendar.monthrange(local_now.year, local_now.month)[1]
    month_partial = local_now.day < days_in_month
    month_elapsed_pct = round(local_now.day / days_in_month * 100)
    entries = monthly_totals(db, telegram_user_id, months=4, include_stored=False)
    if not entries:
        return {
            "as_of": local_now.strftime("%Y-%m-%d"),
            "threshold_pct": threshold_pct,
            "deviations": [],
            "month_partial": month_partial,
            "month_elapsed_pct": month_elapsed_pct,
        }
    current = entries[-1]
    prev = entries[:-1]
    byc_cur = current.get("by_currency") or {}
    currencies = sorted({c for e in entries for c in e.get("by_currency", {})})
    deviations = []
    for c in currencies:
        for kind in ("expense", "income"):
            cur_val = (byc_cur.get(c) or {}).get(kind) or Decimal("0")
            avg_val = Decimal("0")
            samples = 0
            for e in prev:
                v = (e.get("by_currency") or {}).get(c) or {}
                val = v.get(kind)
                if val is not None and val > 0:
                    avg_val += val
                    samples += 1
            if samples == 0:
                continue  # لا أساس للمقارنة
            avg_val = avg_val / samples
            pct = float((cur_val - avg_val) / avg_val * 100) if avg_val else 0.0
            deviations.append(
                {
                    "currency": c,
                    "kind": kind,
                    "current": cur_val,
                    "average": avg_val.quantize(Decimal("0.01")),
                    "pct": round(pct, 1),
                    "significant": abs(pct) >= threshold_pct,
                }
            )
    return {
        "as_of": local_now.strftime("%Y-%m-%d"),
        "threshold_pct": threshold_pct,
        "deviations": deviations,
        "month_partial": month_partial,
        "month_elapsed_pct": month_elapsed_pct,
    }
