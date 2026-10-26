"""
المعالجات البسيطة (commands + معالج الأخطاء) وتسجيل المعالجات في التطبيق.

المعالج الرئيسي للنصوص والوسائط (محادثة التسجيل/الاستعلام) يعيش في
bot.conversation عبر ConversationHandler — هنا فقط الأوامر المستقلة.
"""

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from io import BytesIO

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.database.crud import complete_task, undo_last_record
from app.database.db import SessionLocal
from app.exchange import CURRENCY_NAMES, convert
from app.formatting import fmt_amount
from bot.formatters import safe_reply
from bot.icons import EXPENSE, INCOME, NEW, SUCCESS, TASK

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
PERIOD_HINT = "الاستخدام: today | week/weekly | month/monthly | all"


def resolve_period_arg(raw: str) -> str | None:
    """يمرر وسيط الفترة إلى مفتاح موحّد (today/week/month/all) بين /report و/export.

    يعيد None إن لم يُفهم الوسيط حتى لا يسقط صامتًا إلى "all" (كان يربك المستخدم).
    """
    return PERIOD_KEYS.get((raw or "all").lower())


def _clean_join_token(token: str | None) -> str:
    """يزيل الرموز الاتجاهية و backticks من رمز الدعوة قبل المعالجة."""
    if not token:
        return ""
    try:
        from app.rtl import strip_bidi_marks
    except Exception:
        import re as _re
        _bidi_re = _re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
        def strip_bidi_marks(t: str) -> str:  # type: ignore
            return _bidi_re.sub("", t)
    cleaned = strip_bidi_marks(str(token))
    cleaned = cleaned.strip().strip("`").strip()
    cleaned = "".join(cleaned.split())
    return cleaned


async def _handle_invite_start(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> bool:
    """يعالج deep-link t.me/bot?start=inv_TOKEN. يعيد True إن عالجها."""
    from app.database.db import SessionLocal

    telegram_user_id = update.effective_user.id
    display_name = update.effective_user.first_name or None
    token = _clean_join_token(token)
    if not token:
        return False
    from app.database.crud.company import redeem_invite

    db = SessionLocal()
    try:
        ok, result = redeem_invite(db, telegram_user_id, token, display_name=display_name)
    finally:
        db.close()
    if ok:
        company = result
        await update.message.reply_text(f"✅ انضممت إلى شركة '{company.name}' بنجاح!")
        try:
            bot = _get_bot(update, context)
            if bot:
                await bot.send_message(
                    chat_id=company.owner_telegram_user_id,
                    text=f"👤 انضم {display_name or telegram_user_id} إلى شركتك '{company.name}'.",
                )
            else:
                logger.warning("لا يوجد bot لإرسال إشعار انضمام للمالك %s", company.owner_telegram_user_id)
        except Exception:
            logger.exception("فشل إرسال إشعار انضمام للمالك")
        from bot.menus import send_main_menu

        try:
            await send_main_menu(update.message, context, text=f"أهلاً بك في {company.name} 👋")
        except Exception:
            pass
        return True
    else:
        await update.message.reply_text(f"❌ {result}")
        return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0].startswith("inv_"):
        await _handle_invite_start(update, context, args[0][4:])
        return

    # فحص العضوية
    from app.database.db import SessionLocal

    telegram_user_id = update.effective_user.id
    # إن كان المستخدم اختار "استخدام فردي" سابقًا (solo_mode في user_data)
    # لا تعرض onboarding مجددًا — solo الآن ينشئ شركة فردية فـ has_company سيكون True
    if context.user_data.get("solo_mode"):
        has_company = True
    else:
        db = SessionLocal()
        try:
            from app.database.crud.company import company_id_for_user

            has_company = company_id_for_user(db, telegram_user_id) is not None
        finally:
            db.close()

    if not has_company:
        from bot.onboarding import onboarding_keyboard

        first = update.effective_user.first_name or ""
        await update.message.reply_text(
            f"أهلًا بك {first} 👋\nأنا مساعدك الذكي لإدارة أعمالك.\n\n"
            "يبدو أنك غير منضم لأي شركة بعد. ماذا تريد أن تفعل؟",
            reply_markup=onboarding_keyboard(),
        )
        return

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


