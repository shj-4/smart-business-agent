"""
المعالجات البسيطة (commands + معالج الأخطاء) وتسجيل المعالجات في التطبيق.

المعالج الرئيسي للنصوص والوسائط (محادثة التسجيل/الاستعلام) يعيش في
bot.conversation عبر ConversationHandler — هنا فقط الأوامر المستقلة.
"""

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from app.database.crud import complete_task, find_pending_task, undo_last_record
from app.database.db import SessionLocal
from app.exchange import CURRENCY_NAMES, convert

logger = logging.getLogger(__name__)


PERIOD_KEYS = {
    "today": "today",
    "day": "today",
    "week": "week",
    "weekly": "week",
    "month": "month",
    "monthly": "month",
    "all": "all",
}
PERIOD_HINT = "الإستخدام: today | week/weekly | month/monthly | all"


def resolve_period_arg(raw: str) -> str | None:
    """يمرر وسيط الفترة إلى مفتاح موحّد (today/week/month/all) بين /report و/export.

    يعيد None إن لم يُفهم الوسيط حتى لا يسقط صامتًا إلى "all" (كان يربك المستخدم).
    """
    return PERIOD_KEYS.get((raw or "all").lower())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.menus import send_main_menu

    first = update.effective_user.first_name or ""
    await send_main_menu(
        update.message,
        context,
        text=(
            f"أهلًا بك {first} 👋\nأنا مساعدك الذكي لإدارة أعمالك.\n"
            "اختر من القائمة، أو أرسل مباشرة أي عملية أو سؤال\n"
            "مثل:\n• 300 شيكل لمحمد مقابل مواد\n• كم صرفت هذا الشهر؟\n"
            "🕘 اطلب /help لعرض دليل الأوامر الكامل."
        ),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /help — دليل شامل للأوامر والأزرار بدون أي تحليل AI."""
    from bot.menus import send_main_menu

    guide = (
        "📖 دليل استخدام البوت — مساعد أعمالك الذكي\n\n"
        "🗣️ اكتب مباشرة أي عملية أو سؤال، مثل:\n"
        "• 300 شيكل لمحمد مقابل مواد\n"
        "• صرفت 500 دولار فاتورة كهرباء\n"
        "• كم صرفت هذا الشهر؟ / كم لي على سامر؟\n\n"
        "🧭 الأزرار الرئيسية:\n"
        "• 💰 تسجيل عملية — مصاريف/إيراد/طلبية/ملاحظة/مهمة\n"
        "• 📊 التقارير — ملخصات، روسم، مقارنة فترات\n"
        "• 📋 مهامي — قوائم المهام وإنجازها وحذفها\n"
        "• 🧰 أدوات — آخر سجل، ميزانيات، رسم بياني، تصدير، تحويل، بحث، مساحة مشتركة\n\n"
        "⌨️ الأوامر السريعة:\n"
        "/menu — القائمة الرئيسية\n"
        "/done وصف — إنجاز مهمة\n"
        "/undo — تراجع عن آخر سجل\n"
        "/convert مبلغ من إلى — تحويل عملة (مثل: /convert 300 ILS USD)\n"
        "/budget سقف — إدارة ميزانية شهرية\n"
        "/report — ملخص الفترة\n"
        "/export — تصدير Excel\n"
        "/chart — رسم بياني بالمصاريف\n"
        "/report_on — تقرير دوري تلقائي\n"
        "/report_off — إيقاف التقرير الدوري\n"
        "/work — إدارة المساحة المشتركة\n"
        "/lang — تبديل لغة الواجهة عربي/English\n"
        "/cancel — إلغاء أي عملية معلّقة\n\n"
        "📸 صوّر أي فاتورة وأرسلها، وسأستخرج تفاصيلها تلقائيًا.\n"
        "🎤 أرسل صوتًا وسأفهمه كعملية.\n\n"
        "تحتاج تفاصيل أكثر؟ أرسل أي سؤال مكتوبًا بصياغتك الطبيعية."
    )
    await send_main_menu(update.message, context, text=guide)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /menu — يعرض القائمة الرئيسية (بأزرار InlineKeyboard)."""
    from bot.menus import send_main_menu

    await send_main_menu(update.message, context)


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    # context.args يلتقط الوسائط النصية للأمر بشكل صحيح، ويعمل مع /done@BotUsername
    text = (
        " ".join(context.args).strip()
        if context.args
        else (update.message.text or "").replace("/done", "").strip()
    )

    if not text:
        await update.message.reply_text("استخدم: /done وصف المهمة\nمثال: /done الاتصال بسامر")
        return

    db = SessionLocal()
    try:
        task = find_pending_task(db, telegram_user_id, text)
        if task:
            from bot.menus import MAIN_HOME_KEYBOARD

            complete_task(db, telegram_user_id, task.id)
            await update.message.reply_text(
                f"تم إنجاز المهمة: {task.description}", reply_markup=MAIN_HOME_KEYBOARD
            )
        else:
            await update.message.reply_text("لم أجد مهمة مطابقة ضمن مهامك المعلّقة.")
    finally:
        db.close()


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /lang — تبديل لغة الواجهة بين العربية والإنجليزية."""
    from bot.menus import send_lang_menu

    await send_lang_menu(update.message)


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /undo — يحذف (soft-delete) آخر سجل أضافه المستخدم."""
    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        undone = undo_last_record(db, telegram_user_id)
    finally:
        db.close()
    if undone:
        from bot.menus import MAIN_HOME_KEYBOARD

        await update.message.reply_text(
            f"تم التراجع عن آخر سجل:\n{undone['kind']}: {undone['label'] or '(بدون وصف)'}",
            reply_markup=MAIN_HOME_KEYBOARD,
        )
    else:
        await update.message.reply_text("لا يوجد سجلات سابقة يمكن التراجع عنها.")


async def convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /convert <المبلغ> <المن> <المن أو إلى> <إلى> — تحويل عملة."""
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "استخدم: /convert المبلغ من_إلى\n"
            "مثال: /convert 300 ILS USD\n"
            "العملات المدعومة: " + ", ".join(CURRENCY_NAMES)
        )
        return

    amount_str = args[0]
    from_cur = args[1].upper()
    to_cur = args[2].upper()

    try:
        amount = Decimal(amount_str)
    except InvalidOperation:
        await update.message.reply_text(f"المبلغ غير صالح: {amount_str}")
        return

    result = convert(amount, from_cur, to_cur)
    if "error" in result:
        await update.message.reply_text(result["error"])
        return

    rate = result["rate"]
    converted = result["result"]
    from_name = CURRENCY_NAMES.get(result["from"], result["from"])
    to_name = CURRENCY_NAMES.get(result["to"], result["to"])
    await update.message.reply_text(
        f"{result['amount']} {from_name} = {converted} {to_name}\n"
        f"(سعر الصرف: 1 {result['from']} = {rate} {result['to']})"
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /report — يولّد تقرير Excel للمعاملات المالية ويُرسله كملف."""
    from datetime import timedelta

    from app.timeutil import now_local, to_utc_naive
    from bot.exporters import generate_tasks_excel, generate_transactions_excel

    arg = (context.args[0] if context.args else "all").lower()
    period = resolve_period_arg(arg)
    if period is None:
        await update.message.reply_text(f"لم أفهم الفترة \"{arg}\".\n{PERIOD_HINT}")
        return

    local_now = now_local()
    start = None
    file_label = None

    if period == "today":
        local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)
        file_label = "اليوم"
    elif period == "week":
        from app.timeutil import first_day_of_week

        fd = first_day_of_week()
        weekday = local_now.weekday()
        local = local_now - timedelta(days=(weekday - fd) % 7)
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)
        file_label = "الأسبوع"
    elif period == "month":
        local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)
        file_label = "الشهر"
    else:
        file_label = "الكل"

    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        buf = generate_transactions_excel(db, telegram_user_id, start_utc=start)
        tasks_buf = generate_tasks_excel(db, telegram_user_id)
    finally:
        db.close()

    await update.message.reply_chat_action("upload_document")

    now_str = local_now.strftime("%Y-%m-%d")
    filename = f"transactions_{file_label}_{now_str}.xlsx"
    await update.message.reply_document(
        document=buf,
        filename=filename,
        caption=f"تقرير المعاملات المالية ({file_label})",
    )

    tasks_filename = f"tasks_{now_str}.xlsx"
    await update.message.reply_document(
        document=tasks_buf,
        filename=tasks_filename,
        caption="تقرير المهام",
    )


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /export [today|week|month] — ملف Excel واحد بكل السجلات للفترة المحددة."""
    from datetime import timedelta

    from app.timeutil import first_day_of_week, now_local, to_utc_naive
    from bot.exporters import generate_export_excel

    arg = (context.args[0] if context.args else "all").lower()
    period = resolve_period_arg(arg)
    if period is None:
        await update.message.reply_text(f"لم أفهم الفترة \"{arg}\".\n{PERIOD_HINT}")
        return

    local_now = now_local()
    start = None
    file_label = "all"

    if period == "today":
        local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)
        file_label = "today"
    elif period == "week":
        fd = first_day_of_week()
        weekday = local_now.weekday()
        local = local_now - timedelta(days=(weekday - fd) % 7)
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)
        file_label = "week"
    elif period == "month":
        local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)
        file_label = "month"

    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        buf = generate_export_excel(db, telegram_user_id, start_utc=start)
    finally:
        db.close()

    await update.message.reply_chat_action("upload_document")
    now_str = local_now.strftime("%Y-%m-%d")
    filename = f"export_{file_label}_{now_str}.xlsx"
    caption = {"today": "تصدير (اليوم)", "week": "تصدير (الأسبوع)", "month": "تصدير (الشهر)"}.get(
        file_label, "تصدير (كل السجلات)"
    )
    await update.message.reply_document(
        document=buf,
        filename=filename,
        caption=f"{caption}\nيحتوي: معاملات + مهام + طلبيات وملاحظات",
    )


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /chart — رسم بياني (matplotlib) بمقارنة الإيرادات والمصاريف شهريًا."""
    from app.charts import generate_monthly_chart
    from app.database.db import SessionLocal as _S

    telegram_user_id = update.effective_user.id
    db = _S()
    try:
        buf = generate_monthly_chart(db, telegram_user_id)
    except Exception:
        logger.exception("خطأ في توليد الرسم البياني")
        await update.message.reply_text("حدث خطأ أثناء توليد الرسم البياني، حاول لاحقًا.")
        return
    finally:
        db.close()

    if buf is None:
        await update.message.reply_text(
            "لا توجد بيانات للرسم بعد، أو تعذر جلب أسعار الصرف لتوحيد العملات.\n"
            "أضف بعض العمليات ثم أعد المحاولة، أو تحقق من اتصال الإنترنت."
        )
        return

    await update.message.reply_chat_action("upload_photo")
    await update.message.reply_photo(
        photo=buf,
        caption="📈 الإيرادات مقابل المصاريف — آخر 6 أشهر",
    )


