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
from bot.icons import ERROR, EXPENSE, HOME, INCOME, SUCCESS, TASK, WARNING

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
        lines.append(f"{EXPENSE} {cur_label}: {_totals_line(cur_exp) or 'لا توجد'}")
        lines.append(f"📅 {prev_label}: {_totals_line(prev_exp) or 'لا توجد'}")
        delta = _compare_delta(cur_exp, prev_exp)
        if delta:
            lines.append(delta)

    if cur_inc or prev_inc:
        lines.append("")
        lines.append(f"{INCOME} الإيرادات — {cur_label}: {_totals_line(cur_inc) or 'لا توجد'}")
        lines.append(f"{prev_label}: {_totals_line(prev_inc) or 'لا توجد'}")

    if query_result.get("partial"):
        pct = query_result.get("elapsed_pct")
        hint = f"مضى نحو {pct}%" if pct is not None else "لم تكتمل بعد"
        lines.append(
            f"\n⚠️ {cur_label} {hint} فقط — مقارنته بفترة سابقة كاملة مضلِّلة:"
            " المبالغ تبدو أقل/أعلى لمجرد أن الفترة الحالية لم تنتهِ بعد."
        )

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
                InlineKeyboardButton(f"{SUCCESS} تأكيد", callback_data="confirm:yes"),
                InlineKeyboardButton("✏️ تعديل", callback_data="confirm:edit"),
            ],
            [
                InlineKeyboardButton("🔁 تكرار العملية", callback_data="confirm:repeat"),
                InlineKeyboardButton(f"{HOME} القائمة الرئيسية", callback_data="menu:main"),
            ],
            [
                InlineKeyboardButton("❌ إلغاء", callback_data="confirm:no"),
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
                net_txt = f"{SUCCESS} {_fmt_amount(net)} {base} لك (يدين لك)"
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
            "overdue": WARNING,
            "paid": SUCCESS,
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
        flag = "⏳ مفتوحة" if status == "open" else f"{SUCCESS} منجزة"
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
        if not u.get("unified_ok"):
            status = "⚠️ عملات متعددة بلا توحيد"
            value_txt = "غير متاح"
        else:
            if u["over"]:
                status = "⚠️ تجاوزت"
            elif u["percent"] >= 80:
                status = "⚠️ قريب من السقف"
            else:
                status = "ضمن الحدود"
            if u["side"] == "receivable":
                value_txt = f"لك عليه {_fmt_amount(u['amount'])}"
            elif u["side"] == "payable":
                value_txt = f"عليك له {_fmt_amount(u['outstanding'])}"
            else:
                value_txt = "صفر"
        percent_txt = f"{u['percent']}%" if u["percent"] is not None else "غير متاح"
        lines.append(
            f"• {name}: {value_txt} / {_fmt_amount(u['limit'])} "
            f"({percent_txt}) — {status}"
        )
    return "\n".join(lines)


# ---------- البطاقة المالية الموحّدة (الذمم/فواتير/حدود ائتمانية) ----------


def format_finance_card(
    debts: list[dict],
    invoices: list[Invoice],
    credit_limits: list[dict],
) -> str:
    """بطاقة واحدة تجمع: من يدين لي/أنا مدين لمن + الفواتير + الحدود الائتمانية."""
    lines = ["💳 بطاقة الذمم المالية:\n"]

    # ١) الأرصدة مع الأشخاص (ديون)
    owes_me = [d for d in debts if (d.get("balance_unified") or 0) > 0]
    i_owe = [d for d in debts if (d.get("balance_unified") or 0) < 0]
    if debts:
        lines.append(f"من يدين لي ({len(owes_me)}):")
        if owes_me:
            for d in owes_me[:5]:
                base = d.get("base") or ""
                lines.append(f"  {SUCCESS} {d['person']} — {_fmt_amount(d['balance_unified'])} {base}")
        else:
            lines.append("  لا أحد")
        lines.append(f"أنا مدين لـ ({len(i_owe)}):")
        if i_owe:
            for d in i_owe[:5]:
                base = d.get("base") or ""
                lines.append(f"  {EXPENSE} {d['person']} — {_fmt_amount(abs(d['balance_unified']))} {base}")
        else:
            lines.append("  لا أحد")
    else:
        lines.append("لا توجد أرصدة مع أشخاص بعد.")

    # ٢) الفواتير الآجلة
    active = [i for i in invoices if i.status in ("pending", "overdue")]
    overdue_inv = [i for i in invoices if i.status == "overdue"]
    lines.append("")
    if active:
        due_totals: dict[str, Decimal] = {}
        for inv in active:
            cur = inv.currency or "غير محددة"
            due_totals[cur] = due_totals.get(cur, Decimal("0")) + (inv.amount or Decimal("0"))
        total_txt = _totals_line(due_totals)
        lines.append(
            f"الفواتير الآجلة: {len(active)} (منها {len(overdue_inv)} {WARNING} متأخرة) — "
            f"الإجمالي {total_txt}"
        )
        for inv in active[:5]:
            due_txt = ""
            if inv.due_date:
                from app.timeutil import to_local_naive

                due_txt = to_local_naive(inv.due_date).strftime("%Y-%m-%d")
            status_txt = WARNING if inv.status == "overdue" else "⏳"
            lines.append(
                f"  {status_txt} #{inv.id} {inv.person or 'بدون شخص'}: {_fmt_amount(inv.amount)} "
                f"{inv.currency or ''}{' — يستحق ' + due_txt if due_txt else ''}"
            )
    else:
        lines.append(f"لا فواتير مستحقة {SUCCESS}")

    # ٣) الحدود الائتمانية
    lines.append("")
    if credit_limits:
        over = [d for d in credit_limits if d["usage"].get("over")]
        if over:
            names = ", ".join(d["person"] for d in over[:3])
            lines.append(f"{WARNING} تجاوز حدّ: {names}")
        else:
            unified_ok = [d for d in credit_limits if d["usage"].get("unified_ok")]
            near = [
                d
                for d in unified_ok
                if d["usage"].get("percent") is not None
                and d["usage"]["percent"] >= 80
            ]
            if near:
                names = ", ".join(d["person"] for d in near[:3])
                lines.append(f"⚠️ قريب من السقف: {names}")
            else:
                lines.append(f"الحدود الائتمانية ضمن الحدود {SUCCESS}")
    else:
        lines.append("لا حدود ائتمانية مُعدّة (/credit إضافة <الشخص> <المبلغ>).")

    lines.append("\nللتفصيل: /debts و /invoices و /credit")
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
        f"{TASK} المهام: معلّقة {stats['tasks']['pending']} · منجزة {stats['tasks']['done']}",
        f"🎯 الميزانيات: {stats['budgets']} · الحدود الائتمانية: {stats['credit_limits']}",
        f"⏰ ذروة نشاطك: {peak_line}",
        f"🗄️ حجم قاعدة البيانات: {_size_label(stats.get('db_size_bytes'))}",
    ]
    return "\n".join(lines)


