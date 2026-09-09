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


def _convert_month_currency_groups(by_currency: dict, base_currency: str) -> dict:
    """يحوّل مجاميع شهر (by_currency) إلى العملة الأساسية.

    by_currency: {currency: {"expense": Decimal, "income": Decimal}}
    يعيد: {"expense": Decimal, "income": Decimal, "partial": bool}
    """
    from app.database.crud import normalize_currency
    from app.exchange import convert_totals_to_base

    def _to_base(kind: str) -> Decimal | None:
        totals = {}
        for currency, info in by_currency.items():
            amount = (info or {}).get(kind)
            if amount is None:
                continue
            c = normalize_currency(currency) or currency
            totals[c] = Decimal(str(amount))
        conv = convert_totals_to_base(totals, base_currency)
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
    raw = monthly_totals(db, telegram_user_id, months)

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

    # توحيد كل شهر إلى العملة الأساسية
    converted = []
    all_zero = True
    for month in raw:
        res = _convert_month_currency_groups(month["by_currency"], base)
        converted.append((month["label"], res))
        if (res["expense"] or Decimal("0")) != 0 or (res["income"] or Decimal("0")) != 0:
            all_zero = False

    if all_zero:
        return None

    # إعداد الشكل
    labels = [label for label, _ in converted]
    expenses = [float(res["expense"] or 0) for _, res in converted]
    incomes = [float(res["income"] or 0) for _, res in converted]

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