async def report_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /report_on [daily|weekly|monthly] — تفعيل التقرير الدوري التلقائي."""
    from app.database.crud import set_report_frequency

    arg = (context.args[0] if context.args else "daily").lower()
    freq_map = {
        "daily": "daily",
        "يومي": "daily",
        "weekly": "weekly",
        "أسبوعي": "weekly",
        "اسبوعي": "weekly",
        "monthly": "monthly",
        "شهري": "monthly",
    }
    freq = freq_map.get(arg)
    if freq is None:
        await update.message.reply_text(
            "استخدم: /report_on daily | weekly | monthly\n"
            "مثال: /report_on monthly — تقرير في أول كل شهر"
        )
        return

    from app.config import settings

    deliver = settings.report_time
    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        set_report_frequency(db, telegram_user_id, freq)
    finally:
        db.close()

    freq_name = {"daily": "اليومي", "weekly": "الأسبوعي", "monthly": "الشهري"}[freq]
    freq_interval = {"daily": "كل يوم", "weekly": "في أول كل أسبوع", "monthly": "في أول كل شهر"}[freq]
    await update.message.reply_text(
        f"تم تفعيل التقرير {freq_name}.\n"
        f"سأرسله لك {freq_interval} من الآن عند الساعة {deliver} (بتوقيتك المحلي).\n"
        "لإيقافه أرسل /report_off"
    )


async def report_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /report_off — إيقاف التقرير الدوري."""
    from app.database.crud import set_report_frequency

    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        set_report_frequency(db, telegram_user_id, "off")
    finally:
        db.close()
    await update.message.reply_text("تم إيقاف التقارير الدورية.")