# ---------- لوحة مؤشرات الأداء (/kpi) ----------


def format_kpi_dashboard(payload: dict) -> str:
    """يعرض لوحة KPI مختصرة: مال الشهر الحالي/السابق + مهام/فواتير/طلبيات/ميزانيات."""
    cm = payload["current_month"]
    pm = payload["prev_month"]
    base = payload["base"] or ""

    def _money(value, suffix: str = "") -> str:
        if value is None:
            return f"غير متاح {suffix}".rstrip()
        return f"{_fmt_amount(value)} {suffix}".rstrip()

    def _delta_txt(key: str, label: str) -> str:
        pct = payload.get(key)
        if pct is None:
            return ""
        if abs(pct) < 0.05:
            return f"   {label}: بلا تغيير يُذكر"
        if pct > 0:
            return f"   {label}: ▲ +{pct:.1f}%"
        return f"   {label}: ▼ {pct:.1f}%"

    lines = [f"🧭 لوحة مؤشرات أعمالك — {payload['as_of']}\n"]

    lines.append(f"📅 الشهر الحالي ({cm['label']}):")
    lines.append(f"{INCOME} إيرادات: {_money(cm['income'], base)}")
    lines.append(f"{EXPENSE} مصاريف: {_money(cm['expense'], base)}")
    lines.append(f"💰 الصافي: {_money(cm['net'], base)}")

    delta_lines = [
        d
        for d in (_delta_txt("expense_vs_prev_pct", "مصاريف عن السابق"), _delta_txt("income_vs_prev_pct", "إيرادات عن السابق"))
        if d
    ]
    if delta_lines:
        lines.append("")
        lines.append(f"📈 مقارنة مع الشهر السابق ({pm['label']}):")
        lines.extend(delta_lines)
    else:
        lines.append("")

    tk = payload["tasks"]
    iv = payload["invoices"]
    bg = payload["budgets"]
    overdue_tasks_txt = f" ({tk['overdue']} {WARNING} متأخرة)" if tk["overdue"] else ""
    overdue_inv_txt = f" ({iv['overdue']} {WARNING} متأخرة)" if iv["overdue"] else ""
    lines += [
        f"{TASK} المهام: {tk['pending']} معلّقة{overdue_tasks_txt}",
        f"🧾 الفواتير: {iv['pending']} آجلة{overdue_inv_txt}",
        f"🛒 الطلبيات المفتوحة: {payload['orders_open']}",
    ]
    if bg["total"]:
        warn = []
        if bg["over"]:
            warn.append(f"{bg['over']} تجاوزت السقف {WARNING}")
        if bg["near"]:
            warn.append(f"{bg['near']} تقترب من السقف")
        suffix = f" — {' و '.join(warn)}" if warn else ""
        lines.append(f"🎯 الميزانيات: {bg['total']} مفعّلة{suffix}")
    if payload.get("debts_net") is not None:
        dn = payload["debts_net"]
        if dn > 0:
            lines.append(f"💳 صافي الذمم: {_money(dn, base)} لك (يدين لك الآخرون)")
        elif dn < 0:
            lines.append(f"💳 صافي الذمم: {_money(-dn, base)} عليك (تدين أنت)")
        else:
            lines.append("💳 صافي الذمم: متوازن 0")

    top = payload.get("top_categories") or []
    if top:
        lines.append("")
        lines.append("🏆 أكثر تصنيفات الإنفاق هذا الشهر:")
        for c in top:
            suffix = f" ({c['count']} عملية)" if c["count"] > 1 else ""
            lines.append(f"  {EXPENSE} {c['category']}: {_money(c['amount'], base)}{suffix}")

    lines.append("\nللتفصيل: /report · /finance · /deviation · /forecast")
    return "\n".join(lines)


