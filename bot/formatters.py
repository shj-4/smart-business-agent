"""
منطق العرض (فُصل من bot.py): تنسيق النتائج والرسائل للمستخدم فقط.

لا يحتوي على handlers ولا على آلة حالة — دوال نقية (أو إرسال نصوص) يعتمد عليها
bot.conversation و bot.handlers.
"""

from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.models import Invoice, Note
from app.formatting import fmt_amount as _fmt_amount
from app.formatting import totals_line as _totals_line

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


def split_long_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
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


async def safe_reply(message: Message, text: str, **kwargs) -> None:
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


def _unified_amount(totals: dict, stored: dict | None = None) -> Decimal | None:
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


def _build_confirm_keyboard() -> InlineKeyboardMarkup:
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


# ---------- ديون الأشخاص (#21) ----------


def format_debts(payload: list[dict]) -> str:
    """يعرض رصيد كل شخص: بماذا تدين له / بماذا يدين لك."""
    if not payload:
        return "لا توجد معاملات مع أشخاص بعد.\nسجّل مصروفًا أو إيرادًا باسم شخص وسأتابع ديونه."

    lines = ["💳 الديون والأرصدة:\n"]
    for d in payload:
        name = d["person"]
        base = d.get("base") or ""
        net = d.get("balance_unified")
        if net is not None:
            if net > 0:
                net_txt = f"✅ {_fmt_amount(net)} {base} لك (يدين لك)"
            elif net < 0:
                net_txt = f"⚠️ عليك له {_fmt_amount(abs(net))} {base}"
            else:
                net_txt = "↔️ متوازن"
        elif d.get("partial"):
            net_txt = "(لا يمكن توحيد العملات الآن — تعذر جلب سعر صرف)"
        else:
            net_txt = ""
        lines.append(f"• {name}{'' if not net_txt else ' — ' + net_txt}")

        for currency, info in (d.get("by_currency") or {}).items():
            lines.append(
                f"   • {currency}: الرصيد {_fmt_amount(info['balance'])}"
                f" ({_fmt_amount(info['expense'])} دفعتُ له / {_fmt_amount(info['income'])} استلمتُ منه)"
            )
    lines.append("\nملاحظة: المبالغ الموجبة تعني أن الشخص مدين لك، والسالبة تعني أنك تدين له.")
    return "\n".join(lines)


# ---------- الفواتير الآجلة (#22) ----------


def format_invoices(invoices: list[Invoice], title: str = "🧾 الفواتير الآجلة:") -> str:
    """يعرض قائمة فواتير (objects) بحالة ومبلغ وميعاد."""
    if not invoices:
        return f"{title}\nلا توجد فواتير."

    lines = [title]
    for inv in invoices:
        person = inv.person or "بدون شخص"
        due = getattr(inv, "due_date", None)
        due_txt = ""
        if due is not None:
            try:
                from app.timeutil import to_local_naive

                due_txt = to_local_naive(due).strftime("%Y-%m-%d")
            except Exception:
                due_txt = ""
        desc = (inv.description or "")[:40]
        status_txt = {
            "pending": "⏳",
            "overdue": "⚠️",
            "paid": "✅",
        }.get(inv.status, inv.status)
        line = f"{status_txt} #{inv.id} {person}: {_fmt_amount(inv.amount)} {inv.currency or ''}"
        if due_txt:
            line += f" — يستحق {due_txt}"
        if desc:
            line += f"\n      {desc}"
        lines.append(line)
    return "\n".join(lines)


# ---------- الطلبيات (#38) ----------


def format_orders(orders: list[Note], title: str = "🛒 طلبياتك:") -> str:
    """يعرض الطلبيات (Note note_type=order) بحالتها."""
    if not orders:
        return f"{title}\nلا توجد طلبيات."

    lines = [title]
    for o in orders:
        person = o.person or ""
        desc = (o.description or "(بدون وصف)")[:60]
        status = o.status or "open"
        flag = "⏳ مفتوحة" if status == "open" else "✅ منجزة"
        person_txt = f" — {person}" if person else ""
        lines.append(f"{o.id}. {desc}{person_txt} ({flag})")
    return "\n".join(lines)


# ---------- الحدود الائتمانية (#26) ----------


def format_credit_limits(payload: list[dict]) -> str:
    """يعرض الحدود الائتمانية مع الاستخدام الحالي لكل شخص."""
    if not payload:
        return (
            "لا توجد حدود ائتمانية.\n"
            "استخدم: /credit إضافة <الشخص> <المبلغ>\n"
            "مثال: /credit إضافة محمد 5000"
        )

    lines = ["⚠️ الحدود الائتمانية:\n"]
    for d in payload:
        name = d["person"]
        u = d["usage"]
        if u["over"]:
            status = "⚠️ تجاوزت"
        elif u["percent"] >= 80:
            status = "⚠️ قريب من السقف"
        else:
            status = "ضمن الحدود"
        lines.append(
            f"• {name}: الدين {_fmt_amount(u['outstanding'])} / {_fmt_amount(u['limit'])} "
            f"({u['percent']}%) — {status}"
        )
    return "\n".join(lines)


# ---------- إحصائيات الاستخدام (/stats) ----------


