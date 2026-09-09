"""
قوائم InlineKeyboard للبوت: القائمة الرئيسية + القوائم الفرعية + Router موحّد.

نظام موحّد للـ callback_data بصيغة: action:target:extra
  menu:main | menu:record | menu:reports | menu:tasks | menu:settings
  rec:expense | rec:income | rec:order | rec:note | rec:task
  tsk:list:pending | tsk:list:overdue | tsk:list:done
  tsk:done:<id> | tsk:del:<id> | tsk:edit:<id>
  rpt:period | rpt:p:<period> | rpt:m:<metric>:<period>
  set:on | set:off

أزرار شاشة التأكيد (confirm:*) وحقول التعديل (edit:*/editfield:*) تُدار من
ConversationHandlers الخاصة بها (record_flow / edit_flow) — يقع الـ router هنا في
نهاية سلسلة المعالجات كي لا يعترض ما تديره المحادثات.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.database.crud import (
    complete_task,
    delete_record_by_id,
    delete_task_by_id,
    get_record_by_id,
    list_done_tasks,
    list_overdue_tasks,
    list_pending_tasks,
    list_recent_records,
    mark_overdue_tasks,
    run_query,
    search_records,
)
from app.database.db import SessionLocal
from app.timeutil import to_local_naive
from bot.conversation import _clear_all_pending
from bot.formatters import format_query_result
from bot.i18n import remember_lang, t, user_lang

logger = logging.getLogger(__name__)


def build_menu(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """يبني لوحة أزرار من صفوف [(النص, callback_data)] — أداة قياسية لكل القوائم."""
    keyboard = [
        [InlineKeyboardButton(label, callback_data=cb) for label, cb in row] for row in rows
    ]
    return InlineKeyboardMarkup(keyboard)


# ---------- القوائم (تصميم صوتي) ----------

MAIN_MENU_TEXT = (
    "القائمة الرئيسية — اختر ما تريد:\n"
    "أو اكتب مباشرة أي عملية أو سؤال (مثل: 300 شيكل لمحمد مقابل مواد)."
)

MAIN_MENU = [
    [("💰 تسجيل عملية", "menu:record")],
    [("📊 التقارير", "menu:reports")],
    [("📋 مهامي", "menu:tasks")],
    [("🧰 أدوات", "menu:tools")],
    [("⚙️ الإعدادات", "menu:settings")],
]

MAIN_HOME_KEYBOARD = build_menu(MAIN_MENU)

RECORD_MENU = [
    [("💸 مصروف", "rec:expense"), ("💵 إيراد", "rec:income")],
    [("🛒 طلبية", "rec:order"), ("📝 ملاحظة", "rec:note")],
    [("⏰ مهمة جديدة", "rec:task")],
    [("⬅️ رجوع", "menu:main")],
]

RECORD_HINTS = {
    "expense": "💸 مصروف — أرسل التفاصيل.\nمثال: 300 شيكل لمحمد مقابل مواد",
    "income": "💵 إيراد — أرسل التفاصيل.\nمثال: استلمت 500 دولار من أحمد",
    "order": "🛒 طلبية — أرسل التفاصيل.\nمثال: طلبية 5 صناديق من مورّد الرياض",
    "note": "📝 ملاحظة — أرسل التفاصيل.\nمثال: ملاحظة: سيأتي العمال يوم السبت",
    "task": "⏰ مهمة جديدة — أرسل التفاصيل.\nمثال: مهمة الاتصال بسامر غدًا الساعة 10",
}

TASK_STATUS_MENU = [
    [("⏳ المعلّقة", "tsk:list:pending")],
    [("⚠️ المتأخرة", "tsk:list:overdue")],
    [("✅ المنجزة", "tsk:list:done")],
    [("⬅️ رجوع", "menu:main")],
]

PERIOD_LABELS = {
    "today": "اليوم 📅",
    "this_week": "هذا الأسبوع 🗓",
    "this_month": "هذا الشهر 🗓",
    "this_year": "هذه السنة 🗓",
    "all_time": "كل الفترات 🌐",
}

REPORT_PERIODS = [
    [("📅 اليوم", "rpt:p:today"), ("🗓 هذا الأسبوع", "rpt:p:this_week")],
    [("🗓 هذا الشهر", "rpt:p:this_month"), ("🗓 هذه السنة", "rpt:p:this_year")],
    [("🌐 كل الفترات", "rpt:p:all_time")],
    [("⬅️ رجوع", "menu:main")],
]

REPORT_METRICS = [
    [("💸 المصاريف", "rpt:m:total_expenses:__PERIOD__")],
    [("💰 الإيرادات", "rpt:m:total_income:__PERIOD__")],
    [("🔢 عدد العمليات", "rpt:m:count_transactions:__PERIOD__")],
    [("⬅️ فترة أخرى", "rpt:period")],
]

METRIC_LABELS = {
    "total_expenses": "المصاريف 💸",
    "total_income": "الإيرادات 💰",
    "count_transactions": "عدد العمليات 🔢",
}

SETTINGS_MENU = [
    [("📅 تفعيل التقرير الدوري", "set:on")],
    [("🔕 إيقاف التقرير الدوري", "set:off")],
    [("⬅️ رجوع", "menu:main")],
]

SETTINGS_DOCS = {
    "on": "لتفعيل التقرير الدوري أرسل أحد الأوامر:\n"
    "• /report_on 1d — تلخيص يومي\n"
    "• /report_on 1w — تقرير أسبوعي\n"
    "• /report_on 1m — تقرير شهري",
    "off": "لإيقاف التقارير الدورية أرسل: /report_off",
}

TOOLS_MENU = [
    [("✏️ تعديل آخر سجل", "el:last")],
    [("🕘 آخر العمليات", "his:p:1")],
    [("🔍 بحث", "sb:start")],
    [("💰 الميزانيات", "bg:list")],
    [("📈 الرسم البياني", "tool:chart")],
    [("📦 تصدير Excel", "ex:menu")],
    [("💱 تحويل عملة", "tool:convert")],
    [("🏢 المساحة المشتركة", "ws:status")],
    [("⬅️ رجوع", "menu:main")],
]

EXPORT_MENU = [
    [("🌐 كل السجلات", "ex:p:all")],
    [("📅 اليوم", "ex:p:today")],
    [("🗓 الأسبوع", "ex:p:week")],
    [("🗓 الشهر", "ex:p:month")],
    [("⬅️ رجوع", "menu:tools")],
]

EXPORT_CAPTIONS = {
    "all": "تصدير (كل السجلات)",
    "today": "تصدير (اليوم)",
    "week": "تصدير (الأسبوع)",
    "month": "تصدير (الشهر)",
}


def _main_menu_keyboard(lang: str = "ar") -> InlineKeyboardMarkup:
    rows = [
        [(t("btn_record", lang), "menu:record")],
        [(t("btn_reports", lang), "menu:reports")],
        [(t("btn_tasks", lang), "menu:tasks")],
        [(t("btn_tools", lang), "menu:tools")],
        [(t("btn_settings", lang), "menu:settings")],
    ]
    return build_menu(rows)


def _tools_keyboard(lang: str = "ar") -> InlineKeyboardMarkup:
    base = [
        [("el:last", t("btn_edit_last", lang))],
        [("his:p:1", t("btn_history", lang))],
        [("sb:start", t("btn_search", lang))],
        [("bg:list", t("btn_budget", lang))],
        [("tool:chart", t("btn_chart", lang))],
        [("ex:menu", t("btn_export", lang))],
        [("tool:convert", t("btn_convert", lang))],
        [("ws:status", t("btn_workspace", lang))],
        [("menu:main", t("btn_back", lang))],
    ]
    return build_menu([[(label, cb) for cb, label in row] for row in base])


def _report_metrics_keyboard(period: str) -> InlineKeyboardMarkup:
    rows = [
        [(label, cb.replace("__PERIOD__", period)) for label, cb in row] for row in REPORT_METRICS
    ]
    rows.append([("🏠 القائمة الرئيسية", "menu:main")])
    return build_menu(rows)


def _home_keyboard(*extra_rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [list(row) for row in extra_rows]
    rows.append([("🏠 القائمة الرئيسية", "menu:main")])
    return build_menu(rows)


async def send_main_menu(message, context, text: str = ""):
    """يرسل القائمة الرئيسية كرسالة جديدة (لأوامر /start و /menu)."""
    _clear_all_pending(context)
    lang = (
        user_lang(getattr(message, "from_user", None).id)
        if getattr(message, "from_user", None)
        else "ar"
    )
    body = text or t("main_title", lang)
    await message.reply_text(body, reply_markup=_main_menu_keyboard(lang))


# ---------- رصف القوائم إداريًا ----------

PAGES = {
    "record": ("تسجيل عملية — ماذا تريد أن تسجّل؟", RECORD_MENU),
    "reports": ("التقارير — اختر الفترة:", REPORT_PERIODS),
    "tasks": ("مهامي — اختر الحالة:", TASK_STATUS_MENU),
    "tools": ("🧰 أدوات — اختر:", TOOLS_MENU),
    "settings": ("الإعدادات:", SETTINGS_MENU),
}


async def _handle_menu(query, context, parts: list):
    lang = user_lang(query.from_user.id)
    target = parts[0] if parts else "main"
    if target == "main":
        _clear_all_pending(context)
        await query.edit_message_text(t("main_title", lang), reply_markup=_main_menu_keyboard(lang))
        return
    if target == "tools":
        title = t("tools_title", lang)
        await query.edit_message_text(title, reply_markup=_tools_keyboard(lang))
        return
    title, rows = PAGES.get(target, ("القائمة الرئيسية", MAIN_MENU))
    await query.edit_message_text(title, reply_markup=build_menu(rows))


async def _handle_record(query, context, parts: list):
    rtype = parts[0] if parts else ""
    if rtype not in RECORD_HINTS:
        await query.edit_message_text("اختر نوع العملية:", reply_markup=build_menu(RECORD_MENU))
        return
    context.user_data["record_seed_type"] = rtype
    await query.edit_message_text(
        f"{RECORD_HINTS[rtype]}\n\n(أرسل /cancel لإلغاء)",
        reply_markup=_home_keyboard(),
    )


# ---------- مهامي: قائمة + أزرار لكل عنصر ----------

TASK_STATUS_TITLES = {
    "pending": "المعلّقة ⏳",
    "overdue": "المتأخرة ⚠️",
    "done": "المنجزة ✅",
}


def _task_line(task) -> str:
    parts = [task.description or "(بدون وصف)"]
    if getattr(task, "person", None):
        parts.append(f"👤 {task.person}")
    if getattr(task, "due_date", None):
        parts.append(f"🕒 {to_local_naive(task.due_date).strftime('%Y-%m-%d %H:%M')}")
    if getattr(task, "priority", "normal") == "high":
        parts.append("⚡ عاجلة")
    if getattr(task, "recurrence_rule", None):
        badge = {"daily": "🔁 يومي", "weekly": "🔁 أسبوعي", "monthly": "🔁 شهري"}.get(
            task.recurrence_rule, "🔁"
        )
        parts.append(badge)
    if getattr(task, "status", None) == "done":
        parts.append("✅")
    return " — ".join(parts)


def build_task_list(tasks, status: str) -> tuple[str, InlineKeyboardMarkup]:
    """يبني (نص, لوحة أزرار) لقائمة مهام — كل مهمة بصف أزرار [إنجاز|تعديل|حذف]."""
    if not tasks:
        text = f"لا توجد مهام {TASK_STATUS_TITLES.get(status, '')} 🎉"
        return text, _home_keyboard()

    lines = [f"📋 المهام {TASK_STATUS_TITLES.get(status, '')}:"]
    rows = []
    for i, task in enumerate(tasks[:10], start=1):
        lines.append(f"{i}. {_task_line(task)}")
        buttons = [
            InlineKeyboardButton("✅", callback_data=f"tsk:done:{task.id}"),
            InlineKeyboardButton("✏️", callback_data=f"tsk:edit:{task.id}"),
            InlineKeyboardButton("🗑️", callback_data=f"tsk:del:{task.id}"),
        ]
        if status == "done":
            buttons = [InlineKeyboardButton("🗑️", callback_data=f"tsk:del:{task.id}")]
        rows.append(buttons)

    rows.append([InlineKeyboardButton("⬅️ رجوع للقائمة", callback_data="menu:tasks")])
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")])
    return "\n".join(lines) + "\n\n(✅ إنجاز • ✏️ تعديل • 🗑️ حذف)", InlineKeyboardMarkup(rows)


async def _handle_task_list(query, context, status: str):
    uid = query.from_user.id
    db = SessionLocal()
    try:
        mark_overdue_tasks(db, uid)
        if status == "overdue":
            tasks = list_overdue_tasks(db, uid, limit=10)
        elif status == "done":
            tasks = list_done_tasks(db, uid, limit=10)
        else:
            tasks = list_pending_tasks(db, uid, limit=10)
    finally:
        db.close()
    text, kb = build_task_list(tasks, status)
    await query.edit_message_text(text, reply_markup=kb)
    _clear_all_pending(context)


async def _task_save_ack(query, context, prefix, task):
    text = f"{prefix}{task.description}"
    await query.edit_message_text(text, reply_markup=_home_keyboard())


async def _handle_task_done(query, context, task_id: str):
    uid = query.from_user.id
    db = SessionLocal()
    try:
        task = complete_task(db, uid, int(task_id))
    finally:
        db.close()
    if task:
        await _task_save_ack(query, context, "✅ تم إنجاز المهمة:\n", task)
    else:
        await query.edit_message_text(
            "لم أجد هذه المهمة (ربما حُذفت).", reply_markup=_home_keyboard()
        )


async def _handle_task_delete(query, context, task_id: str):
    uid = query.from_user.id
    db = SessionLocal()
    try:
        task = delete_task_by_id(db, uid, int(task_id))
    finally:
        db.close()
    if task:
        await _task_save_ack(query, context, "🗑️ حُذفت المهمة:\n", task)
    else:
        await query.edit_message_text("لا يمكن حذف هذه المهمة الآن.", reply_markup=_home_keyboard())


async def _handle_task_edit(query, context, task_id: str):
    uid = query.from_user.id
    db = SessionLocal()
    try:
        task, _ = get_record_by_id(db, uid, "Task", int(task_id))
    finally:
        db.close()
    if task is None:
        await query.edit_message_text(
            "لم أجد هذه المهمة (ربما حُذفت).", reply_markup=_home_keyboard()
        )
        return
    context.user_data["pending_task_edit_id"] = task.id
    await query.edit_message_text(
        f"أرسل الوصف الجديد للمهمة:\nالوصف الحالي: {task.description}\n\n(أرسل /cancel للإلغاء)"
    )


async def _handle_task(query, context, parts: list):
    if not parts:
        await query.edit_message_text(
            "مهامي — اختر الحالة:", reply_markup=build_menu(TASK_STATUS_MENU)
        )
        return
    act = parts[0]
    if act == "list":
        await _handle_task_list(query, context, parts[1] if len(parts) > 1 else "pending")
    elif act == "done" and len(parts) > 1:
        await _handle_task_done(query, context, parts[1])
    elif act == "del" and len(parts) > 1:
        await _handle_task_delete(query, context, parts[1])
    elif act == "edit" and len(parts) > 1:
        await _handle_task_edit(query, context, parts[1])


# ---------- التقارير: فترة ← نوع ----------


async def _handle_report(query, context, parts: list):
    if not parts or parts[0] == "period":
        await query.edit_message_text(
            "التقارير — اختر الفترة:", reply_markup=build_menu(REPORT_PERIODS)
        )
        return
    if parts[0] == "p":
        period = parts[1] if len(parts) > 1 else "all_time"
        title = f"التقارير — {PERIOD_LABELS.get(period, period)}:\nاختر نوع التقرير:"
        await query.edit_message_text(title, reply_markup=_report_metrics_keyboard(period))
        return
    if parts[0] == "m" and len(parts) > 2:
        metric, period = parts[1], parts[2]
        uid = query.from_user.id
        db = SessionLocal()
        try:
            result = run_query(db, uid, {"metric": metric, "period": period})
        finally:
            db.close()
        metric_label = METRIC_LABELS.get(metric, metric)
        header = f"📊 {metric_label} — {PERIOD_LABELS.get(period, period)}:\n"
        await query.edit_message_text(
            header + format_query_result(result),
            reply_markup=_home_keyboard([("⬅️ فترة أخرى", "rpt:period")]),
        )
        return
    await query.edit_message_text(
        "التقارير — اختر الفترة:", reply_markup=build_menu(REPORT_PERIODS)
    )


async def _handle_settings(query, context, parts: list):
    key = parts[0] if parts else ""
    if key not in SETTINGS_DOCS:
        await query.edit_message_text("الإعدادات:", reply_markup=build_menu(SETTINGS_MENU))
        return
    await query.edit_message_text(SETTINGS_DOCS[key], reply_markup=_home_keyboard())


# ---------- أدوات: ميزانيات / رسم / تصدير / تحويل ----------


def export_period_start(period: str) -> tuple[object | None, str]:
    """حدود فترة التصدير (بحسب الآن المحلي، بلغة UTC). يعيد (start_datetime, label)."""
    from datetime import timedelta

    from app.timeutil import first_day_of_week, now_local, to_utc_naive

    local_now = now_local()
    if period == "today":
        local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return to_utc_naive(local), "today"
    if period == "week":
        fd = first_day_of_week()
        local = local_now - timedelta(days=(local_now.weekday() - fd) % 7)
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return to_utc_naive(local), "week"
    if period == "month":
        local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return to_utc_naive(local), "month"
    return None, "all"


def _budget_list_payload(db, telegram_user_id: int) -> tuple[list[str], list]:
    """سطور ميزانيات المستخدم (مع الاستخدام الشهري الحالي) + كائناتها للزر لكل عنصر."""
    from app.database.crud import budget_monthly_reset, budget_usage, list_budgets
    from app.exchange import CURRENCY_NAMES

    budgets = list_budgets(db, telegram_user_id)
    lines = ["ميزانياتك الشهرية:\n"]
    if not budgets:
        return ["لا توجد ميزانيات بعد.\nلمزيد: /budget إضافة عملة ILS 2000"], budgets

    for b in budgets:
        budget_monthly_reset(db, b)
        usage = budget_usage(db, b)
        target_txt = (
            CURRENCY_NAMES.get(b.currency, b.currency)
            if b.scope == "currency"
            else f"الشخص {b.person}"
        )
        if usage["over"]:
            status = "⚠️ تجاوزت"
        elif usage["percent"] >= 80:
            status = "⚠️ قريب من السقف"
        else:
            status = "ضمن الحدود"
        lines.append(
            f"#{b.id} {target_txt}: {usage['spent']} / {usage['limit']} "
            f"({usage['percent']}%) — {status}"
        )
    return lines, budgets


async def _handle_budget_list(query, context):
    uid = query.from_user.id
    db = SessionLocal()
    try:
        lines, budgets = _budget_list_payload(db, uid)
    finally:
        db.close()

    rows = [[("➕ إضافة (عملة)", "bg:add:currency"), ("➕ إضافة (شخص)", "bg:add:person")]]
    for b in budgets:
        rows.append([("🗑️ حذف #" + str(b.id), f"bg:del:{b.id}")])
    rows.append([("🏠 القائمة الرئيسية", "menu:main")])
    await query.edit_message_text("\n".join(lines), reply_markup=build_menu(rows))


async def _handle_budget_add(query, context, scope: str):
    context.user_data["pending_budget"] = {"scope": scope}
    if scope == "person":
        prompt = "أرسل اسم الشخص والمبلغ:\nمثال — محمد 1500\n\n(أرسل /cancel للإلغاء)"
    else:
        prompt = (
            "أرسل رمز العملة والمبلغ:\nمثال — ILS 2000 (أو: شيكل 2000)\n\n(أرسل /cancel للإلغاء)"
        )
    await query.edit_message_text(prompt, reply_markup=_home_keyboard())


async def _handle_budget_delete(query, context, budget_id: str):
    from app.database.crud import delete_budget

    uid = query.from_user.id
    db = SessionLocal()
    try:
        ok = delete_budget(db, uid, int(budget_id))
    finally:
        db.close()
    text = f"🗑️ حُذفت الميزانية {budget_id}." if ok else "لم أجد هذه الميزانية."
    await query.edit_message_text(text, reply_markup=_home_keyboard())


async def _handle_budget(query, context, parts: list):
    act = parts[0] if parts else "list"
    if act == "add" and len(parts) > 1:
        await _handle_budget_add(query, context, parts[1])
    elif act == "del" and len(parts) > 1:
        await _handle_budget_delete(query, context, parts[1])
    else:
        await _handle_budget_list(query, context)


# ---------- المساحة المشتركة (Workspace) ----------


async def _workspace_status(query, context):
    from app.database.crud import list_workspace

    uid = query.from_user.id
    db = SessionLocal()
    try:
        info = list_workspace(db, uid)
        if info is None:
            text = (
                "لا تملك مساحة مشتركة بعد.\n\n"
                "مع المساحة المشتركة يرى جميع الأعضاء نفس الأرقام "
                "(حساب واحد للشركة أو العائلة).\n"
                "أنشئ مساحتك ثم ادعُ الشركاء بمعرّفاتهم الرقمية."
            )
            rows = [[("🆕 إنشاء المساحة", "ws:new")]]
        else:
            members = "\n".join(f"• {m}" for m in info["members"])
            role = " (المرتكز)" if info["owner"] else " (عضو)"
            text = (
                f"🏢 مساحة العمل الحالية: #{info['workspace_id']} — {role}\n"
                f"الأعضاء ({len(info['members'])}):\n{members}\n\n"
                "يعمل الجميع على سجل واحد مشترك."
            )
            rows = [
                [("👥 إدارة الأعضاء", "ws:members")],
                [("➕ إضافة عضو", "ws:add")],
            ]
            if not info["owner"]:
                rows.append([("🚪 مغادرة المساحة", "ws:leave")])
    finally:
        db.close()
    rows.append([("🏠 القائمة الرئيسية", "menu:main")])
    await query.edit_message_text(text, reply_markup=build_menu(rows))


async def _workspace_members(query, context):
    from app.database.crud import list_workspace

    uid = query.from_user.id
    db = SessionLocal()
    try:
        info = list_workspace(db, uid)
        if info is None:
            text = "لا توجد مساحة مشتركة بعد."
            rows = [[("🆕 إنشاء المساحة", "ws:new")]]
        else:
            text = f"الأعضاء ({len(info['members'])}):\n" + "\n".join(
                f"• {m}" for m in info["members"]
            )
            rows = []
            if info["owner"]:
                rows = [[("❌ إخراج " + str(m), f"ws:rm:{m}")] for m in info["members"] if m != uid]
            rows.append([("⬅️ رجوع للمساحة", "ws:status")])
    finally:
        db.close()
    rows.append([("🏠 القائمة الرئيسية", "menu:main")])
    await query.edit_message_text(text, reply_markup=build_menu(rows))


async def _workspace_create(query, context):
    from app.database.crud import create_workspace

    uid = query.from_user.id
    db = SessionLocal()
    try:
        create_workspace(db, uid)
        await query.answer("✅ أُنشئت المساحة المشتركة.")
    finally:
        db.close()
    await _workspace_status(query, context)


async def _workspace_add(query, context):
    context.user_data["pending_ws_invite"] = True
    await query.edit_message_text(
        "أرسل المعرّف الرقمي (Telegram ID) للشريك الذي تريد مشاركته:\n"
        "مثال — 555666777\n\n(أرسل /cancel للإلغاء)",
        reply_markup=_home_keyboard(),
    )


async def _workspace_leave(query, context):
    from app.database.crud import leave_workspace

    uid = query.from_user.id
    db = SessionLocal()
    try:
        ok = leave_workspace(db, uid)
        await query.answer("غادرت المساحة المشتركة." if ok else "لا يمكنك مغادرة مساحة تملكها.")
    finally:
        db.close()
    await _workspace_status(query, context)


async def _workspace_remove(query, context, target: str):
    from app.database.crud import remove_from_workspace

    uid = query.from_user.id
    db = SessionLocal()
    try:
        ok = remove_from_workspace(db, uid, int(target))
        await query.answer("أُخرج العضو." if ok else "تعذّر إخراج هذا العضو.")
    finally:
        db.close()
    await _workspace_members(query, context)


async def _handle_workspace(query, context, parts: list):
    act = parts[0] if parts else "status"
    if act == "new":
        await _workspace_create(query, context)
    elif act == "add":
        await _workspace_add(query, context)
    elif act == "leave":
        await _workspace_leave(query, context)
    elif act == "members":
        await _workspace_members(query, context)
    elif act == "rm" and len(parts) > 1:
        await _workspace_remove(query, context, parts[1])
    else:
        await _workspace_status(query, context)


async def _handle_tool_chart(query, context):
    from app.charts import generate_monthly_chart

    uid = query.from_user.id
    db = SessionLocal()
    try:
        buf = generate_monthly_chart(db, uid)
    except Exception:
        logger.exception("خطأ في توليد الرسم البياني")
        await query.edit_message_text("حدث خطأ أثناء توليد الرسم البياني، حاول لاحقًا.")
        return
    finally:
        db.close()

    if buf is None:
        await query.edit_message_text(
            "لا توجد بيانات للرسم بعد، أو تعذر جلب أسعار الصرف لتوحيد العملات.",
            reply_markup=_home_keyboard(),
        )
        return

    await query.message.reply_chat_action("upload_photo")
    await query.message.reply_photo(
        photo=buf,
        caption="📈 الإيرادات مقابل المصاريف — آخر 6 أشهر",
    )
    await query.edit_message_text("تم إرسال الرسم البياني ✅", reply_markup=_home_keyboard())


async def _handle_tool_convert(query, context):
    context.user_data["pending_convert"] = True
    await query.edit_message_text(
        "أرسل التحويل بصيغة:\n"
        "مثال 1: 300 ILS إلى USD\n"
        "مثال 2: 500 دولار إلى شيكل\n\n"
        "(أرسل /cancel للإلغاء)"
    )


async def _handle_tool(query, context, parts: list):
    act = parts[0] if parts else ""
    if act == "chart":
        await _handle_tool_chart(query, context)
    elif act == "convert":
        await _handle_tool_convert(query, context)
    else:
        await query.edit_message_text(PAGES["tools"][0], reply_markup=build_menu(TOOLS_MENU))


async def _handle_export(query, context, parts: list):
    if not parts or parts[0] not in EXPORT_CAPTIONS:
        await query.edit_message_text(
            "تصدير Excel — اختر الفترة:", reply_markup=build_menu(EXPORT_MENU)
        )
        return

    from bot.exporters import generate_export_excel

    period = parts[0]
    start, label = export_period_start(period)
    uid = query.from_user.id
    db = SessionLocal()
    try:
        buf = generate_export_excel(db, uid, start_utc=start)
    finally:
        db.close()

    from app.timeutil import now_local

    now_str = now_local().strftime("%Y-%m-%d")
    await query.message.reply_chat_action("upload_document")
    await query.message.reply_document(
        document=buf,
        filename=f"export_{label}_{now_str}.xlsx",
        caption=f"{EXPORT_CAPTIONS.get(period, 'تصدير')}\nيحتوي: معاملات + مهام + طلبيات وملاحظات",
    )
    await query.edit_message_text("تم إرسال ملف التصدير ✅", reply_markup=_home_keyboard())


# ---------- تعديل السجلات (el / rf) ----------


async def _start_record_edit(query, context, model_name: str, record_id: int) -> None:
    """يعرض حقول سجل محدد كأزرار تعديل (يُستخدم لآخر سجل ولملفات التاريخ)."""
    from bot.editing import EDITABLE_FIELDS, FIELD_LABELS_AR, _get_current_value

    _clear_all_pending(context)
    uid = query.from_user.id
    db = SessionLocal()
    try:
        record, _ = get_record_by_id(db, uid, model_name, record_id)
    finally:
        db.close()

    if record is None:
        await query.edit_message_text("لم أجد السجل (ربما حُذف).", reply_markup=_home_keyboard())
        return

    context.user_data["pending_record_edit_id"] = record.id
    context.user_data["pending_record_edit_model"] = model_name
    context.user_data["pending_record_edit_obj"] = record

    lines = ["✏️ تعديل السجل — اختر الحقل:\n"]
    if model_name == "Transaction":
        lines.append(f"• المبلغ: {_get_current_value(record, 'amount')}")
        lines.append(f"• العملة: {_get_current_value(record, 'currency')}")
        lines.append(f"• الشخص: {_get_current_value(record, 'person')}")
        lines.append(f"• التصنيف: {_get_current_value(record, 'category')}")
        lines.append(f"• الوصف: {_get_current_value(record, 'description')}")
    elif model_name == "Task":
        lines.append(f"• الوصف: {_get_current_value(record, 'description')}")
        lines.append(f"• الشخص: {_get_current_value(record, 'person')}")
        lines.append(f"• الموعد: {_get_current_value(record, 'due_date')}")
        lines.append(f"• الأولوية: {_get_current_value(record, 'priority')}")
        if getattr(record, "recurrence_rule", None):
            lines.append(f"• التكرار: {record.recurrence_rule}")
    else:
        lines.append(f"• الوصف: {_get_current_value(record, 'description')}")
        lines.append(f"• الشخص: {_get_current_value(record, 'person')}")
        lines.append(f"• التصنيف: {_get_current_value(record, 'category')}")

    rows = [
        [(FIELD_LABELS_AR.get(f, f), f"rf:{model_name}:{f}")]
        for f in EDITABLE_FIELDS.get(model_name, [])
    ]
    rows.append([("🚫 إنهاء التعديل", "rf:end")])
    await query.edit_message_text("\n".join(lines), reply_markup=build_menu(rows))


async def _handle_edit_last(query, context, parts: list | None = None):
    """زر "تعديل آخر سجل": يعرض حقول آخر سجل محفوظ كأزرار تعديل."""
    uid = query.from_user.id
    db = SessionLocal()
    try:
        recs = list_recent_records(db, uid, limit=1)
        model_name = recs[0]["model"] if recs else None
        record_id = recs[0]["id"] if recs else None
    finally:
        db.close()

    if record_id is None or model_name is None:
        await query.edit_message_text(
            "لا توجد سجلات محفوظة لتعديلها.", reply_markup=_home_keyboard()
        )
        return
    await _start_record_edit(query, context, model_name, record_id)


async def _handle_record_field(query, context, parts: list):
    """زر حقل تعديل سجل (rf:Model:Field أو rf:end)."""
    from bot.editing import FIELD_LABELS_AR, _get_current_value

    if not parts or parts[0] == "end":
        _clear_all_pending(context)
        await query.edit_message_text(MAIN_MENU_TEXT, reply_markup=_main_menu_keyboard())
        return
    field = parts[-1]
    record = context.user_data.get("pending_record_edit_obj")
    if record is None:
        await query.edit_message_text(
            "انتهت الجلسة — أعد فتح من القائمة.", reply_markup=_home_keyboard()
        )
        return
    label = FIELD_LABELS_AR.get(field, field)
    context.user_data["pending_record_edit_field"] = field
    current = _get_current_value(record, field)
    await query.edit_message_text(
        f"أرسل القيمة الجديدة للحقل {label}:\nالقيمة الحالية: {current}\n\n(أرسل /cancel للإلغاء)",
        reply_markup=_home_keyboard(),
    )


# ---------- آخر العمليات + البحث (his / rb / sb / sr) ----------

PAGE_SIZE = 5


def _records_page_payload(
    recs: list[dict], term: str | None, page: int, prefix: str
) -> tuple[str, InlineKeyboardMarkup]:
    """يرجّع (نص, لوحة أزرار) لصفحة سجلات — يعرض التعديل/الحذف وتنقّل الصفحات."""
    total = len(recs)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), pages)
    start = (page - 1) * PAGE_SIZE
    chunk = recs[start : start + PAGE_SIZE]

    heading = "🕘 آخر العمليات — " if not term else "🔍 نتائج البحث — "
    lines = [f"{heading}صفحة {page}/{pages}"]
    if not chunk:
        lines.append("لا توجد سجلات بعد." if not term else "لا توجد نتائج مطابقة.")
    for r in chunk:
        lines.append(f"• {r['date'] or '—'} | {r['kind']}: {r['label']}")
        if r["person"]:
            lines.append(f"   👤 {r['person']}")

    rows: list[list[tuple[str, str]]] = []
    for r in chunk:
        rows.append(
            [
                (f"✏️ تعديل #{r['id']}", f"rb:e:{r['model']}:{r['id']}"),
                (f"🗑️ حذف #{r['id']}", f"rb:d:{r['model']}:{r['id']}"),
            ]
        )
    nav: list[tuple[str, str]] = []
    if page > 1:
        nav.append((f"⬅️ ص{page - 1}", f"{prefix}:p:{page - 1}"))
    if page < pages:
        nav.append((f"ص{page + 1} ➡️", f"{prefix}:p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("🏠 القائمة الرئيسية", "menu:main")])
    return "\n".join(lines), build_menu(rows)


def _records_page_number(parts: list) -> int:
    """يستخرج رقم الصفحة من callback (p:2 أو 2)."""
    if not parts:
        return 1
    raw = parts[-1]
    return int(raw) if raw.isdigit() else 1


async def _handle_history(query, context, parts: list):
    page = _records_page_number(parts)
    uid = query.from_user.id
    db = SessionLocal()
    try:
        recs = list_recent_records(db, uid, limit=50)
    finally:
        db.close()
    text, markup = _records_page_payload(recs, None, page, "his")
    await query.edit_message_text(text, reply_markup=markup)


async def _handle_record_action(query, context, parts: list):
    """أزرار سريعة في صفحات السجلات: rb:e:Model:id (تعديل) / rb:d:Model:id (حذف)."""
    act = parts[0] if parts else ""
    if act == "e" and len(parts) == 3 and parts[2].isdigit():
        await _start_record_edit(query, context, parts[1], int(parts[2]))
        return
    if act == "d" and len(parts) == 3 and parts[2].isdigit():
        uid = query.from_user.id
        db = SessionLocal()
        try:
            deleted = delete_record_by_id(db, uid, parts[1], int(parts[2]))
        finally:
            db.close()
        if deleted:
            await query.answer("حُذف السجل ✅")
        else:
            await query.answer("تعذّر الحذف (صلاحيات أو سجل غير موجود).")
        await _handle_history(query, context, ["p", "1"])
        return
    await _handle_history(query, context, ["p", "1"])


async def _handle_search_start(query, context, parts: list):
    _clear_all_pending(context)
    context.user_data["pending_search"] = True
    await query.edit_message_text(
        "🔍 اكتب كلمة البحث (شخص، تصنيف، وصف، قيمة أو نوع):\nمثال: محمد أو فاتورة",
        reply_markup=_home_keyboard(),
    )


async def _handle_search_page(query, context, parts: list):
    term = context.user_data.get("pending_search_term")
    if not term:
        await _handle_search_start(query, context, parts)
        return
    page = _records_page_number(parts)
    uid = query.from_user.id
    db = SessionLocal()
    try:
        recs = search_records(db, uid, term, limit=50)
    finally:
        db.close()
    text, markup = _records_page_payload(recs, term, page, "sr")
    await query.edit_message_text(text, reply_markup=markup)


# ---------- اللغة (ln) ----------


def _lang_keyboard() -> InlineKeyboardMarkup:
    return build_menu(
        [
            [("🌐 العربية", "ln:ar"), ("🌐 English", "ln:en")],
            [("🏠 القائمة الرئيسية", "menu:main")],
        ]
    )


def send_lang_menu(message, text: str = ""):
    """يرسل شاشة اختيار اللغة كرسالة جديدة (لأمر /lang)."""
    return message.reply_text(text or "اختر لغة الواجهة:", reply_markup=_lang_keyboard())


async def _handle_lang(query, context, parts: list):
    lang = "en" if (parts and parts[0] == "en") else "ar"
    uid = query.from_user.id
    from app.database.crud import set_user_lang

    db = SessionLocal()
    try:
        set_user_lang(db, uid, lang)
    finally:
        db.close()
    remember_lang(uid, lang)
    await query.edit_message_text(
        t("lang_done_en" if lang == "en" else "lang_done_ar", lang),
        reply_markup=_lang_keyboard(),
    )


# ---------- Router موحّد ----------

HANDLERS = {
    "menu": _handle_menu,
    "rec": _handle_record,
    "tsk": _handle_task,
    "rpt": _handle_report,
    "set": _handle_settings,
    "tool": _handle_tool,
    "bg": _handle_budget,
    "ex": _handle_export,
    "ws": _handle_workspace,
    "el": _handle_edit_last,
    "rf": _handle_record_field,
    "his": _handle_history,
    "rb": _handle_record_action,
    "sb": _handle_search_start,
    "sr": _handle_search_page,
    "ln": _handle_lang,
}


async def menu_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يقفز حسب أول جزء من callback_data (action:target:extra).

    ما لا يخص القوائم (confirm:* و edit:* و editfield:*) يمرّ دون اعتراض
    إلى ConversationHandlers المسجّلة قبله.
    """
    query = update.callback_query
    data = query.data or ""
    action, *parts = data.split(":")
    handler = HANDLERS.get(action)
    if handler is None:
        return
    await query.answer()
    await handler(query, context, [p for p in parts if p != ""])