def format_brief_data(payload: dict) -> str:
    """يحوّل بيانات لوحة KPI إلى نص سردي مضغوط بأرقام فقط — يُغذّى للذكاء.

    بلا تنسيق رسائل (لا أيقونات/توابع) لأن المخرج يُرسَل كبيانات سياق لـ Gemini،
    والمتلقّي النهائي يُنسَّق عنونةً في generate_daily_brief. أي وحدة نقدية
    تُكتب مع المبلغ، والقيم غير المتاحة تُترك صراحةً (غير متاح) لا أرقامًا.
    """
    from decimal import Decimal

    cm = payload["current_month"]
    pm = payload["prev_month"]
    base = payload["base"] or ""
    tk = payload["tasks"]
    iv = payload["invoices"]
    bg = payload["budgets"]
    dn = payload.get("debts_net")

    def _num(value) -> str:
        if value is None:
            return "غير متاح"
        if isinstance(value, Decimal):
            return f"{value}"
        return f"{value}"

    lines = [
        f"التاريخ المرجعي: {payload['as_of']}",
        f"العملة الأساس: {base or 'غير محددة'}",
        f"الشهر الحالي ({cm['label']}):",
        f"  الإيرادات المجمّعة: {_num(cm['income'])} {base}",
        f"  المصاريف المجمّعة: {_num(cm['expense'])} {base}",
        f"  الصافي: {_num(cm['net'])} {base}",
    ]
    if pm.get("label"):
        lines.append(f"الشهر السابق ({pm['label']}):")
        lines.append(f"  الإيرادات: {_num(pm.get('income'))} · المصاريف: {_num(pm.get('expense'))}")
    for key, label in (("expense_vs_prev_pct", "نسبة تغيّر المصاريف"), ("income_vs_prev_pct", "نسبة تغيّر الإيرادات")):
        pct = payload.get(key)
        lines.append(f"  {label} مقابل الشهر السابق: {pct}%" if pct is not None else f"  {label}: غير متاح")
    lines += [
        f"المهام المعلّقة: {tk['pending']} · المتأخرة: {tk['overdue']}",
        f"الفواتير الآجلة: {iv['pending']} · المتأخرة: {iv['overdue']}",
        f"الطلبيات المفتوحة: {payload['orders_open']}",
        f"الميزانيات: {bg['total']} مفعّلة · تجاوزت السقف {bg['over']} · تقترب {bg['near']}",
        (
            f"صافي الذمم (لك/عليك): {_num(dn)} {base}"
            if dn is not None
            else "صافي الذمم: غير متاح"
        ),
        "أعلى تصنيفات الإنفاق هذا الشهر:",
    ]
    top = payload.get("top_categories") or []
    if top:
        for c in top[:3]:
            lines.append(f"  {c['category']}: {_num(c['amount'])} {base} ({c['count']} عملية)")
    else:
        lines.append("  لا توجد مصاريف هذا الشهر")
    return "\n".join(lines)


# ---------- الفحص الصحي (/health) ----------


def format_health_report(checks: list[dict]) -> str:
    """يهيّئ نص تقرير الفحص الصحي من قائمة {ok, label, detail}."""
    if not checks:
        return "لا توجد فحوصات."
    lines = ["🩺 الفحص الصحي للنظام:\n"]
    for c in checks:
        mark = SUCCESS if c.get("ok") else (WARNING if c.get("warn") else ERROR)
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
    if payload.get("month_partial"):
        pct = payload.get("month_elapsed_pct")
        hint = f"مضى نحو {pct}%" if pct is not None else "لم يكتمل بعد"
        lines.append(
            f"\n⚠️ الشهر الحالي {hint} — المقارنة مع متوسط أشهر كاملة تقريبية"
            " وقد تكون مضلِّلة حتى اكتمال الشهر."
        )
    return "\n".join(lines)
