"""
إدارة عملات التحويل典礼 تلقائية باستخدام free API.

- مصفوفة أسعار من https://open.er-api.com/v6/latest/{base}
- تخزين مؤقت في الذاكرة لمدة ساعة (لتجنّب طلبات كثيرة).
- لا يخزّن مفاتيح API — API مجاني لا يحتاج مفتاح.
"""

import logging
import time
from decimal import ROUND_HALF_UP, Decimal

import httpx

logger = logging.getLogger(__name__)

# تخزين مؤقت: {(from_cur, to_cur): (rate, timestamp)}
_cache: dict[tuple[str, str], tuple[Decimal, float]] = {}
_CACHE_TTL = 3600  # ساعة واحدة

# _last_rates[base] = (rates_dict, timestamp)
_last_rates: dict[str, tuple[dict, float]] = {}

API_BASE = "https://open.er-api.com/v6/latest"

# ---------- قاطع الدائرة (Circuit Breaker) للخدمة الخارجية ----------
# بعد عدة فشل متتالٍ يتوقف الاتصال بالكامل لفترة (لا يُبطئ كل تقرير/فحص),
# مع محاولة استكشاف (half-open) بعد انتهاء فترة التهدئة. لا تحاول العملية
# إصلاح الشبكة — السياق عملي: عند تعطّل الخدمة، يتجنّب البوت انتظار 10 ثوانٍ
# كل مرة ويعتمد بدلًا على آخر أسعار مخزّنة أو يعلن النقص صراحةً.
_CB_THRESHOLD = 3  # عدد الفشل المتتالي لفتح الدائرة
_CB_COOLDOWN = 120  # ثوانٍ قبل السماح بمحاولة استكشاف واحدة
_BREAKER: dict = {"failures": 0, "open_until": 0.0}


def breaker_open() -> bool:
    return time.time() < _BREAKER["open_until"]


def reset_breaker() -> None:
    """إعادة ضبط قاطع الدائرة (للاختبارات والمراقبة اليدوية)."""
    _BREAKER["failures"] = 0
    _BREAKER["open_until"] = 0.0


def _record_fetch_result(success: bool) -> None:
    if success:
        _BREAKER["failures"] = 0
        return
    _BREAKER["failures"] += 1
    if _BREAKER["failures"] >= _CB_THRESHOLD:
        _BREAKER["open_until"] = time.time() + _CB_COOLDOWN
        logger.error(
            "قاطع الدائرة لأسعار الصرف مفتوح بعد %d فشل متتالٍ حتى %s",
            _BREAKER["failures"],
            _BREAKER["open_until"],
        )


def _fetch_rates(base: str) -> dict[str, float] | None:
    """يجلب أسعار الصرف لعملة أساسية من API المجاني (بدون منطق قاطع الدائرة)."""
    url = f"{API_BASE}/{base}"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            if data.get("result") == "success":
                return data.get("rates", {})
    except Exception as exc:  # noqa: BLE001 — أي فشل شبكة يُعاد None ويعالجه المستدعي
        logger.warning("فشل جلب أسعار الصرف من %s: %s", url, exc)
    return None


def get_rate(from_cur: str, to_cur: str) -> Decimal | None:
    """يجلب سعر صرف من from_cur → to_cur مع تخزين مؤقت."""
    from_cur = from_cur.upper().strip()
    to_cur = to_cur.upper().strip()

    if from_cur == to_cur:
        return Decimal("1.0000")

    cache_key = (from_cur, to_cur)
    now = time.time()

    if cache_key in _cache:
        rate, ts = _cache[cache_key]
        if now - ts < _CACHE_TTL:
            return rate

    rates = _last_rates.get(from_cur)
    if not rates or (now - rates[1]) >= _CACHE_TTL:
        raw = None
        attempted = False
        if breaker_open():
            logger.warning("أسعار الصرف: الدائرة مفتوحة — نعتمد آخر سعر مخزّن (%s)", from_cur)
        else:
            attempted = True
            raw = _fetch_rates(from_cur)
        if raw is not None:
            _record_fetch_result(True)
            _last_rates[from_cur] = (raw, now)
        else:
            if attempted:
                _record_fetch_result(False)
            # محاولة آخر سعر مخزّن
            if from_cur in _last_rates:
                r = _last_rates[from_cur][0].get(to_cur)
                if r is not None:
                    return Decimal(str(r)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            return None

    rates_dict = _last_rates[from_cur][0]
    rate_val = rates_dict.get(to_cur)
    if rate_val is None:
        return None

    rate = Decimal(str(rate_val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    _cache[cache_key] = (rate, now)
    return rate


def convert(amount: Decimal | float | str, from_cur: str, to_cur: str) -> dict:
    """يحوّل مبلغ من عملة إلى أخرى.

    يعيد dict: {amount, from, to, rate, result} أو {error: str}.
    """
    from_cur = from_cur.upper().strip()
    to_cur = to_cur.upper().strip()

    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return {"error": "مبلغ غير صالح"}

    if from_cur == to_cur:
        return {
            "amount": amount,
            "from": from_cur,
            "to": to_cur,
            "rate": Decimal("1.0000"),
            "result": amount,
        }

    rate = get_rate(from_cur, to_cur)
    if rate is None:
        return {"error": f"تعذر جلب سعر صرف {from_cur} → {to_cur}"}

    result = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"amount": amount, "from": from_cur, "to": to_cur, "rate": rate, "result": result}


CURRENCY_NAMES = {
    "ILS": "شيكل (ILS)",
    "USD": "دولار (USD)",
    "JOD": "دينار (JOD)",
    "EUR": "يورو (EUR)",
    "GBP": "جنيه إسترليني (GBP)",
}


def convert_totals_to_base(totals: dict, base_currency: str) -> dict:
    """يحوّل مجموعًا بعدة عملات إلى عملة أساسية.

    totals: {currency: amount (Decimal)}.
    يعيد: {"base": base_currency, "total": Decimal|None, "partial": bool, "rates": {...}}

    - `total` None إذا فشل كل التحويل (مشكلة شبكة).
    - `partial` True إذا نجحت بعض العملات فقط (لا يمكن عرض مجموع كامل).
    """
    base_currency = base_currency.upper().strip()
    if not totals:
        return {"base": base_currency, "total": Decimal("0.00"), "partial": False, "rates": {}}

    total = Decimal("0")
    partial = False
    rates = {}

    for currency, amount in totals.items():
        if currency == base_currency:
            rates[currency] = Decimal("1.0000")
            total += amount
            continue
        try:
            rate = get_rate(currency, base_currency)
        except Exception:
            rate = None
        if rate is None:
            partial = True
            rates[currency] = None
            continue
        rates[currency] = rate
        total = total + (amount * rate)

    total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # إذا فشل تحويل عملة واحدة، لا نعرض مجموعًا ناقصًا قد يضلل المستخدم
    final_total = None if partial else total
    return {
        "base": base_currency,
        "total": final_total,
        "partial": partial,
        "rates": rates,
    }