async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /register <token> — تسجيل مستخدم جديد عبر رمز التسجيل."""
    from app.config import settings
    from bot.ratelimit import is_user_allowed, register_via_token

    telegram_user_id = update.effective_user.id
    if is_user_allowed(telegram_user_id):
        await update.message.reply_text("✅ أنت مسجل بالفعل ويمكنك استخدام البوت.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "استخدم: /register <رمز التسجيل>\n"
            "تواصل مع الإدارة للحصول على الرمز."
        )
        return
    token = (args[0] or "").strip()
    if register_via_token(telegram_user_id, token):
        await update.message.reply_text("✅ تم التسجيل بنجاح! يمكنك الآن استخدام البوت.")
        try:
            from bot.menus import send_main_menu
            await send_main_menu(update.message, context)
        except Exception:
            pass
    else:
        # لا نكشف إن كان الرمز خطأ أم غير مضبوط، لتجنب التخمين
        if not (settings.registration_token or "").strip():
            await update.message.reply_text("التسجيل معطّل حاليًا. تواصل مع الإدارة.")
        else:
            await update.message.reply_text("❌ رمز التسجيل غير صحيح. تأكد من الرمز وحاول مجددًا.")


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /privacy — الخصوصية: إفصاح وإلغاء/تفعيل إرسال البيانات لـ Gemini وحفظ الملغاة."""
    from app.database.crud.common import (
        get_or_create_user_pref,
        set_ai_consent,
        set_feedback_consent,
    )
    from app.database.crud.feedback import delete_user_feedback

    uid = update.effective_user.id
    args = (context.args or [])
    db = SessionLocal()
    try:
        pref = get_or_create_user_pref(db, uid)
        if not args:
            lines = [
                "🔐 الخصوصية — BizMate",
                "",
                "• نصوصك وصور الفواتير تُرسل إلى Google Gemini لتحليلها وتحويلها لعملية/مهمة.",
                "• التشفير at-rest لا يغطي هذا الإرسال (يُفك قبل الإرسال).",
                "• الرسائل الملغاة (/cancel) قد تُحفظ مشفّرة للتحسين فقط إن وافقت.",
                "",
                f"🤖 إرسال لـ Gemini: {'✅ مفعل' if pref.ai_consent else '❌ معطل'}",
                f"🧾 حفظ الملغاة: {'✅ مفعل' if pref.feedback_consent else '❌ معطل'}",
                "",
                "الأوامر:",
                "/privacy ai off — تعطيل الإرسال لـ Gemini (تحليل محلي فقط)",
                "/privacy ai on — تفعيل الإرسال",
                "/privacy feedback off — عدم حفظ الملغاة",
                "/privacy feedback on — السماح بحفظها",
                "/privacy forget — حذف كل ملغاة تخصك نهائيًا",
                "/privacy purge — (أدمن) حذف القديم >30يوم",
            ]
            await update.message.reply_text("\n".join(lines))
            return
        if args[0] == "ai" and len(args) >= 2 and args[1] in ("on","off","ON","OFF"):
            enabled = args[1].lower() == "on"
            set_ai_consent(db, uid, enabled)
            await update.message.reply_text(f"🤖 إرسال Gemini الآن: {'✅ مفعل' if enabled else '❌ معطل'}")
            return
        if args[0] == "feedback" and len(args) >= 2 and args[1] in ("on","off"):
            enabled = args[1].lower() == "on"
            set_feedback_consent(db, uid, enabled)
            await update.message.reply_text(f"🧾 حفظ الملغاة الآن: {'✅ مفعل' if enabled else '❌ معطل'}")
            return
        if args[0] == "forget":
            count = delete_user_feedback(db, uid)
            await update.message.reply_text(f"🗑️ حُذفت {count} رسالة ملغاة تخصك نهائيًا.")
            return
        if args[0] == "purge":
            if not await _require_admin(update, "privacy_purge", context):
                return
            from app.database.crud.feedback import purge_old_feedback
            count = purge_old_feedback(db, days=30)
            await update.message.reply_text(f"🧹 حُذف {count} سجل قديم/مراجع (>30يوم).")
            return
        await update.message.reply_text("استخدم: /privacy أو /privacy ai on|off | feedback on|off | forget")
    finally:
        db.close()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /help — دليل شامل للأوامر والأزرار بدون أي تحليل AI."""
    from bot.menus import send_main_menu

    guide = (
        "📖 دليل استخدام البوت — مساعد أعمالك الذكي\n\n"
        "🗣️ اكتب مباشرة أي عملية أو سؤال، مثل:\n"
        "• 300 شيكل لمحمد مقابل مواد\n"
        "• صرفت 500 دولار فاتورة كهرباء\n"
        "• كم صرفت هذا الشهر؟ / كم لي على سامر؟\n\n"
        "🧭 الأزرار الرئيسية:\n"
        f"• {NEW} تسجيل عملية — مصاريف/إيراد/طلبية/ملاحظة/مهمة\n"
        "• 📊 التقارير — ملخصات، رسوم بيانية، مقارنة فترات\n"
        f"• {TASK} مهامي — قوائم المهام وإنجازها وحذفها\n"
        "• 🧰 أدوات — آخر سجل، ميزانيات، رسم بياني، تصدير، تحويل، بحث، مساحة مشتركة\n\n"
        "⚠️ الخصوصية: نصوصك/صورك تُرسل لـ Gemini (انظر /privacy). التشفير لا يغطي الإرسال.\n\n"
        "⌨️ الأوامر السريعة:\n"
        "/menu — القائمة الرئيسية\n"
        "/privacy — إعدادات الخصوصية وحذف الملغاة\n"
        "/done وصف — إنجاز مهمة\n"
        "/undo — تراجع عن آخر سجل\n"
        "/redo — استعادة آخر سجل تم التراجع عنه\n"
        "/convert مبلغ من إلى — تحويل عملة (مثل: /convert 300 ILS USD)\n"
        "/budget سقف — إدارة ميزانية شهرية\n"
        "/report — ملخص الفترة\n"
        "/export — تصدير Excel\n"
        "/chart — رسم بياني بالمصاريف\n"
        "/report_on — تقرير دوري تلقائي\n"
        "/report_off — إيقاف التقرير الدوري\n"
        "/work — إدارة المساحة المشتركة\n"
        "/company — شركتك وعضويتها\n"
        "/join — الانضمام برمز دعوة\n"
        "/debts — من يدين لك ومن تدين له\n"
        "/finance — بطاقة ذمم موحّدة (ديون/فواتير/ائتمان)\n"
        "/invoices — الفواتير الآجلة\n"
        "/orders — الطلبيات وحالتها\n"
        "/credit — الحدود الائتمانية للأشخاص\n"
        "/bonus — البونس والمكافآت ونقاط الولاء\n"
        "/stats — إحصائيات استخدامك\n"
        "/kpi — لوحة مؤشرات أداء أعمالك (مال الشهر/مهام/فواتير)\n"
        "/brief — نشرة إدارة يومية ذكية عن حالة عملك\n"
        "/health — فحص صحة النظام\n"
        "/export pdf — تصدير PDF بكل السجلات\n"
        "/forecast — توقعات الأشهر القادمة\n"
        "/deviation — انحراف الإنفاق عن المتوسط\n"
        "/lang — تبديل لغة الواجهة عربي/English\n"
        "/cancel — إلغاء أي عملية معلّقة\n\n"
        "📸 صوّر أي فاتورة وأرسلها، وسأستخرج تفاصيلها تلقائيًا.\n"
        "🎤 أرسل صوتًا وسأفهمه كعملية.\n\n"
        "تحتاج تفاصيل أكثر؟ أرسل أي سؤال مكتوبًا بصياغتك الطبيعية."
    )
    await send_main_menu(update.message, context, text=guide)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /menu — يعرض القائمة الرئيسية (بأزرار InlineKeyboard)."""
    from bot.menus import send_main_menu

    await send_main_menu(update.message, context)


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    from app.database.crud import find_pending_tasks

    if len(text.strip()) < 3:
        await update.message.reply_text("وصف المهمة قصير جدًا — أرسل 3 أحرف على الأقل أو اختر من القائمة.")
        return
    db = SessionLocal()
    try:
        candidates = find_pending_tasks(db, telegram_user_id, text)
        if len(candidates) > 1:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            rows = [
                [InlineKeyboardButton(f"✅ {c.description[:40]}", callback_data=f"tsk:done:{c.id}")]
                for c in candidates[:8]
            ]
            await update.message.reply_text(
                f"وجدت {len(candidates)} مهمة تطابق \"{text}\" — اختر التي تريد إنجازها:",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        elif len(candidates) == 1:
            task = candidates[0]
            from bot.menus import MAIN_HOME_KEYBOARD

            _done_res = complete_task(db, telegram_user_id, task.id)
            if _done_res is None:
                await update.message.reply_text("❌ لا تملك صلاحية إنجاز هذه المهمة.")
                return
            await update.message.reply_text(
                f"تم إنجاز المهمة: {task.description}", reply_markup=MAIN_HOME_KEYBOARD
            )
        else:
            await update.message.reply_text("لم أجد مهمة مطابقة ضمن مهامك المعلّقة.")
    finally:
        db.close()


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /lang — تبديل لغة الواجهة بين العربية والإنجليزية."""
    from bot.menus import send_lang_menu

    await send_lang_menu(update.message)


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def redo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /redo — يستعيد آخر سجل تم التراجع عنه (/undo عكسيًا)."""
    from app.database.crud import restore_last_deleted

    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        restored = restore_last_deleted(db, telegram_user_id)
    finally:
        db.close()
    if restored:
        from bot.menus import MAIN_HOME_KEYBOARD

        await update.message.reply_text(
            f"تمت استعادة آخر سجل:\n{restored['kind']}: {restored['label'] or '(بدون وصف)'}",
            reply_markup=MAIN_HOME_KEYBOARD,
        )
    else:
        await update.message.reply_text("لا توجد سجلات محذوفة يمكن استعادتها.")


async def convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    result = await asyncio.to_thread(convert, amount, from_cur, to_cur)
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


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /report — يولّد تقرير Excel للمعاملات المالية ويُرسله كملف."""
    telegram_user_id = update.effective_user.id
    db_perm = SessionLocal()
    try:
        from app.database.crud import has_permission

        if not has_permission(db_perm, telegram_user_id, "export"):
            await update.message.reply_text("ليس لديك صلاحية تصدير التقارير.")
            return
    finally:
        db_perm.close()
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


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /export [today|week|month|pdf] — ملف Excel واحد بكل السجلات للفترة المحددة."""
    telegram_user_id = update.effective_user.id
    db_perm = SessionLocal()
    try:
        from app.database.crud import has_permission

        if not has_permission(db_perm, telegram_user_id, "export"):
            await update.message.reply_text("ليس لديك صلاحية التصدير.")
            return
    finally:
        db_perm.close()
    from datetime import timedelta

    from app.timeutil import first_day_of_week, now_local, to_utc_naive

    arg = (context.args[0] if context.args else "all").lower()
    # دعم "pdf" ومرادفات عربية (بي دي إف مع/بدون شرطة سفلية) — كان "بي_دي_إف" وحده يربك المستخدم
    if arg in ("pdf", "بي_دي_إف", "بي دي إف", "بي-دي-إف"):
        await _export_pdf(update, context)
        return

    from bot.exporters import generate_export_excel

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


async def _export_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تصدير PDF بكل السجلات (الفترة الاختيارية عبر /export pdf [period] أعلاه)."""
    from datetime import timedelta

    from app.timeutil import first_day_of_week, now_local, to_utc_naive
    from bot.exporters import generate_export_pdf

    arg = (context.args[1] if len(context.args or []) > 1 else "all").lower()
    period = resolve_period_arg(arg)
    local_now = now_local()
    start = None
    if period == "today":
        local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)
    elif period == "week":
        fd = first_day_of_week()
        weekday = local_now.weekday()
        local = local_now - timedelta(days=(weekday - fd) % 7)
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)
    elif period == "month":
        local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = to_utc_naive(local)

    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        try:
            buf = generate_export_pdf(db, telegram_user_id, start_utc=start)
        except Exception:
            logger.exception("خطأ في توليد PDF")
            await update.message.reply_text(
                "حدث خطأ أثناء توليد ملف PDF (reportlab غير متوفر؟). جرّب /export للملف Excel."
            )
            return
    finally:
        db.close()

    if buf is None:
        await update.message.reply_text("لم يُولَّد الملف.")
        return

    await update.message.reply_chat_action("upload_document")
    now_str = local_now.strftime("%Y-%m-%d")
    await update.message.reply_document(
        document=buf,
        filename=f"export_{arg}_{now_str}.pdf",
        caption="تصدير PDF — يحتوي: معاملات + مهام + طلبيات وملاحظات",
    )


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /chart — رسم بياني (matplotlib) بمقارنة الإيرادات والمصاريف شهريًا."""
    from app.charts import generate_monthly_chart
    from app.database.db import SessionLocal as _S

    telegram_user_id = update.effective_user.id

    def _gen_chart() -> BytesIO | None:
        db = _S()
        try:
            return generate_monthly_chart(db, telegram_user_id)
        finally:
            db.close()

    try:
        buf = await asyncio.to_thread(_gen_chart)
    except Exception:
        logger.exception("خطأ في توليد الرسم البياني")
        await update.message.reply_text("حدث خطأ أثناء توليد الرسم البياني، حاول لاحقًا.")
        return

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


async def report_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def report_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /report_off — إيقاف التقرير الدوري."""
    from app.database.crud import set_report_frequency

    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        set_report_frequency(db, telegram_user_id, "off")
    finally:
        db.close()
    await update.message.reply_text("تم إيقاف التقارير الدورية.")


async def notif_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /notif — عرض وتعديل تفضيلات الإشعارات."""
    from app.database.crud.common import get_or_create_user_pref, toggle_notification_pref

    LABELS = {
        "notif_task_reminder": ("⏰ تذكيرات المهام", True),
        "notif_budget_alert": ("💰 تنبيهات الميزانية", True),
        "notif_credit_alert": ("💳 تنبيهات الحد الائتماني", True),
        "notif_invoice_alert": ("🧾 تنبيهات الفواتير", True),
        "notif_morning_summary": ("☀️ الملخص الصباحي", True),
        "notif_deviation": ("🚨 تنبيهات الانحراف", True),
        "notif_periodic_report": ("📊 التقارير الدورية", True),
        "notif_proactive": ("🚨 النشرة الاستباقية اليومية", True),
    }

    telegram_user_id = update.effective_user.id
    args = context.args or []

    if len(args) == 2:
        field_name = args[0]
        value = args[1].lower()
        if field_name not in LABELS or value not in ("on", "off"):
            await update.message.reply_text(
                "الاستخدام: /notif <نوع> on|off\n"
                "مثال: /notif notif_task_reminder off\n\n"
                "الأنواع المتاحة:\n" +
                "\n".join(f"• {k}" for k in LABELS)
            )
            return
        db = SessionLocal()
        try:
            toggle_notification_pref(db, telegram_user_id, field_name, value == "on")
        finally:
            db.close()
        label = LABELS[field_name][0]
        status = "✅ مفعّل" if value == "on" else "❌ معطّل"
        await update.message.reply_text(f"{label}: {status}")
        return

    # عرض الحالة الحالية
    db = SessionLocal()
    try:
        pref = get_or_create_user_pref(db, telegram_user_id)
    finally:
        db.close()

    lines = ["⚙️ تفضيلات الإشعارات الحالية:\n"]
    for field, (label, _) in LABELS.items():
        enabled = getattr(pref, field, True)
        icon = "✅" if enabled else "❌"
        lines.append(f"{icon} {label}: {'مفعّل' if enabled else 'معطّل'}")
    lines.append("\nلتعديل: /notif <نوع> on|off")
    await update.message.reply_text("\n".join(lines))


async def budget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /budget — ميزانيات شهرية للمصاريف حسب العملة أو الشخص."""
    # فحص صلاحية الميزانية
    telegram_user_id = update.effective_user.id
    db_perm = SessionLocal()
    try:
        from app.database.crud import has_permission

        if not has_permission(db_perm, telegram_user_id, "budget.manage"):
            await update.message.reply_text("ليس لديك صلاحية إدارة الميزانيات.")
            return
    finally:
        db_perm.close()
    from app.database.crud import (
        budget_monthly_reset,
        budget_usage,
        create_budget,
        delete_budget,
        list_budgets,
    )
    from bot.bonus import split_name_and_amount

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

            if scope_txt.lower() in ("عملة", "currency"):
                scope = "currency"
            elif scope_txt.lower() in ("شخص", "person"):
                scope = "person"
            elif scope_txt.lower() in ("تصنيف", "category", "فئة"):
                scope = "category"
            else:
                await update.message.reply_text(
                    "النطاق غير معروف. استخدم: عملة|currency أو شخص|person أو تصنيف|category"
                )
                return

            target, amount, _ = split_name_and_amount(args[2:])
            if not target or amount is None:
                await update.message.reply_text(
                    "لم أستطع قراءة الهدف والمبلغ. مثال:\n"
                    "/budget إضافة شخص أبو محمد 1500"
                )
                return
            limit_str = str(amount)

            if scope == "currency":
                from app.database.crud import normalize_currency
                from app.exchange import CURRENCY_NAMES

                norm = normalize_currency(target)
                if norm is None or norm.upper() not in CURRENCY_NAMES:
                    await update.message.reply_text(
                        f"العملة غير معروفة: {target}.\n"
                        "العملات المدعومة: " + "، ".join(sorted(CURRENCY_NAMES))
                    )
                    return

            budget = create_budget(db, telegram_user_id, scope, target, limit_str)
            if not budget:
                await update.message.reply_text(
                    "لم يتم إنشاء الميزانية. ربما ميزانية بنفس النطاق والهدف موجودة مسبقًا "
                    "أو المبلغ غير صالح."
                )
                return

            scope_txt = {
                "currency": budget.currency,
                "person": f"الشخص {budget.person}",
                "category": f"التصنيف {budget.category}",
            }.get(budget.scope, budget.scope)
            await update.message.reply_text(
                f"تم إنشاء ميزانية شهرية: {scope_txt} — {fmt_amount(budget.monthly_limit)}\n"
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
            target_txt = {
                "currency": CURRENCY_NAMES.get(b.currency, b.currency),
                "person": f"الشخص {b.person}",
                "category": f"التصنيف {b.category}",
            }.get(b.scope, str(getattr(b, b.scope, "")))
            if usage["over"]:
                status = "⚠️ تجاوزت"
            elif usage["percent"] >= 80:
                status = "⚠️ قريب من السقف"
            else:
                status = "ضمن الحدود"
            lines.append(
                f"{b.id}. {target_txt}: {fmt_amount(usage['spent'])} / {fmt_amount(usage['limit'])} "
                f"({usage['percent']}%) — {status}"
            )
        lines.append("\nلحذف: /budget حذف <رقم>")
        await update.message.reply_text("\n".join(lines))
    finally:
        db.close()


async def debts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /debts — رصيد كل شخص (من يدين لك ومن تدين له)."""
    import asyncio

    from app.database.crud import person_debts
    from bot.formatters import format_debts

    telegraph_id = update.effective_user.id

    def _load():
        db = SessionLocal()
        try:
            return person_debts(db, telegraph_id)
        finally:
            db.close()

    payload = await asyncio.to_thread(_load)
    await safe_reply(update.message, format_debts(payload))


async def finance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /finance — بطاقة موحّدة: ديون + فواتير + حدود ائتمانية."""
    import asyncio

    from app.database.crud import credit_usage, list_credit_limits, list_invoices, person_debts
    from bot.formatters import format_finance_card

    telegraph_id = update.effective_user.id

    def _load():
        db = SessionLocal()
        try:
            debts = person_debts(db, telegraph_id)
            invoices = list_invoices(db, telegraph_id, status=None, limit=50)
            limits = list_credit_limits(db, telegraph_id)
            credit_payload = [
                {"person": lim.person, "usage": credit_usage(db, lim)} for lim in limits
            ]
            return debts, invoices, credit_payload
        finally:
            db.close()

    debts, invoices, credit_payload = await asyncio.to_thread(_load)
    await safe_reply(update.message, format_finance_card(debts, invoices, credit_payload))


async def invoices_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /invoices — قائمة الفواتير الآجلة (معلّقة/متأخرة) + إنشاء."""
    from app.database.crud import create_invoice, list_invoices
    from bot.formatters import format_invoices

    telegraph_id = update.effective_user.id
    args = context.args or []
    # إنشاء فاتورة: /invoices add 500 ILS محمد due:2026-10-01 وصف
    if args and args[0].strip().lower() in ("add", "إضافة", "اضافة", "new", "إنشاء", "create"):
        if len(args) < 3:
            await update.message.reply_text(
                "استخدم: /invoices add <المبلغ> [العملة] [الشخص] [due:YYYY-MM-DD] [وصف]\n"
                "مثال: /invoices add 500 ILS محمد due:2026-10-01 فاتورة مواد"
            )
            return

        # تحليل: أول رقم هو المبلغ، الباقي عملة/شخص/تاريخ
        raw = " ".join(args[1:])
        # استخراج المبلغ والعملة
        import re

        m = re.search(r"(\d+(?:[.,]\d+)?)", raw)
        if not m:
            await update.message.reply_text("لم أجد مبلغًا صالحًا.")
            return
        amount = m.group(1).replace(",", ".")
        # عملة
        from app.database.crud import normalize_currency

        currency = None
        person = None
        due_date = None
        desc = None
        tokens = raw.replace(amount, "", 1).split()
        for tok in tokens:
            if tok.lower().startswith("due:"):
                due_date = tok.split(":", 1)[1]
                continue
            cur = normalize_currency(tok)
            if cur and not currency:
                currency = cur
                continue
            if not person:
                person = tok
            else:
                desc = (desc + " " + tok) if desc else tok
        db = SessionLocal()
        try:
            inv = create_invoice(
                db,
                telegraph_id,
                {"amount": amount, "currency": currency, "person": person, "description": desc, "due_date": due_date},
                raw_message=raw,
            )
        finally:
            db.close()
        if inv:
            await update.message.reply_text(f"{SUCCESS} أُنشئت فاتورة #{inv.id} لـ {inv.person or 'بدون شخص'}: {inv.amount} {inv.currency or ''}")
        else:
            await update.message.reply_text("تعذر إنشاء الفاتورة — تحقق من المبلغ والصلاحية.")
        return

    status_arg = (args[0].strip().lower() if args else "") or None
    # معالجة overdue بشكل صحيح
    if status_arg in ("overdue", "متأخرة", "متاخرة", "overdue"):
        status = "overdue"
    else:
        status_map = {"paid": "paid", "المسددة": "paid", "مدفوع": "paid"}
        status = status_map.get(status_arg, "pending" if status_arg in (None, "pending", "المعلقة", "معلق") else None)

    db = SessionLocal()
    try:
        if status == "pending" and not status_arg:
            # افتراضي: اعرض معلّقة + متأخرة معًا (المتأخرة هي pending فات موعدها)
            pending = list_invoices(db, telegraph_id, status="pending", limit=50)
            overdue = list_invoices(db, telegraph_id, status="overdue", limit=50)
            # دمج مع ترتيب زمني (الأحدث أولًا) — list_invoices مرتبة desc
            invoices = sorted(pending + overdue, key=lambda x: x.created_at or 0, reverse=True)[:50]
            title = "🧾 الفواتير الآجلة (معلّقة + ⚠️ متأخرة):"
            # إن كانت القائمة فارغة، استخدم pending فقط
            if not invoices:
                invoices = pending
                title = "🧾 الفواتير الآجلة:"
        elif status == "pending":
            # طلب صريح لـ pending فقط — لكن اعرض overdue ضمن pending إن وجدت
            pending = list_invoices(db, telegraph_id, status="pending", limit=50)
            overdue = list_invoices(db, telegraph_id, status="overdue", limit=50)
            invoices = (pending + overdue)[:50]
            title = "🧾 الفواتير الآجلة (معلّقة + ⚠️ متأخرة):"
            if not overdue:
                title = "🧾 الفواتير الآجلة:"
                invoices = pending
        else:
            invoices = list_invoices(db, telegraph_id, status=status, limit=50)
            title = {
                "paid": f"{SUCCESS} الفواتير المسددة:",
                "overdue": "⚠️ الفواتير المتأخرة:",
                "pending": "🧾 الفواتير الآجلة:",
            }.get(status or "pending", "🧾 الفواتير الآجلة:")
    finally:
        db.close()

    await safe_reply(update.message, format_invoices(invoices, title=title))


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /orders — قائمة الطلبيات وحالتها (مفتوحة/منجزة)."""
    from app.database.crud import list_orders
    from bot.formatters import format_orders

    telegraph_id = update.effective_user.id
    args = context.args or []
    status_arg = (args[0].strip().lower() if args else "") or None
    if status_arg in ("done", "منجزة", "مكتملة", "انجزت"):
        status = "done"
    elif status_arg in ("open", "مفتوحة", "المفتوحة"):
        status = "open"
    else:
        status = "open"

    db = SessionLocal()
    try:
        orders = list_orders(db, telegraph_id, status=status, limit=50)
    finally:
        db.close()

    title = f"{SUCCESS} الطلبيات المنجزة:" if status == "done" else "🛒 الطلبيات المفتوحة:"
    await update.message.reply_text(format_orders(orders, title=title))


async def credit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /credit — حدود ائتمانية للأشخاص.

    /credit قائمة
    /credit إضافة <الشخص> <المبلغ>   (مثال: /credit إضافة محمد 5000)
    """
    from app.database.crud import credit_usage, list_credit_limits, set_credit_limit
    from bot.formatters import format_credit_limits

    telegraph_id = update.effective_user.id
    args = context.args or []
    # الكتابة تبقى متزامنة (سريعة)؛ القراءة تُنقل لـ thread
    if args and args[0].strip().lower() in ("add", "اضافة", "إضافة"):
        if len(args) < 3:
            await update.message.reply_text("استخدم: /credit إضافة <الشخص> <المبلغ>\nمثال: /credit إضافة أبو محمد 5000")
            return
        from bot.bonus import split_name_and_amount

        person, limit_dec, _ = split_name_and_amount(args[1:])
        if not person or limit_dec is None:
            await update.message.reply_text("استخدم: /credit إضافة <الشخص> <المبلغ>\nمثال: /credit إضافة أبو محمد 5000")
            return
        limit = str(limit_dec)
        db = SessionLocal()
        try:
            row = set_credit_limit(db, telegraph_id, person, limit)
        finally:
            db.close()
        if not row:
            await update.message.reply_text("لم يُضبط الحد. المبلغ يجب أن يكون رقمًا موجبًا أو صلاحية مفقودة.")
            return
        await update.message.reply_text(
            f"{SUCCESS} حُدّد سقف ائتماني لـ {row.person}: {fmt_amount(row.limit_amount)}\n"
            "سأرسل تنبيهًا عند الاقتراب من السقف أو تجاوزه."
        )
        return

    import asyncio

    def _load():
        db2 = SessionLocal()
        try:
            lims = list_credit_limits(db2, telegraph_id)
            return [
                {"person": lim.person, "usage": credit_usage(db2, lim)} for lim in lims
            ]
        finally:
            db2.close()

    payload = await asyncio.to_thread(_load)
    await safe_reply(update.message, format_credit_limits(payload))


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /health — فحص صحة النظام (قاعدة، هجرات، إعدادات، إصدارات).

    مقيّد بالأدمن فقط: يعرض إصدارات المكتبات، حالة الهجرات، أسماء المتغيرات
    الناقصة، حالة ENCRYPTION_KEY، وحجم قاعدة البيانات — معلومات داخلية
    لا يجب كشفها لكل مستخدم.
    """
    if not await _require_admin(update, "health", context):
        return
    from bot.diagnostics import run_health_checks
    from bot.formatters import format_health_report

    db = SessionLocal()
    try:
        checks = run_health_checks(db)
    finally:
        db.close()
    await update.message.reply_text(format_health_report(checks))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /stats — إحصائيات استخدام البوت حسب مساحة عملك."""
    import asyncio

    from app.database.crud import user_stats
    from bot.formatters import format_user_stats

    telegraph_id = update.effective_user.id

    def _load():
        db = SessionLocal()
        try:
            return user_stats(db, telegraph_id)
        finally:
            db.close()

    stats = await asyncio.to_thread(_load)
    await update.message.reply_text(format_user_stats(stats))


async def forecast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /forecast [عدد الأشهر] — توقعات المصاريف/الإيرادات للأشهر القادمة."""
    from app.database.crud import forecast_totals
    from bot.formatters import format_forecast

    telegraph_id = update.effective_user.id
    months = 3
    if context.args:
        try:
            months = max(1, min(int(context.args[0]), 12))
        except ValueError:
            months = 3
    db = SessionLocal()
    try:
        payload = forecast_totals(db, telegraph_id, months=months)
    finally:
        db.close()
    await safe_reply(update.message, format_forecast(payload))


async def deviation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /deviation — انحراف إنفاق/إيراد الشهر الحالي عن متوسط آخر 3 أشهر."""
    from app.database.crud import deviation_summary
    from bot.formatters import format_deviation

    telegraph_id = update.effective_user.id
    db = SessionLocal()
    try:
        payload = deviation_summary(db, telegraph_id)
    finally:
        db.close()
    await update.message.reply_text(format_deviation(payload))


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر مخفي /admin_stats — إحصائيات عامة للمسؤول (لا يكشف بيانات فردية)."""
    from app.admin import build_admin_stats

    if not await _require_admin(update, "admin_stats", context):
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
        f"{TASK} مهام معلّقة: {stats['pending_tasks']}",
        f"⚠️ مهام متأخرة: {stats['overdue_tasks']}",
        f"🗒️ طلبيات/ملاحظات: {stats['notes_count']}",
        f"🎯 عدد الميزانيات: {stats['budgets_count']}",
        f"📅 تقارير دورية مفعّلة: {stats['active_reports']}",
    ]

    def _sum_line(label: str, totals: dict[str, Decimal]) -> str:
        if not totals:
            return f"{label}: 0"
        parts = ", ".join(f"{v} {c}" for c, v in totals.items())
        return f"{label}: {parts}"

    lines.append(_sum_line(f"{EXPENSE} إجمالي المصاريف", stats["total_expenses"]))
    lines.append(_sum_line(f"{INCOME} إجمالي الإيرادات", stats["total_incomes"]))

    if stats.get("pending_feedback"):
        lines.append(
            f"🧾 تحليلات خاطئة بانتظار المراجعة: {stats['pending_feedback']} (انظر /feedback)"
        )

    await update.message.reply_text("\n".join(lines))


def _is_admin(user_id: int) -> bool:
    from app.config import settings

    return user_id in settings.admin_user_ids


def _get_bot(update: Update | None, context: ContextTypes.DEFAULT_TYPE | None = None):
    """يعيد كائن Bot المتاح: context.bot أولاً (PTB v20+)، ثم fallback للاختبارات."""
    if context is not None and hasattr(context, "bot") and context.bot is not None:
        return context.bot
    if update is not None:
        if hasattr(update, "get_bot"):
            try:
                b = update.get_bot()
                if b is not None:
                    return b
            except Exception:
                pass
        if hasattr(update, "bot") and update.bot is not None:
            return update.bot
        # fallback قديم: effective_user.bot (غير موجود في PTB v20، لكن اختبارات SimpleNamespace تستخدمه)
        try:
            eu = getattr(update, "effective_user", None)
            if eu is not None and hasattr(eu, "bot") and eu.bot is not None:
                return eu.bot
        except Exception:
            pass
    return None


async def _alert_admin_of_probe(update: Update, attacker_id: int, count: int, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    """يرسل إنذارًا فوريًا لأول أدمن مُعدّ عند تجاوز عتبة استكشاف الصلاحيات."""
    from app.config import settings

    bot = _get_bot(update, context)
    try:
        if not settings.admin_user_ids:
            return
        if bot is None:
            logger.error("فشل إرسال إنذار الأمان: لا يوجد bot متاح (context.bot is None)")
            return
        await bot.send_message(
            chat_id=settings.admin_user_ids[0],
            text=f"⚠️ إنذار أمني: {count} محاولات فاشلة لأوامر الأدمن من user={attacker_id} خلال ٥ دقائق.",
        )
    except Exception:
        logger.exception("فشل إرسال إنذار الأمان الخاص بمحاولات الأدمن الفاشلة")


async def _require_admin(update: Update, label: str, context: ContextTypes.DEFAULT_TYPE | None = None) -> bool:
    """بوابة موحّدة لأوامر الأدمن: يرفض غير المصرّح له مع تتبّع المحاولات
    الفاشلة وإنذار عند بلوغ عتبة استكشاف الصلاحيات."""
    from bot.ratelimit import (
        ADMIN_ATTEMPTS_MAX,
        ADMIN_ATTEMPTS_WINDOW,
        is_admin_probing,
        register_admin_denied,
    )

    user_id = update.effective_user.id
    if _is_admin(user_id):
        return True
    denied = register_admin_denied(user_id)
    logger.warning("محاولة وصول غير مصرّح لأمر الأدمن (%s) من user=%s", label, user_id)
    if is_admin_probing(user_id):
        logger.critical(
            "استكشاف صلاحيات مفترض: %d محاولات فاشلة لأوامر الأدمن من user=%s خلال %ds",
            denied,
            user_id,
            int(ADMIN_ATTEMPTS_WINDOW),
        )
        if denied == ADMIN_ATTEMPTS_MAX:
            await _alert_admin_of_probe(update, user_id, denied, context)
    await update.message.reply_text("عذرًا، هذا الأمر متاح للمسؤول فقط.")
    return False


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر مخفي /feedback — يعرض رسائل التحليل الخاطئ بانتظار المراجعة اليدوية."""
    if not await _require_admin(update, "feedback", context):
        return

    from app.database.crud import list_correction_feedback

    db = SessionLocal()
    try:
        items = list_correction_feedback(db, only_unreviewed=True, limit=20)
    finally:
        db.close()

    if not items:
        await update.message.reply_text(f"{SUCCESS} لا توجد تحليلات خاطئة بانتظار المراجعة.")
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


async def feedback_ack_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر مخفي /feedback_ack <id> — يعلّم سجل تحليل خاطئ كمراجَع يدويًا."""
    if not await _require_admin(update, "feedback_ack", context):
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

    await update.message.reply_text(f"{SUCCESS} عُلِّم السجل كمراجَع." if ok else "لم أجد سجلًا بهذا الرقم.")


async def kpi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /kpi — لوحة مؤشرات أداء مختصرة (مال هذا الشهر/السابق + مهام/فواتير/ميزانيات)."""
    import asyncio

    from app.database.crud import kpi_dashboard
    from bot.formatters import format_kpi_dashboard

    def _load():
        db = SessionLocal()
        try:
            return kpi_dashboard(db, update.effective_user.id)
        finally:
            db.close()

    payload = await asyncio.to_thread(_load)
    await update.message.reply_text(format_kpi_dashboard(payload))


async def brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /brief — نشرة إدارة يومية بالذكاء الاصطناعي (تسقط للنص المحلي عند فشل Gemini)."""
    from bot.formatters import safe_reply
    from bot.ratelimit import is_rate_limited

    if is_rate_limited(update.effective_user.id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return
    import asyncio

    from app.ai_service import generate_daily_brief
    from app.database.crud import kpi_dashboard
    from app.database.crud.common import has_ai_consent
    from app.database.db import SessionLocal as _SLb
    from bot.formatters import format_brief_data, format_kpi_dashboard
    from bot.ratelimit import check_and_increment_daily

    # فحص الموافقة والحصة اليومية قبل إرسال ملخص مالي لـ Gemini
    _consent = True
    try:
        _dbb = _SLb()
        try:
            _consent = has_ai_consent(_dbb, update.effective_user.id)
        finally:
            _dbb.close()
    except Exception:
        _consent = True

    def _load():
        db = SessionLocal()
        try:
            return kpi_dashboard(db, update.effective_user.id)
        finally:
            db.close()

    payload = await asyncio.to_thread(_load)
    brief = None
    if _consent and check_and_increment_daily(update.effective_user.id):
        brief = await asyncio.to_thread(generate_daily_brief, format_brief_data(payload))
    if brief:
        await safe_reply(update.message, brief)
    else:
        await safe_reply(update.message, format_kpi_dashboard(payload))


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /join <code> — الانضمام لشركة برمز دعوة."""
    from app.database.db import SessionLocal

    args = context.args or []
    if not args:
        await update.message.reply_text("استخدم: /join <رمز الدعوة>\nمثال: /join 482913  أو  /join inv_a1b2c3")
        return
    token = _clean_join_token(args[0])
    # دعم رابط كامل
    if "inv_" in token:
        token = _clean_join_token(token.split("inv_")[-1].split()[0].split("&")[0])
    telegram_user_id = update.effective_user.id

    from bot.ratelimit import is_join_blocked, register_join_denied

    if is_join_blocked(telegram_user_id):
        await update.message.reply_text("تم حظر المحاولات مؤقتًا (5 محاولات فاشلة). حاول بعد 15 دقيقة.")
        return

    from app.database.crud.company import redeem_invite

    display_name = update.effective_user.first_name or None
    db = SessionLocal()
    try:
        ok, result = redeem_invite(db, telegram_user_id, token, display_name=display_name)
    finally:
        db.close()
    if ok:
        company = result
        await update.message.reply_text(f"✅ انضممت إلى شركة '{company.name}' بنجاح!")
        try:
            bot = _get_bot(update, context)
            if bot:
                await bot.send_message(
                    chat_id=company.owner_telegram_user_id,
                    text=f"👤 انضم {display_name or telegram_user_id} إلى شركتك '{company.name}'.",
                )
            else:
                logger.warning("لا يوجد bot لإرسال إشعار انضمام للمالك %s", company.owner_telegram_user_id)
        except Exception:
            logger.exception("فشل إرسال إشعار انضمام للمالك")
        from bot.menus import send_main_menu

        try:
            await send_main_menu(update.message, context, text=f"أهلاً بك في {company.name} 👋")
        except Exception:
            pass
    else:
        register_join_denied(telegram_user_id)
        await update.message.reply_text(f"❌ {result}")


async def company_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /company — عرض معلومات شركتك وإدارتها."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from app.database.crud.company import (
        company_id_for_user,
        company_member_ids,
        get_company,
        get_company_member,
    )
    from app.database.db import SessionLocal
    from app.permissions import ROLE_LABELS

    uid = update.effective_user.id
    db = SessionLocal()
    try:
        cid = company_id_for_user(db, uid)
        if cid is None:
            await update.message.reply_text("لست عضوًا في أي شركة بعد. أنشئ شركة عبر /start.")
            return
        comp = get_company(db, cid)
        m = get_company_member(db, uid)
        members = company_member_ids(db, cid)
        role_label = ROLE_LABELS.get(m.role, m.role) if m else "—"
        text = (
            f"🏢 {comp.name}\n"
            f"📝 {comp.description or '—'}\n"
            f"🏷️ النوع: {comp.business_type or '—'} | 💱 {comp.base_currency or '—'}\n"
            f"👥 الأعضاء: {len(members)} | 🎖️ دورك: {role_label}\n"
            f"🆔 #{comp.id}"
        )
        # أزرار تعديل للمالك فقط
        rows = []
        if m and m.role == "owner":
            rows.append([InlineKeyboardButton("✏️ تعديل الاسم", callback_data="company:edit_name"), InlineKeyboardButton("📝 تعديل الوصف", callback_data="company:edit_desc")])
            rows.append([InlineKeyboardButton("👥 الفريق", callback_data="team:menu")])
            rows.append([InlineKeyboardButton("🔑 نقل الملكية", callback_data="company:transfer"), InlineKeyboardButton("🗑️ حذف الشركة", callback_data="company:delete")])
        else:
            rows.append([InlineKeyboardButton("👥 الفريق", callback_data="team:menu")])
        rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:main")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
    finally:
        db.close()


