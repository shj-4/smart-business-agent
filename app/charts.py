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
import threading
from decimal import Decimal

logger = logging.getLogger(__name__)

_CHART_LOCK = threading.Lock()


def _reshape_ar(text: str) -> str:
    """يعيد نصًا عربيًا مُشكَّلًا للعرض الصحيح في matplotlib (يحتاج reshaper)."""
    if not text or not any(0x0600 <= ord(ch) <= 0x06FF for ch in text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _convert_month_currency_groups(
    by_currency: dict, base_currency: str, stored: dict | None = None
) -> dict:
    """يحوّل مجاميع شهر إلى العملة الأساسية (عقد موحّد).

    by_currency: {currency: {"expense": Decimal, "income": Decimal}} — يجب أن
      يكون الحي فقط (live) عند وجود `stored`، وإلا فهو الإجمالي الكامل.
      العقد الموحّد: by_currency = حي فقط، stored = مثبّت فقط، ثم الجمع
      (live×rate + stored). يمنع إسقاط الجزء الحي عند وجود stored لنفس العملة.
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
            totals[c] = totals.get(c, Decimal("0")) + Decimal(str(amount))
            if stored and (stored.get(currency) or {}).get(kind) is not None:
                stored_kind[c] = stored_kind.get(c, Decimal("0")) + Decimal(str(stored[currency][kind]))
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
    - Thread-safe عبر قفل عالمي (matplotlib غير آمن للخيوط).
    """
    # استخدم Agg داخل الخيط (قبل استيراد pyplot) لضمان الأمان مع to_thread
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
    # بنفس العملة الأساسية المطلوبة) — العقد الموحّد: live + stored
    converted = []
    all_zero = True
    has_data = False
    for month in raw:
        stored = month.get("stored") if month.get("stored_base") == base else None
        live = month.get("live_by_currency") if month.get("stored_base") == base else None
        by_for_conv = live if live is not None else month["by_currency"]
        res = _convert_month_currency_groups(by_for_conv, base, stored=stored)
        converted.append((month["label"], res))
        # لا تعتبر partial كـ zero — تجنب رسم صفر مضلل عند فشل التحويل
        if res.get("partial"):
            if (res.get("expense") is not None and res["expense"] != 0) or (
                res.get("income") is not None and res["income"] != 0
            ):
                has_data = True
                all_zero = False
            continue
        if (res["expense"] or Decimal("0")) != 0 or (res["income"] or Decimal("0")) != 0:
            has_data = True
            all_zero = False
            # قيمة صفرية حقيقية (ليست partial)
            has_data = True

    if all_zero:
        return None

    labels = [label for label, _ in converted]
    # لا ترسم partial كصفر — استخدم 0 للعرض لكن مع تمييز (سيُظهر الرسم فجوة/شفافية)
    expenses = [float(res["expense"]) if res.get("expense") is not None else 0 for _, res in converted]
    incomes = [float(res["income"]) if res.get("income") is not None else 0 for _, res in converted]

    # استخدم chart_months الفعلي (عدد الأشهر المرسومة) في العنوان
    actual_months = len(labels) if labels else months
    return _render_monthly_png(labels, incomes, expenses, actual_months, base)


def _render_monthly_png(
    labels: list[str],
    incomes: list[float],
    expenses: list[float],
    months: int,
    base: str,
):
    """يرسم أعمدة الإيرادات/المصاريف ويصدر PNG (أو None عند فشل الحفظ).

    Thread-safe: يحمي matplotlib عبر _CHART_LOCK (غير آمن للخيوط).
    """
    # استخدم Agg داخل الخيط قبل pyplot (آمان الخيوط)
    import matplotlib

    matplotlib.use("Agg")

    from matplotlib import font_manager
    from matplotlib import pyplot as plt

    # قفل عالمي لحماية matplotlib من التزامن
    with _CHART_LOCK:
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
        # إعادة تشكيل العربية للأسطورة والعناوين
        income_label = _reshape_ar("إيرادات")
        expense_label = _reshape_ar("مصروفات")
        title_text = _reshape_ar(f"الإيرادات مقابل المصاريف — آخر {months} أشهر ({base})")
        ylabel_text = _reshape_ar(f"المبلغ ({base})")
        # تسميات المحور X قد تحتوي شهر عربي — شكّلها أيضًا
        reshaped_labels = [_reshape_ar(str(l)) for l in labels]
        ax.bar(
            [p - width / 2 for p in x_pos],
            incomes,
            width,
            label=income_label,
            color="#16a34a",
            edgecolor="white",
        )
        ax.bar(
            [p + width / 2 for p in x_pos],
            expenses,
            width,
            label=expense_label,
            color="#dc2626",
            edgecolor="white",
        )

        ax.set_title(title_text)
        ax.set_xticks(list(x_pos))
        ax.set_xticklabels(reshaped_labels, rotation=45, ha="right")
        ax.set_ylabel(ylabel_text)
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
                "live_by_currency": {},
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
        is_stored = stored is not None and stored_base == base and c and c != base
        if is_stored:
            stored_entry = bucket["stored"].setdefault(
                c, {"expense": Decimal("0"), "income": Decimal("0")}
            )
            stored_entry[kind] = stored_entry.get(kind, Decimal("0")) + stored
        else:
            live_entry = bucket["live_by_currency"].setdefault(
                c, {"expense": Decimal("0"), "income": Decimal("0")}
            )
            live_entry[kind] = live_entry.get(kind, Decimal("0")) + (amount or Decimal("0"))
    return buckets, base


def generate_global_monthly_chart(db, months: int | None = None, base_currency: str | None = None):
    """مثل generate_monthly_chart لكن لكل المستخدمين (لوحة التحكم) — لا فلتر معرّف."""
    from app.config import settings

    months = months or settings.chart_months
    buckets, base = _global_monthly_raw(db, months, base_currency)

    converted = []
    all_zero = True
    has_data = False
    for b in buckets:
        stored = b["stored"] if b["stored_base"] == base else None
        live = b.get("live_by_currency") if b.get("stored_base") == base else None
        by_for_conv = live if live is not None else b["by_currency"]
        res = _convert_month_currency_groups(by_for_conv, base, stored=stored or None)
        converted.append((b["ym"], res))
        if res.get("partial"):
            if (res.get("expense") is not None and res["expense"] != 0) or (
                res.get("income") is not None and res["income"] != 0
            ):
                has_data = True
                all_zero = False
            continue
        if (res["expense"] or 0) != 0 or (res["income"] or 0) != 0:
            has_data = True
            all_zero = False

    if all_zero:
        return None

    labels = [f"{y}-{m:02d}" for (y, m), _ in converted]
    expenses = [float(res["expense"]) if res.get("expense") is not None else 0 for _, res in converted]
    incomes = [float(res["income"]) if res.get("income") is not None else 0 for _, res in converted]
    # استخدم العدد الفعلي للأشهر المرسومة في العنوان
    actual_months = len(labels) if labels else months
    return _render_monthly_png(labels, incomes, expenses, actual_months, base)