def _size_label(size_bytes: int | None) -> str:
    if not size_bytes:
        return "غير متاح"
    value = float(size_bytes)
    for unit in ("بايت", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "غير متاح"


def format_user_stats(stats: dict) -> str:
    """يعرض ملخص استخدام البوت حسب مساحة عمل المستخدم."""
    tx = stats["transactions"]
    exp_lines = _totals_line(stats["expenses_by_currency"]) if stats["expenses_by_currency"] else "—"
    inc_lines = _totals_line(stats["incomes_by_currency"]) if stats["incomes_by_currency"] else "—"

    month = stats["current_month"] or {}
    month_label = month.get("label", "")
    month_totals = month.get("by_currency", {}) if month else {}
    month_line = _totals_line(month_totals) if month_totals else "—"
    unified = None
    if month:
        unified = _unified_amount(month_totals, stored=month.get("stored"))

    peak = stats["peak_hour_local"]
    if peak is None:
        peak_line = "لا توجد عمليات خلال آخر 90 يومًا"
    else:
        peak_line = f"{peak:02d}:00 ({stats['peak_activity']} عملية)"

    lines = [
        "📊 إحصائيات استخدامك:\n",
        f"👥 حجم مساحة العمل: {stats['workspace_size']} حساب",
        f"💳 العمليات الكلية: {tx['total']} (مصروف {tx['expense']} · إيراد {tx['income']})",
        f"📉 المصاريف: {exp_lines}",
        f"📈 الإيرادات: {inc_lines}",
        f"🗓️ الشهر الحالي ({month_label}): مصاريف {month_line}",
    ]
    if unified is not None:
        lines.append(f"   الموحّد (أسعار مثبّتة): {_fmt_amount(unified)} {stats['current_month'].get('stored_base') or ''}".rstrip())
    lines += [
        f"🛒 الطلبيات: مفتوحة {stats['orders']['open']} · منجزة {stats['orders']['done']}",
        f"🧾 الفواتير: معلّقة {stats['invoices']['pending']} · مسددة {stats['invoices']['paid']} · متأخرة {stats['invoices']['overdue']}",
        f"📋 المهام: معلّقة {stats['tasks']['pending']} · منجزة {stats['tasks']['done']}",
        f"🎯 الميزانيات: {stats['budgets']} · الحدود الائتمانية: {stats['credit_limits']}",
        f"⏰ ذروة نشاطك: {peak_line}",
        f"🗄️ حجم قاعدة البيانات: {_size_label(stats.get('db_size_bytes'))}",
    ]
    return "\n".join(lines)


# ---------- الفحص الصحي (/health) ----------


def format_health_report(checks: list[dict]) -> str:
    """يهيّئ نص تقرير الفحص الصحي من قائمة {ok, label, detail}."""
    if not checks:
        return "لا توجد فحوصات."
    lines = ["🩺 الفحص الصحي للنظام:\n"]
    for c in checks:
        mark = "✅" if c.get("ok") else ("⚠️" if c.get("warn") else "❌")
        detail = c.get("detail") or ""
        line = f"{mark} {c.get('label', '')}"
        if detail:
            line += f" — {detail}"
        lines.append(line)
    return "\n".join(lines)


# ---------- التنبؤات (/forecast) ----------


def format_forecast(payload: dict) -> str:
    """يعرض توقعات المصاريف/الإيرادات للأشهر القادمة."""
    months = payload.get("months") or []
    if not months:
        return "لا توجد بيانات كافية للتنبؤ بعد — أضف عمليات على الأقل شهرين."

    lines = [
        "🔮 توقعات الأشهر القادمة (نموذج تبسيطي إرشادي)\n",
        f"مبنية على متوسط اتجاه آخر {payload.get('history', 6)} أشهر (حتى {payload.get('last_key', '—')}).",
    ]
    for m in months:
        exp_parts = [f"{_fmt_amount(v)} {c}" for c, v in m["expense"].items() if v]
        inc_parts = [f"{_fmt_amount(v)} {c}" for c, v in m["income"].items() if v]
        exp_line = " + ".join(exp_parts) if exp_parts else "—"
        inc_line = " + ".join(inc_parts) if inc_parts else "—"
        unified_line = ""
        if m.get("unified_expense") is not None:
            unified_line = (
                f"\n   📉 الموحّد المتوقع (بعملة الأساس {payload.get('base', '')}): "
                f"{_fmt_amount(m['unified_expense'])}"
            )
        lines.append(f"\n🗓️ {m['label']}:\n• مصاريف: {exp_line}{unified_line}\n• إيرادات: {inc_line}")
    lines.append("\n⚠️ التوقعات إرشادية ولا تُغني عن التخطيط الفعلي.")
    return "\n".join(lines)


# ---------- انحراف الإنفاق (/deviation) ----------


def format_deviation(payload: dict) -> str:
    """يعرض انحراف الشهر الحالي عن متوسط الأشهر الثلاثة السابقة."""
    deviations = payload.get("deviations") or []
    if not deviations:
        return "لا توجد انحرافات ملحوظة هذا الشهر مقابل متوسط آخر 3 أشهر."

    kind_name = {"expense": "مصاريف", "income": "إيرادات"}
    lines = ["📉 انحراف الشهر الحالي مقابل متوسط آخر 3 أشهر:"]
    for d in deviations:
        arrow = "⬆️ فوق المتوسط" if d["pct"] >= 0 else "⬇️ تحت المتوسط"
        lines.append(
            f"• {kind_name.get(d['kind'], d['kind'])} ({d['currency']}): "
            f"{_fmt_amount(d['current'])} مقابل متوسط {_fmt_amount(d['average'])} "
            f"({d['pct']}%) — {arrow} {'⚠️' if d['significant'] else ''}"
        )
    lines.append(f"\nحدّ الإشارة: انحراف ≥ {payload.get('threshold_pct', 30)}%.")
    return "\n".join(lines)
