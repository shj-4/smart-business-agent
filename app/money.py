"""
حسابات مالية نقية (وَرَقة بلا اعتماد على crud أو SQL).

تُقرأ صفوف المعاملات بعد فك تشفير الحقول (EncryptedNumeric)، فتُجمَع المبالغ
في Python بدل SQL SUM على حقل مشفَّر. الدوال هنا مشتركة بين تقارير crud
وإحصائيات الأدمن — وضعها في وحدة مستقلة يكسر الدورة crud↔admin
(كان admin يستورد _sum_amounts_by_currency من crud).
"""
from decimal import Decimal

from app import exchange
from app.config import settings


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
    stored, live = _split_stored_base(rows, base_currency)
    conv = exchange.convert_totals_to_base(live, base_currency, stored=stored)
    return {
        "base": base_currency,
        "total": conv.get("total"),
        "partial": conv.get("partial", False),
        "from_stored": bool(stored),
    }


def _best_effort_base_amount(amount, currency: str | None):
    """يحوّل مبلغًا لعملة الأساس وقت التسجيل (best-effort) — يعيد (المبلغ، العملة) أو (None, None).

    دقة تاريخية: نقوم بمحاولة تحويل واحدة لحظة الإنشاء؛ إن فشلت (لا إنترنت/
    عملة غير معرفة) تُترك القيم None، وفي كل تقرير لاحق تُعاد العملية للباقي
    بأسعار اليوم عبر convert_totals_to_base(stored=...).
    """
    base = (settings.base_currency or "").upper().strip()
    if amount is None or not currency or not base or currency.upper() == base:
        return None, None
    try:
        conv = exchange.convert(amount, currency, base)
    except Exception:
        return None, None
    result = conv.get("result")
    if result is None:
        return None, None
    return Decimal(str(result)), base