async def budget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /budget — ميزانيات شهرية للمصاريف حسب العملة أو الشخص.

    /budget إضافة عملة ILS 2000
    /budget إضافة شخص محمد 1500
    /budget قائمة
    /budget حذف <رقم>
    """
    from app.database.crud import (
        budget_monthly_reset,
        budget_usage,
        create_budget,
        delete_budget,
        list_budgets,
    )

    telegram_user_id = update.effective_user.id
    args = context.args or []

    if not args:
        args = ["قائمة"]

    action = args[0].strip()
    action_ar = {
        "add": "إضافة",
        "اضافة": "إضافة",
        "إضافة": "إضافة",
        "list": "قائمة",
        "قائمة": "قائمة",
        "عرض": "قائمة",
        "del": "حذف",
        "delete": "حذف",
        "حذف": "حذف",
    }.get(action.lower())

    db = SessionLocal()
    try:
        if action_ar == "إضافة":
            if len(args) < 4:
                await update.message.reply_text(
                    "استخدم: /budget إضافة <عملة | شخص> <الهدف> <المبلغ الشهري>\n"
                    "مثال: /budget إضافة عملة ILS 2000\n"
                    "مثال: /budget إضافة شخص محمد 1500"
                )
                return
            scope_txt = args[1].strip()
            target = args[2].strip()
            limit_str = args[3]

            if scope_txt in ("عملة", "عملة"):
                scope = "currency"
            elif scope_txt in ("شخص", "شخص"):
                scope = "person"
            else:
                await update.message.reply_text("النطاق غير معروف. استخدم: عملة أو شخص")
                return

            budget = create_budget(db, telegram_user_id, scope, target, limit_str)
            if not budget:
                await update.message.reply_text(
                    "لم يتم إنشاء الميزانية. ربما ميزانية بنفس النطاق والهدف موجودة مسبقًا "
                    "أو المبلغ غير صالح."
                )
                return

            scope_txt = budget.currency if budget.scope == "currency" else f"الشخص {budget.person}"
            await update.message.reply_text(
                f"تم إنشاء ميزانية شهرية: {scope_txt} — {budget.monthly_limit}\n"
                f"سأرسل تنبيهًا عند اقترابك من السقف وتجاوزه."
            )
            return

        if action_ar == "حذف":
            if len(args) < 2:
                await update.message.reply_text("استخدم: /budget حذف <رقم الميزانية>")
                return
            try:
                budget_id = int(args[1])
            except ValueError:
                await update.message.reply_text("رقم الميزانية غير صالح.")
                return
            if delete_budget(db, telegram_user_id, budget_id):
                await update.message.reply_text(f"تم حذف الميزانية رقم {budget_id}.")
            else:
                await update.message.reply_text("لم أجد هذه الميزانية.")
            return

        # قائمة
        budgets = list_budgets(db, telegram_user_id)
        if not budgets:
            await update.message.reply_text(
                "لا توجد ميزانيات بعد.\n"
                "استخدم: /budget إضافة عملة ILS 2000  أو  /budget إضافة شخص محمد 1500"
            )
            return

        lines = ["ميزانياتك الشهرية:\n"]
        for b in budgets:
            budget_monthly_reset(db, b)
            usage = budget_usage(db, b)
            if b.scope == "currency":
                target_txt = CURRENCY_NAMES.get(b.currency, b.currency)
            else:
                target_txt = f"الشخص {b.person}"
            if usage["over"]:
                status = "⚠️ تجاوزت"
            elif usage["percent"] >= 80:
                status = "⚠️ قريب من السقف"
            else:
                status = "ضمن الحدود"
            lines.append(
                f"{b.id}. {target_txt}: {usage['spent']} / {usage['limit']} "
                f"({usage['percent']}%) — {status}"
            )
        lines.append("\nلحذف: /budget حذف <رقم>")
        await update.message.reply_text("\n".join(lines))
    finally:
        db.close()


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر مخفي /admin_stats — إحصائيات عامة للمسؤول (لا يكشف بيانات فردية)."""
    from app.admin import build_admin_stats
    from app.config import settings

    user_id = update.effective_user.id
    if user_id not in settings.admin_user_ids:
        logger.warning("محاولة وصول غير مصرّح لأمر الأدمن من user=%s", user_id)
        await update.message.reply_text("عذرًا، هذا الأمر متاح للمسؤول فقط.")
        return

    db = SessionLocal()
    try:
        stats = build_admin_stats(db)
    finally:
        db.close()

    lines = [
        "🔐 إحصائيات عامة (بدون بيانات فردية):",
        f"👥 المستخدمون الكلي: {stats['users_count']}",
        f"📊 المعاملات: {stats['transactions_count']}",
        f"✅ مهام معلّقة: {stats['pending_tasks']}",
        f"⚠️ مهام متأخرة: {stats['overdue_tasks']}",
        f"🗒️ طلبيات/ملاحظات: {stats['notes_count']}",
        f"🎯 عدد الميزانيات: {stats['budgets_count']}",
        f"📅 تقارير دورية مفعّلة: {stats['active_reports']}",
    ]

    def _sum_line(label, totals):
        if not totals:
            return f"{label}: 0"
        parts = ", ".join(f"{v} {c}" for c, v in totals.items())
        return f"{label}: {parts}"

    lines.append(_sum_line("💸 إجمالي المصاريف", stats["total_expenses"]))
    lines.append(_sum_line("💰 إجمالي الإيرادات", stats["total_incomes"]))

    if stats.get("pending_feedback"):
        lines.append(
            f"🧾 تحليلات خاطئة بانتظار المراجعة: {stats['pending_feedback']} (انظر /feedback)"
        )

    await update.message.reply_text("\n".join(lines))