async def work_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /work — إدارة الحساب المشترك (مساحة عمل لعدة معرّفات Telegram).

    الاستخدام:
      /work            ← الحالة الحالية (مساحة؟ أعضاء؟ دعوة معلّقة؟)
      /work جديد       ← إنشاء مساحتك الخاصة (أنت مديرها)
      /work اضافة <id> ← يدعو المالك معرّفًا — لا يُدمج حتى يقبل الطرف
      /work قبول       ← قبول الدعوة المعلّقة بانضمامك
      /work رفض        ← رفض الدعوة المعلّقة
      /work حذف <id>   ← يُخرج المالك عضوًا / يُلغي دعوة (لا يمس المالك نفسه)
      /work مغادرة     ← العضو يغادر (المالك لا يغادر)
    """
    from app.database.crud import (
        accept_workspace_invite,
        create_workspace,
        decline_workspace_invite,
        invite_to_workspace,
        leave_workspace,
        list_workspace,
        pending_workspace_invite,
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
                f"{SUCCESS} أُنشئت مساحة عملك. بياناتك الآن تُقرأ ضمن أعضاء مساحتك.\n"
                "لإضافة مشارك: /work اضافة <المعرّف الرقمي لتيليغرام>"
            )
            return

        if action == "اضافة" and len(args) >= 2 and args[1].isdigit():
            target = int(args[1])
            ok = invite_to_workspace(db, telegram_user_id, target)
            if ok:
                await update.message.reply_text(
                    f"{SUCCESS} أُرسلت دعوة للمعرّف {target} إلى مساحتك.\n"
                    "لن تُدمج بياناتكما حتى يقبل الطرف الدعوة بنفسه."
                )
            else:
                await update.message.reply_text(
                    "تعذّرت الدعوة: لست المالك، أو معرّف ذاتي، "
                    "أو الطرف عضو نشط في مساحة أخرى (لا سحب دون موافقته)."
                )
            return

        if action in ("قبول", "تقبل"):
            invite_wid = pending_workspace_invite(db, telegram_user_id)
            ok = accept_workspace_invite(db, telegram_user_id, invite_wid) if invite_wid else False
            await update.message.reply_text(
                f"{SUCCESS} قبلت الدعوة — بياناتكما أصبحت مشتركة الآن."
                if ok
                else "لا توجد دعوة معلّقة للقبول."
            )
            return

        if action in ("رفض", "رفض الدعوة"):
            ok = decline_workspace_invite(db, telegram_user_id)
            await update.message.reply_text(
                "تم رفض دعوة الانضمام." if ok else "لا توجد دعوة معلّقة للرفض."
            )
            return

        if action == "حذف" and len(args) >= 2 and args[1].isdigit():
            target = int(args[1])
            ok = remove_from_workspace(db, telegram_user_id, target)
            await update.message.reply_text(
                f"{SUCCESS} أُخرج المعرّف {target} من مساحة عملك." if ok else "لم أجد العضو أو لست المالك."
            )
            return

        if action == "مغادرة":
            ok = leave_workspace(db, telegram_user_id)
            await update.message.reply_text(
                f"{SUCCESS} غادرت المساحة — بياناتك عادت فردية لك."
                if ok
                else "لا شيء للمغادرة (أو أنت المالك)."
            )
            return

        # الحالة الحالية
        info = list_workspace(db, telegram_user_id)
        if info is None:
            invite_wid = pending_workspace_invite(db, telegram_user_id)
            if invite_wid is not None:
                await update.message.reply_text(
                    f"📩 لديك دعوة انضمام إلى مساحة عمل #{invite_wid}.\n\n"
                    "ابقَ مسؤولًا عن بياناتك: لا يُدمج شيء قبل موافقتك.\n"
                    "• للقبول: /work قبول\n• للرفض: /work رفض"
                )
                return
            await update.message.reply_text(
                "أنت حاليًا بمساحة فردية (بياناتك خاصة بك).\n"
                "لإنشاء مساحة مشتركة: /work جديد\n"
                "ثم أضف شركاءك: /work اضافة <المعرّف الرقمي>"
            )
            return

        members = ", ".join(str(m) for m in info["members"])
        role = "مالك" if info["owner"] else "عضو"
        text = (
            f"🏢 مساحة العمل: {info['workspace_id']} (أنت: {role})\n"
            f"الأعضاء ({len(info['members'])}): {members}\n\n"
            f"مشاركون يرون نفس البيانات (معاملات/مهام/ميزانيات).\n"
            f"• إضافة: /work اضافة <id>   • إزالة: /work حذف <id>   • مغادرة: /work مغادرة"
        )
        if info["owner"] and info["pending"]:
            text += (
                "\n\n⏳ دعوات بانتظار القبول:\n"
                + "\n".join(f"• {m}" for m in info["pending"])
            )
        await update.message.reply_text(text)
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



async def requirements_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض وثيقة المتطلبات (SRS) لـ BizMate."""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "SRS_BizMate.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        await update.message.reply_text("تعذّر قراءة وثيقة المتطلبات.")
        return
    # تيليجرام حد 4096 حرف
    await update.message.reply_text(text[:4000])


