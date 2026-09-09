"""
منطق العرض (فُصل من bot.py): تنسيق النتائج والرسائل للمستخدم فقط.

لا يحتوي على handlers ولا على آلة حالة — دوال نقية (أو إرسال نصوص) يعتمد عليها
bot.conversation و bot.handlers.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

TYPE_NAMES = {
    "expense": "مصروف",
    "income": "إيراد",
    "task": "مهمة",
    "order": "طلبية",
    "note": "ملاحظة",
    "complete_task": "إنجاز مهمة",
}

PERIOD_NAMES = {
    "today": "اليوم",
    "this_week": "هذا الأسبوع",
    "this_month": "هذا الشهر",
    "this_year": "هذه السنة",
    "all_time": "منذ البداية",
}

METRIC_NAMES = {
    "total_expenses": "إجمالي المصاريف",
    "total_income": "إجمالي الإيرادات",
    "person_balance": "رصيد الشخص",
    "compare_periods": "مقارنة فترات",
    "count_transactions": "عدد العمليات",
    "list_tasks": "قائمة المهام",
    "list_overdue_tasks": "المهام المتأخرة",
}

# أسماء الفترات في وضع المقارنة (الحالية مقابل السابقة)
COMPARE_PERIOD_LABELS = {
    "today": ("اليوم", "أمس"),
    "this_week": ("هذا الأسبوع", "الأسبوع الماضي"),
    "this_month": ("هذا الشهر", "الشهر الماضي"),
    "this_year": ("هذه السنة", "السنة الماضية"),
}

FIELD_LABELS = {
    "amount": "المبلغ",
    "currency": "العملة",
    "person": "الشخص",
    "description": "الوصف",
    "date": "الموعد",
}

MAX_MESSAGE_LEN = 4096  # حد تيليجرام لطول الرسالة الواحدة


def split_long_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list:
    """يقسّم نصًا طويلًا إلى قطع لا تتجاوز حد تيليجرام (4096 حرفًا).

    يحاول القصّ عند أسطر جديدة أولاً، ثم عند مسافات، ثم يقطع قسريًا.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks = []
    buf = []
    buf_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1  # +1 للسطر الجديد
        if line_len > limit:
            # سطر طويل جدًا وحده → قصّه قسريًا
            if buf:
                chunks.append("\n".join(buf))
                buf, buf_len = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        if buf_len + line_len > limit:
            chunks.append("\n".join(buf))
            buf, buf_len = [], 0
        buf.append(line)
        buf_len += line_len
    if buf:
        chunks.append("\n".join(buf))
    return chunks


async def safe_reply(message, text: str, **kwargs):
    """يرسل نصًا قد يكون طويلًا مقسّمًا على عدة رسائل ضمن حد تيليجرام."""
    parts = split_long_message(text)
    for _i, chunk in enumerate(parts):
        await message.reply_text(chunk, **kwargs)


def format_record_result(data: dict, saved: bool) -> str:
    data_type = data.get("type")
    if not data_type or data_type == "unknown" or data_type not in TYPE_NAMES:
        return "لم أفهم نوع العملية بوضوح\nهل يمكنك توضيح أكثر؟"

    lines = ["تم تحليل رسالتك:\n"]
    if data.get("type"):
        lines.append(f"النوع: {TYPE_NAMES.get(data['type'], data['type'])}")
    if data.get("amount") is not None:
        lines.append(f"المبلغ: {data['amount']}")
    if data.get("currency"):
        lines.append(f"العملة: {data['currency']}")
    if data.get("person"):
        lines.append(f"الشخص: {data['person']}")
    if data.get("category"):
        lines.append(f"التصنيف: {data['category']}")
    if data.get("description"):
        lines.append(f"الوصف: {data['description']}")

    missing = data.get("missing_fields") or []
    if missing:
        lines.append(f"\nبيانات ناقصة: {', '.join(missing)}")

    lines.append("\nتم الحفظ في قاعدة البيانات" if saved else "\n(لم يتم الحفظ)")
    return "\n".join(lines)