def _is_admin(user_id: int) -> bool:
    from app.config import settings

    return user_id in settings.admin_user_ids


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر مخفي /feedback — يعرض رسائل التحليل الخاطئ بانتظار المراجعة اليدوية."""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        logger.warning("محاولة وصول غير مصرّح لأمر /feedback من user=%s", user_id)
        await update.message.reply_text("عذرًا، هذا الأمر متاح للمسؤول فقط.")
        return

    from app.database.crud import list_correction_feedback

    db = SessionLocal()
    try:
        items = list_correction_feedback(db, only_unreviewed=True, limit=20)
    finally:
        db.close()

    if not items:
        await update.message.reply_text("✅ لا توجد تحليلات خاطئة بانتظار المراجعة.")
        return

    lines = [f"🧾 تحليلات خاطئة بانتظار المراجعة ({len(items)}):", ""]
    for fb in items:
        raw = (fb.raw_message or "")[:200].replace("\n", " ")
        lines.append(f"[#{fb.id}] {fb.source} — نوع: {fb.data_type or 'غير معروف'}")
        lines.append(f"   🗒️ {raw or '(بدون نص)'}")
        lines.append(f"   ⏰ {fb.created_at:%Y-%m-%d %H:%M}")
        lines.append("")
    lines.append("لتعليم سجل كمراجَع: /feedback_ack <رقم السجل>")
    await update.message.reply_text("\n".join(lines))


async def feedback_ack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر مخفي /feedback_ack <id> — يعلّم سجل تحليل خاطئ كمراجَع يدويًا."""
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        logger.warning("محاولة وصول غير مصرّح لأمر /feedback_ack من user=%s", user_id)
        await update.message.reply_text("عذرًا، هذا الأمر متاح للمسؤول فقط.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("الاستخدام: /feedback_ack <رقم السجل>")
        return

    from app.database.crud import mark_correction_reviewed

    db = SessionLocal()
    try:
        ok = mark_correction_reviewed(db, int(args[0]))
    finally:
        db.close()

    await update.message.reply_text("✅ عُلِّم السجل كمراجَع." if ok else "لم أجد سجلًا بهذا الرقم.")


async def work_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /work — إدارة الحساب المشترك (مساحة عمل لعدة معرّفات Telegram).

    الاستخدام:
      /work            ← الحالة الحالية (مساحة؟ أعضاء؟)
      /work جديد       ← إنشاء مساحتك الخاصة (أنت مديرها)
      /work اضافة <id> ← يُدخل المالك معرّفًا إلى المساحة (يشارك البيانات)
      /work حذف <id>   ← يُخرج المالك عضوًا من المساحة
      /work مغادرة     ← العضو يغادر (المالك لا يغادر)
    """
    from app.database.crud import (
        create_workspace,
        invite_to_workspace,
        leave_workspace,
        list_workspace,
        remove_from_workspace,
    )

    telegram_user_id = update.effective_user.id
    args = context.args or []
    action = args[0].strip() if args else ""

    db = SessionLocal()
    try:
        if action == "جديد":
            create_workspace(db, telegram_user_id)
            await update.message.reply_text(
                "✅ أُنشئت مساحة عملك. بياناتك الآن تُقرأ ضمن أعضاء مساحتك.\n"
                "لإضافة مشارك: /work اضافة <المعرّف الرقمي لتيليغرام>"
            )
            return

        if action == "اضافة" and len(args) >= 2 and args[1].isdigit():
            target = int(args[1])
            ok = invite_to_workspace(db, telegram_user_id, target)
            if ok:
                await update.message.reply_text(f"✅ أُضيف المعرّف {target} إلى مساحة عملك.")
            else:
                await update.message.reply_text(
                    "لا يوجد إجراء: تأكد أنك مالك المساحة (أنشئها بـ /work جديد)، "
                    "وأن المعرّف مختلف عن معرّفك."
                )
            return

        if action == "حذف" and len(args) >= 2 and args[1].isdigit():
            target = int(args[1])
            ok = remove_from_workspace(db, telegram_user_id, target)
            await update.message.reply_text(
                f"✅ أُخرج المعرّف {target} من مساحة عملك." if ok else "لم أجد العضو أو لست المالك."
            )
            return

        if action == "مغادرة":
            ok = leave_workspace(db, telegram_user_id)
            await update.message.reply_text(
                "✅ غادرت المساحة — بياناتك عادت فردية لك."
                if ok
                else "لا شيء للمغادرة (أو أنت المالك)."
            )
            return

        # الحالة الحالية
        info = list_workspace(db, telegram_user_id)
        if info is None:
            await update.message.reply_text(
                "أنت حاليًا بمساحة فردية (بياناتك خاصة بك).\n"
                "لإنشاء مساحة مشتركة: /work جديد\n"
                "ثم أضف شركاءك: /work اضافة <المعرّف الرقمي>"
            )
            return

        members = ", ".join(str(m) for m in info["members"])
        role = "مالك" if info["owner"] else "عضو"
        await update.message.reply_text(
            f"🏢 مساحة العمل: {info['workspace_id']} (أنت: {role})\n"
            f"الأعضاء ({len(info['members'])}): {members}\n\n"
            f"مشاركون يرون نفس البيانات (معاملات/مهام/ميزانيات).\n"
            f"• إضافة: /work اضافة <id>   • إزالة: /work حذف <id>   • مغادرة: /work مغادرة"
        )
    finally:
        db.close()


async def system_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """معالج أخطاء عام — أي استثناء غير متوقع يُبلّغ المستخدم بدلًا من الصمت."""
    logger.error("خطأ عام غير متوقع: %s", context.error, exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "عذرًا، حدث خطأ غير متوقع. أعد المحاولة أو أرسل /cancel للبدء من جديد."
            )
            if hasattr(context, "user_data"):
                context.user_data.clear()
    except Exception:
        logger.exception("فشل إرسال رسالة خطأ إلى المستخدم")


def register_handlers(app) -> None:
    """يُسجّل كل المعالجات في Application (بما فيها ConversationHandler)."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("undo", undo_command))
    app.add_handler(CommandHandler("lang", lang_command))
    app.add_handler(CommandHandler("convert", convert_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("budget", budget_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("chart", chart_command))
    app.add_handler(CommandHandler("report_on", report_on_command))
    app.add_handler(CommandHandler("report_off", report_off_command))
    app.add_handler(CommandHandler("admin_stats", admin_stats_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("feedback_ack", feedback_ack_command))
    app.add_handler(CommandHandler("work", work_command))

    from bot.conversation import conversation_handler
    from bot.editing import edit_conversation
    from bot.menus import menu_callback_router

    # ترتيب مهم: ConversationHandler للمحادثة العامة أولًا ثم التعديل
    # ثم router القوائم (يسلم confirm:/edit:/editfield: للمحادثات ولا يلتقطها)
    app.add_handler(conversation_handler)
    app.add_handler(edit_conversation)
    app.add_handler(CallbackQueryHandler(menu_callback_router))
    app.add_error_handler(system_error_handler)

    # التذكيرات التلقائية: المهام المتأخرة، الميزانيات، التقارير الدورية، النسخ الاحتياطي
    from bot.reminders import (
        setup_budget_check,
        setup_daily_backup,
        setup_overdue_reminder,
        setup_periodic_reports,
    )

    setup_overdue_reminder(app)
    setup_budget_check(app)
    setup_periodic_reports(app)
    setup_daily_backup(app)
