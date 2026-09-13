"""
توليد رسوم بيانية بسيطة (matplotlib) لمقارنة الإيرادات والمصاريف شهريًا.

تُستخدم مع أمر /chart: ترسم آخر N أشهر (افتراضيًا 6) كأعمدة متجاورة
لكل شهر (إيرادات مقابل مصروفات) بعد توحيد العملات إلى العملة الأساسية
(settings.base_currency) عبر أسعار الصرف المخزّنة مؤقتًا في app.exchange.

لا يرسم أي شيء إذا فشل توحيد كل العملات (الشبكة معطّلة مثلًا) ويعيد None
ليعرض البوت رسالة توضيحية للمستخدم.
"""

import io
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def _convert_month_currency_groups(
    by_currency: dict, base_currency: str, stored: dict | None = None
) -> dict:
    """يحوّل مجاميع شهر (by_currency) إلى العملة الأساسية.

    by_currency: {currency: {"expense": Decimal, "income": Decimal}}
    stored (اختياري): {currency: {"expense": Decimal, "income": Decimal}} مبالغ
      موحّدة بعملة الأساس وقت التسجيل (يبقى الرسم دقيقًا تاريخيًا حتى لو تغيّر
      سعر الصرف لاحقًا) — يُفضَّل على سعر اليوم لكل عملة متاحة.
    يعيد: {"expense": Decimal, "income": Decimal, "partial": bool}
    """
    from app.database.crud import normalize_currency
    from app.exchange import convert_totals_to_base

    def _to_base(kind: str) -> Decimal | None:
        totals = {}
        stored_kind = {}
        for currency, info in by_currency.items():
            amount = (info or {}).get(kind)
            if amount is None:
                continue
            c = normalize_currency(currency) or currency
            totals[c] = Decimal(str(amount))
            if stored and (stored.get(currency) or {}).get(kind) is not None:
                stored_kind[c] = Decimal(str(stored[currency][kind]))
        conv = convert_totals_to_base(totals, base_currency, stored=stored_kind or None)
        return conv.get("total")

    expense = _to_base("expense")
    income = _to_base("income")
    if expense is None and income is None:
        return {"expense": None, "income": None, "partial": True}

    return {
        "expense": expense or Decimal("0"),
        "income": income or Decimal("0"),
        "partial": expense is None or income is None,
    }


def generate_monthly_chart(
    db,
    telegram_user_id: int,
    months: int | None = None,
    base_currency: str | None = None,
):
    """يرسم أعمدة الإيرادات/المصاريف لآخر N أشهر ويعيد بايتات PNG (أو None).

    - يتحول تلقائيًا إلى Agg backend (بلا نافذة) لأمان الخادم.
    - عند فشل توحيد كل العملات يعيد None (والداعي يعرض رسالة).
    """
    import matplotlib

    matplotlib.use("Agg")

    from app.config import settings
    from app.database.crud import monthly_totals

    months = months or settings.chart_months
    base = (base_currency or settings.base_currency).upper().strip()
    raw = monthly_totals(db, telegram_user_id, months, include_stored=True)

    from matplotlib import font_manager
    from matplotlib import pyplot as plt

    # تحميل خط يدعم العربية (Segoe UI متوفر في ويندوز)
    try:
        available = {f.name for f in font_manager.fontManager.ttflist}
        for candidate in ("Segoe UI", "Tahoma", "Arial", "DejaVu Sans"):
            if candidate in available:
                plt.rcParams["font.family"] = [candidate]
                break
    except Exception:
        logger.debug("تعذر ضبط خط الرسم، نستخدم الافتراضي.")
    plt.rcParams["axes.unicode_minus"] = False

    # توحيد كل شهر إلى العملة الأساسية (يفضّل المبالغ المثبّتة عند التسجيل إن كانت
    # بنفس العملة الأساسية المطلوبة)
    converted = []
    all_zero = True
    for month in raw:
        stored = month.get("stored") if month.get("stored_base") == base else None
        res = _convert_month_currency_groups(month["by_currency"], base, stored=stored)
        converted.append((month["label"], res))
        if (res["expense"] or Decimal("0")) != 0 or (res["income"] or Decimal("0")) != 0:
            all_zero = False

    if all_zero:
        return None

    labels = [label for label, _ in converted]
    expenses = [float(res["expense"] or 0) for _, res in converted]
    incomes = [float(res["income"] or 0) for _, res in converted]

    return _render_monthly_png(labels, incomes, expenses, months, base)