def register_handlers(app: Application) -> None:
    # فلتر السماح (ALLOWED_USER_IDS / registration_token) على كل الرسائل والأزرار
    try:
        from bot.ratelimit import is_user_allowed
    except Exception:
        is_user_allowed = lambda uid: True

    async def _allowlist_message_guard(update, context):
        user = getattr(update, "effective_user", None)
        if user is not None and not is_user_allowed(user.id):
            # استثناء /start و /register
            msg = getattr(update, "message", None)
            text = getattr(msg, "text", "") or ""
            if text.strip().startswith("/start") or text.strip().startswith("/register"):
                return None
            try:
                if msg is not None:
                    await msg.reply_text("غير مسموح لك باستخدام هذا البوت.")
            except Exception:
                pass
            raise ApplicationHandlerStop
        return None

    async def _allowlist_callback_guard(update, context):
        user = getattr(update, "effective_user", None)
        if user is not None and not is_user_allowed(user.id):
            try:
                if getattr(update, "callback_query", None) is not None:
                    await update.callback_query.answer("غير مسموح", show_alert=True)
            except Exception:
                pass
            raise ApplicationHandlerStop
        return None

    app.add_handler(MessageHandler(filters.ALL, _allowlist_message_guard), group=-1)
    app.add_handler(CallbackQueryHandler(_allowlist_callback_guard), group=-1)

    """يُسجّل كل المعالجات في Application (بما فيها ConversationHandler).

    يفعّل أيضًا غلاف Bidi الموحد على طرق إرسال النصوص (reply_text/
    edit_message_text/send_message) بتمريرها عبر fix_bidi — لا يلمس اختبارات
    الوحدة لأنها تستبدل الطرق على مستوى الـ instance بـ AsyncMock.
    """
    from app.rtl import install_bidi_patches

    install_bidi_patches()
    app.add_handler(CommandHandler("register", register_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("requirements", requirements_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("undo", undo_command))
    app.add_handler(CommandHandler("redo", redo_command))
    app.add_handler(CommandHandler("lang", lang_command))
    app.add_handler(CommandHandler("convert", convert_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("budget", budget_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("chart", chart_command))
    app.add_handler(CommandHandler("report_on", report_on_command))
    app.add_handler(CommandHandler("report_off", report_off_command))
    app.add_handler(CommandHandler("notif", notif_command))
    app.add_handler(CommandHandler("admin_stats", admin_stats_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("feedback_ack", feedback_ack_command))
    app.add_handler(CommandHandler("join", join_command))
    app.add_handler(CommandHandler("company", company_command))
    app.add_handler(CommandHandler("work", work_command))
    app.add_handler(CommandHandler("privacy", privacy_command))
    app.add_handler(CommandHandler("debts", debts_command))
    app.add_handler(CommandHandler("finance", finance_command))
    app.add_handler(CommandHandler("invoices", invoices_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("credit", credit_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("kpi", kpi_command))
    app.add_handler(CommandHandler("brief", brief_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("forecast", forecast_command))
    app.add_handler(CommandHandler("deviation", deviation_command))

    from bot.bonus import register_bonus_handlers, setup_bonus_check
    register_bonus_handlers(app)

    from bot.conversation import conversation_handler
    from bot.editing import edit_conversation
    from bot.menus import menu_callback_router
    from bot.onboarding import onboarding_conversation

    # ترتيب مهم: Onboarding أولًا، ثم التعديل (edit) قبل المحادثة العامة —
    # وإلا فإن input التعديل (أي نص) يُلتقط بواسطة conversation_handler
    # ويُحلل كرسالة جديدة وقد تُسجّل كعملية.
    app.add_handler(onboarding_conversation)
    app.add_handler(edit_conversation)
    app.add_handler(conversation_handler)
    app.add_handler(CallbackQueryHandler(menu_callback_router))
    app.add_error_handler(system_error_handler)

    # التذكيرات التلقائية: المهام المتأخرة، الميزانيات، الفواتير، الحدود
    # الائتمانية، التقارير الدورية، انحراف الإنفاق، النسخ الاحتياطي
    from bot.reminders import (
        setup_budget_check,
        setup_credit_check,
        setup_daily_backup,
        setup_deviation_check,
        setup_invoice_check,
        setup_morning_summary,
        setup_overdue_reminder,
        setup_periodic_reports,
        setup_proactive_check,
    )

    setup_overdue_reminder(app)
    setup_budget_check(app)
    setup_invoice_check(app)
    setup_credit_check(app)
    setup_periodic_reports(app)
    setup_deviation_check(app)
    setup_daily_backup(app)
    setup_morning_summary(app)
    setup_proactive_check(app)
    setup_bonus_check(app)