def format_query_result(query_result: dict) -> str:
    metric = query_result.get("metric")
    period = query_result.get("period")
    person = query_result.get("person")
    result = query_result.get("result")

    if query_result.get("error") or result is None:
        return "لم أستطع فهم استعلامك بدقة، حاول صياغته بشكل مختلف."

    period_txt = f" ({PERIOD_NAMES.get(period, period)})" if PERIOD_NAMES.get(period) else ""
    person_txt = f" — {person}" if person else ""

    # قائمة مهام (list)
    if query_result.get("kind") == "list":
        if not result:
            if metric == "list_overdue_tasks":
                return "لا توجد مهام متأخرة."
            if metric == "list_tasks":
                return "لا توجد مهام حاليًا."
            return "لا توجد عناصر مسجلة بعد."
        if metric in ("list_tasks", "list_overdue_tasks"):
            title = "مهامك الحالية:" if metric == "list_tasks" else "المهام المتأخرة:"
            lines = [title]
            for i, task in enumerate(result, 1):
                due = task.get("due_date") or "بدون موعد"
                cat = f" - {task.get('category')}" if task.get("category") else ""
                lines.append(f"{i}. {task.get('description') or '(بدون وصف)'}{cat} (الموعد: {due})")
            return "\n".join(lines)

    # إجمالي مصاريف/إيرادات (نتيجة dict مصنّفة حسب العملة)
    if metric in ("total_expenses", "total_income"):
        title = f"{METRIC_NAMES.get(metric, metric)}:{period_txt}{person_txt}"
        if not result:
            return f"{title}\nلا توجد بيانات لهذه الفترة."
        lines = [title]
        for currency, total in result.items():
            lines.append(f"• {total} {currency}")
        lines.append(_unified_total_line(result, metric, query_result.get("unified_total")))
        return "\n".join(lines)

    # رصيد مستحق مع شخص
    if metric == "person_balance":
        lines = [f"الرصيد مع {person}:{period_txt}"]
        for currency, info in result.items():
            balance = info.get("balance", 0)
            income = info.get("income", 0)
            expense = info.get("expense", 0)
            if balance >= 0:
                relation = (
                    f"له عندك {balance} {currency} (لصالحك +{balance})" if balance > 0 else "متوازن"
                )
                # للإيضاح: موجب = هو مدين لك
                if balance > 0:
                    relation = f"مدين لك بـ {balance} {currency} (استلمت {income} / دفعت {expense})"
            else:
                relation = f"عليك له {abs(balance)} {currency} (استلمت {income} / دفعت {expense})"
            lines.append(f"• {relation}")
        if not result:
            lines.append("• لا توجد معاملات مع هذا الشخص.")
        return "\n".join(lines)

    # عدد العمليات (رقم)
    if metric == "count_transactions":
        return f"{METRIC_NAMES.get(metric, metric)}{period_txt}: {result}"

    # مقارنة فترات
    if metric == "compare_periods":
        return _format_comparison(query_result)

    # fallback: نص عام
    lines = [f"{METRIC_NAMES.get(metric, metric)}{period_txt}{person_txt}:\n"]
    lines.append(str(result))
    return "\n".join(lines)


def _fmt_amount(value) -> str:
    """يُنسّق Decimal/رقم بلا أصفار زائدة."""
    from decimal import Decimal

    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    if d == d.to_integral_value():
        return format(d, "f")
    return format(d.normalize(), "f")


def _totals_line(totals: dict) -> str:
    """يعرض مجموعًا بعملات متعددة: 1500 شيكل + 200 دولار."""
    if not totals:
        return ""
    parts = [f"{_fmt_amount(total)} {currency}" for currency, total in totals.items()]
    return " + ".join(parts)


def _unified_amount(totals: dict, stored: dict | None = None):
    """المجموع موحّدًا بالعملة الأساسية (Decimal) أو None عند الفشل.

    stored (اختياري): مبالغ بعملة الأساس مثبّتة وقت التسجيل — تُفضّل للدقة التاريخية.
    """
    from decimal import Decimal

    from app.config import settings
    from app.database.crud import normalize_currency
    from app.exchange import convert_totals_to_base

    base = settings.base_currency
    normalized = {}
    for currency, total in totals.items():
        c = normalize_currency(currency) or currency
        try:
            normalized[c] = Decimal(str(total))
        except Exception:
            normalized[c] = Decimal("0")
    try:
        conv = convert_totals_to_base(normalized, base, stored=stored)
    except Exception:
        return None
    return conv.get("total")


def _compare_delta(current: dict, previous: dict) -> str | None:
    """يفرق نسبة التغيير في المصاريف بين فترتين (بالمجموع الموحّد إن أمكن)."""
    cur_total = _unified_amount(current)
    prev_total = _unified_amount(previous)
    if cur_total is None or prev_total is None or prev_total == 0:
        return None

    from app.config import settings

    base = settings.base_currency
    diff = cur_total - prev_total
    percent = float(diff / prev_total * 100)
    if abs(percent) < 0.05:
        return f"التغيير: ≈ بدون فرق يُذكر ({_fmt_amount(prev_total)} {base})"
    if diff > 0:
        return f"التغيير: ▲ زيادة {_fmt_amount(diff)} {base} (+{percent:.1f}%)"
    return f"التغيير: ▼ انخفاض {_fmt_amount(abs(diff))} {base} ({percent:.1f}%)"


