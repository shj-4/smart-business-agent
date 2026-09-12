"""
أدوات تنسيق مشتركة بين وحدات العرض والتقارير (مصدر واحد بدل النسخ المكررة):

- fmt_amount: تنسيق مبلغ Decimal/رقم بلا أصفار زائدة.
- totals_line: دمج مجموع بعملات متعددة بعلامة "+".
- current_month_key: مفتاح الشهر الحالي المحلي (YYYY-MM) للميزانيات والتقارير.

كانت هذه الوظائف معرّفة سابقًا بنسخ متشابهة في bot/formatters.py وbot/reports.py
ووظيفة الشهر في app/database/crud/budgets.py — توحيدها هنا يُبقي السلوك واحدًا.
"""

from decimal import Decimal

from app.timeutil import now_local


def fmt_amount(value: Decimal | int | float) -> str:
    """يُنسّق Decimal/رقم بلا أصفار زائدة."""
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    if d == d.to_integral_value():
        return format(d, "f")
    return format(d.normalize(), "f")


def totals_line(totals: dict) -> str:
    """يعرض مجموعًا بعملات متعددة: 1500 شيكل + 200 دولار."""
    if not totals:
        return ""
    return " + ".join(f"{fmt_amount(total)} {currency}" for currency, total in totals.items())


def current_month_key() -> str:
    """مفتاح الشهر الحالي محليًا (YYYY-MM) — يُستخدم للميزانيات الشهرية."""
    return now_local().strftime("%Y-%m")