def _render_monthly_png(
    labels: list[str],
    incomes: list[float],
    expenses: list[float],
    months: int,
    base: str,
):
    """يرسم أعمدة الإيرادات/المصاريف ويصدر PNG (أو None عند فشل الحفظ)."""
    import matplotlib

    matplotlib.use("Agg")

    from matplotlib import font_manager
    from matplotlib import pyplot as plt

    try:
        available = {f.name for f in font_manager.fontManager.ttflist}
        for candidate in ("Segoe UI", "Tahoma", "Arial", "DejaVu Sans"):
            if candidate in available:
                plt.rcParams["font.family"] = [candidate]
                break
    except Exception:
        logger.debug("تعذر ضبط خط الرسم، نستخدم الافتراضي.")
    plt.rcParams["axes.unicode_minus"] = False

    x_pos = range(len(labels))
    width = 0.38

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(
        [p - width / 2 for p in x_pos],
        incomes,
        width,
        label="إيرادات",
        color="#16a34a",
        edgecolor="white",
    )
    ax.bar(
        [p + width / 2 for p in x_pos],
        expenses,
        width,
        label="مصروفات",
        color="#dc2626",
        edgecolor="white",
    )

    ax.set_title(f"الإيرادات مقابل المصاريف — آخر {months} أشهر ({base})")
    ax.set_xticks(list(x_pos))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_ylabel(f"المبلغ ({base})")
    ax.legend(loc="upper left")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=110)
    except Exception:
        logger.exception("خطأ في حفظ الرسم البياني")
        return None
    finally:
        plt.close(fig)

    buf.seek(0)
    return buf


def _global_monthly_raw(db, months: int = 6, base_currency: str | None = None):
    """مجموع الشهور الستة الأخيرة لجميع المستخدمين (لوحة التحكم) بعملاتها الخام.

    يعيد (buckets, base) حيث كل صندوق: {ym, by_currency, stored, stored_base}.
    """
    from datetime import datetime
    from decimal import Decimal

    from app.config import settings
    from app.database.models import Transaction
    from app.timeutil import now_local, to_local_naive, to_utc_naive

    months = max(1, int(months))
    base = (base_currency or settings.base_currency or "").upper().strip()
    local_now = now_local()
    pairs = []
    y, m = local_now.year, local_now.month
    for _ in range(months):
        pairs.append((y, m))
        if m == 1:
            y, m = y - 1, 12
        else:
            m -= 1
    pairs.reverse()
    start = to_utc_naive(datetime(pairs[0][0], pairs[0][1], 1))

    rows = (
        db.query(
            Transaction.created_at,
            Transaction.type,
            Transaction.currency,
            Transaction.amount,
            Transaction.amount_in_base_currency,
            Transaction.base_currency_at_creation,
        )
        .filter(Transaction.deleted_at.is_(None), Transaction.created_at >= start)
        .all()
    )

    buckets = []
    for (yy, mm) in pairs:
        buckets.append(
            {
                "ym": (yy, mm),
                "by_currency": {},
                "stored": {},
                "stored_base": base,
            }
        )
    index = {b["ym"]: b for b in buckets}
    for created_at, rtype, currency, amount, stored, stored_base in rows:
        local = to_local_naive(created_at)
        bucket = index.get((local.year, local.month))
        if bucket is None:
            continue
        c = (currency or "").upper()
        kind = "expense" if rtype == "expense" else "income"
        entry = bucket["by_currency"].setdefault(
            c, {"expense": Decimal("0"), "income": Decimal("0")}
        )
        entry[kind] = entry.get(kind, Decimal("0")) + (amount or Decimal("0"))
        if stored is not None and stored_base == base and c:
            stored_entry = bucket["stored"].setdefault(
                c, {"expense": Decimal("0"), "income": Decimal("0")}
            )
            stored_entry[kind] = stored_entry.get(kind, Decimal("0")) + stored
    return buckets, base


def generate_global_monthly_chart(db, months: int | None = None, base_currency: str | None = None):
    """مثل generate_monthly_chart لكن لكل المستخدمين (لوحة التحكم) — لا فلتر معرّف."""
    from app.config import settings

    months = months or settings.chart_months
    buckets, base = _global_monthly_raw(db, months, base_currency)

    converted = []
    all_zero = True
    for b in buckets:
        stored = b["stored"] if b["stored_base"] == base else None
        res = _convert_month_currency_groups(b["by_currency"], base, stored=stored or None)
        converted.append((b["ym"], res))
        if (res["expense"] or 0) != 0 or (res["income"] or 0) != 0:
            all_zero = False

    if all_zero:
        return None

    labels = [f"{y}-{m:02d}" for (y, m), _ in converted]
    expenses = [float(res["expense"] or 0) for _, res in converted]
    incomes = [float(res["income"] or 0) for _, res in converted]
    return _render_monthly_png(labels, incomes, expenses, months, base)