def _format_comparison(query_result: dict) -> str:
    """يعرض نتيجة مقارنة الفترات (expense/income) بشكل مقروء."""
    period = query_result.get("period")
    person = query_result.get("person")
    result = query_result.get("result") or {}
    cur_label, prev_label = COMPARE_PERIOD_LABELS.get(period, ("الفترة الحالية", "الفترة السابقة"))

    cur = result.get("current") or {}
    prev = result.get("previous") or {}
    cur_exp = cur.get("expense") or {}
    prev_exp = prev.get("expense") or {}
    cur_inc = cur.get("income") or {}
    prev_inc = prev.get("income") or {}

    person_txt = f" — {person}" if person else ""
    lines = [f"📊 مقارنة المصاريف: {cur_label} مقابل {prev_label}{person_txt}\n"]

    if not cur_exp and not prev_exp:
        lines.append("لا توجد بيانات مصاريف في الفترتين.")
    else:
        lines.append(f"💸 {cur_label}: {_totals_line(cur_exp) or 'لا توجد'}")
        lines.append(f"📅 {prev_label}: {_totals_line(prev_exp) or 'لا توجد'}")
        delta = _compare_delta(cur_exp, prev_exp)
        if delta:
            lines.append(delta)

    if cur_inc or prev_inc:
        lines.append("")
        lines.append(f"💰 الإيرادات — {cur_label}: {_totals_line(cur_inc) or 'لا توجد'}")
        lines.append(f"{prev_label}: {_totals_line(prev_inc) or 'لا توجد'}")

    return "\n".join(lines)


def _unified_total_line(result: dict, metric: str, unified: dict | None = None) -> str:
    """يضيف المجموع الموحّد بالعملة الأساسية.

    unified (اختياري): مجموع محسوب مسبقًا في crud بدقة تاريخية — يفضّل مبالغ
    سعر الصرف المثبَّت وقت التسجيل ويُظهر ذلك صراحةً؛ عند غيابه (نتائج قديمة/
    مخزنة) يُحسب هنا بأسعار اليوم.
    """
    from decimal import Decimal

    from app.config import settings
    from app.database.crud import normalize_currency
    from app.exchange import convert_totals_to_base

    if unified is not None and unified.get("total") is not None:
        base = unified.get("base") or settings.base_currency
        hint = ""
        if unified.get("from_stored") and len(result) > 1:
            hint = " (بأسعار مثبّتة لحظة التسجيل)"
        elif len(result) > 1:
            hint = " (تقريبًا)"
        return f"\nالمجموع الموحّد{hint}: {unified['total']} {base}"

    base = settings.base_currency
    totals = {}
    for currency, total in result.items():
        normalized = normalize_currency(currency) or currency
        try:
            totals[normalized] = Decimal(str(total))
        except Exception:
            totals[normalized] = Decimal("0")

    try:
        conv = convert_totals_to_base(totals, base)
    except Exception:
        return f"\n(تعذر حساب المجموع الموحّد بـ {base})"

    if conv.get("total") is None:
        return f"\n(المجموع الموحّد بـ {base} غير متوفر الآن — تعذر جلب سعر صرف)"
    hint = " (تقريبًا)" if len(totals) > 1 else ""
    return f"\nالمجموع الموحّد{hint}: {conv['total']} {base}"


def _build_confirm_text(data: dict) -> str:
    lines = ["هل تريد حفظ هذا السجل؟\n"]
    if data.get("type"):
        lines.append(f"النوع: {TYPE_NAMES.get(data['type'], data['type'])}")
    if data.get("amount") is not None:
        lines.append(f"المبلغ: {data['amount']}")
    if data.get("currency"):
        lines.append(f"العملة: {data['currency']}")
    if data.get("person"):
        lines.append(f"الشخص: {data['person']}")
    if data.get("category"):
        lines.append(f"التصنيف: {data['category']}")
    if data.get("description"):
        lines.append(f"الوصف: {data['description']}")
    if data.get("date"):
        lines.append(f"الموعد: {data['date']}")
    return "\n".join(lines)


def _build_confirm_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأكيد", callback_data="confirm:yes"),
                InlineKeyboardButton("✏️ تعديل", callback_data="confirm:edit"),
            ],
            [
                InlineKeyboardButton("❌ إلغاء", callback_data="confirm:no"),
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main"),
            ],
        ]
    )
