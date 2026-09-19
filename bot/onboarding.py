"""
محادثة Onboarding للشركات: إنشاء شركة أو الانضمام برمز دعوة أو استخدام فردي.

نفس نمط bot/editing.py — ConversationHandler بحالات منفصلة.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from app.database.db import SessionLocal
from app.permissions import BUSINESS_TYPES

logger = logging.getLogger(__name__)

ASK_NAME, ASK_DESC, ASK_TYPE, ASK_CURRENCY, CONFIRM = range(20, 25)
ASK_INVITE = 25

CURRENCY_OPTIONS = ["ILS", "USD", "JOD", "EUR"]


def _clear_co_keys(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("co_name", "co_desc", "co_type", "co_currency", "co_pending_invite"):
        context.user_data.pop(key, None)


def _type_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"co:t:{key}")] for key, label in BUSINESS_TYPES.items()]
    return InlineKeyboardMarkup(rows)


def _currency_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(code, callback_data=f"co:c:{code}")] for code in CURRENCY_OPTIONS]
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ تأكيد وإنشاء", callback_data="co:confirm")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="co:cancel")],
        ]
    )


def onboarding_keyboard() -> InlineKeyboardMarkup:
    """أزرار /start للمستخدم بلا شركة."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏢 إنشاء شركة", callback_data="co:new")],
            [InlineKeyboardButton("🔑 عندي رمز دعوة", callback_data="co:join")],
            [InlineKeyboardButton("👤 استخدام فردي", callback_data="co:solo")],
        ]
    )


# ---------- إنشاء شركة ----------


async def onboarding_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _clear_co_keys(context)
    await query.edit_message_text("ما اسم شركتك؟\n(أرسل الاسم، أو /cancel للإلغاء)")
    return ASK_NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from app.validation import clean_free_text

    raw = (update.message.text or "").strip()
    cleaned = clean_free_text(raw, max_len=150)
    if not cleaned:
        await update.message.reply_text("الاسم لا يمكن أن يكون فارغًا. أرسل اسم شركتك:")
        return ASK_NAME
    context.user_data["co_name"] = cleaned[:150]
    await update.message.reply_text("شو بتعمل شركتك؟ اوصفها بجملة أو جملتين (هاد بيساعدني أجاوب أسئلة عنها):")
    return ASK_DESC


async def got_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from app.validation import clean_free_text

    raw = (update.message.text or "").strip()
    cleaned = clean_free_text(raw, max_len=500)
    # الوصف اختياري - لكن نسأل عنه
    if cleaned:
        context.user_data["co_desc"] = cleaned
    else:
        context.user_data["co_desc"] = None
    await update.message.reply_text("ما نوع نشاط شركتك؟ اختر من القائمة:", reply_markup=_type_keyboard())
    return ASK_TYPE


async def got_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key = query.data.replace("co:t:", "").strip()
    if key not in BUSINESS_TYPES:
        await query.edit_message_text("نوع غير معروف. اختر من القائمة:", reply_markup=_type_keyboard())
        return ASK_TYPE
    context.user_data["co_type"] = key
    await query.edit_message_text("ما العملة الأساسية لشركتك؟", reply_markup=_currency_keyboard())
    return ASK_CURRENCY


async def got_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    code = query.data.replace("co:c:", "").strip().upper()
    if code not in CURRENCY_OPTIONS:
        await query.edit_message_text("عملة غير معروفة. اختر:", reply_markup=_currency_keyboard())
        return ASK_CURRENCY
    context.user_data["co_currency"] = code
    name = context.user_data.get("co_name", "")
    desc = context.user_data.get("co_desc") or "—"
    btype = BUSINESS_TYPES.get(context.user_data.get("co_type", ""), "—")
    text = (
        f"ملخص الشركة:\n"
        f"• الاسم: {name}\n"
        f"• الوصف: {desc}\n"
        f"• النوع: {btype}\n"
        f"• العملة: {code}\n\n"
        f"هل تريد إنشاء الشركة بهذه البيانات؟"
    )
    await query.edit_message_text(text, reply_markup=_confirm_keyboard())
    return CONFIRM


async def got_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "co:cancel":
        _clear_co_keys(context)
        await query.edit_message_text("تم إلغاء إنشاء الشركة.")
        return ConversationHandler.END
    # confirm
    name = context.user_data.get("co_name")
    desc = context.user_data.get("co_desc")
    btype = context.user_data.get("co_type")
    currency = context.user_data.get("co_currency")
    telegram_user_id = query.from_user.id

    from app.database.crud.company import create_company

    db = SessionLocal()
    try:
        company = create_company(db, telegram_user_id, name, description=desc, business_type=btype, base_currency=currency)
    finally:
        db.close()

    _clear_co_keys(context)
    if company is None:
        await query.edit_message_text("تعذر إنشاء الشركة. ربما أنت عضو في شركة بالفعل أو الاسم غير صالح.")
        return ConversationHandler.END

    from bot.menus import send_main_menu

    await query.edit_message_text(f"✅ تم إنشاء شركة '{company.name}' بنجاح! أنت المالك الآن.")
    # إرسال القائمة الرئيسية كرسالة جديدة
    try:
        await send_main_menu(query.message, context, text=f"أهلاً بك في {company.name} 👋")
    except Exception:
        logger.exception("فشل إرسال القائمة بعد إنشاء الشركة")
    return ConversationHandler.END


# ---------- رمز دعوة ----------


async def invite_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _clear_co_keys(context)
    context.user_data["co_pending_invite"] = True
    await query.edit_message_text("أرسل رمز الدعوة (6 أرقام أو رابط الدعوة):\n(أرسل /cancel للإلغاء)")
    return ASK_INVITE


async def got_invite_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    # دعم رابط كامل t.me/bot?start=inv_XXXX
    token = raw
    if "inv_" in token:
        # استخراج التوكن بعد inv_
        token = token.split("inv_")[-1].split()[0].split("&")[0].strip()
    token = token.strip()
    telegram_user_id = update.effective_user.id
    display_name = update.effective_user.first_name or None

    from app.database.crud.company import redeem_invite

    db = SessionLocal()
    try:
        ok, result = redeem_invite(db, telegram_user_id, token, display_name=display_name)
    finally:
        db.close()

    _clear_co_keys(context)
    if ok:
        company = result
        await update.message.reply_text(f"✅ انضممت إلى شركة '{company.name}' بنجاح!")
        from bot.menus import send_main_menu

        try:
            await send_main_menu(update.message, context, text=f"أهلاً بك في {company.name} 👋")
        except Exception:
            logger.exception("فشل إرسال القائمة بعد الانضمام")
        # إشعار للمالك
        try:
            await update.effective_user.bot.send_message(
                chat_id=company.owner_telegram_user_id,
                text=f"👤 انضم {display_name or telegram_user_id} إلى شركتك '{company.name}'.",
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(f"❌ {result}\nحاول مجددًا أو أرسل /cancel للإلغاء.")
        return ASK_INVITE
    return ConversationHandler.END


# ---------- استخدام فردي ----------


async def solo_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _clear_co_keys(context)
    from bot.menus import send_main_menu

    await query.edit_message_text("حسنًا، ستستمر كمستخدم فردي.\nيمكنك إنشاء شركة لاحقًا من الإعدادات.")
    try:
        await send_main_menu(query.message, context)
    except Exception:
        logger.exception("فشل إرسال القائمة للفردي")
    return ConversationHandler.END


async def onboarding_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_co_keys(context)
    # تنظيف بذور إضافية
    for key in ("co_pending_invite",):
        context.user_data.pop(key, None)
    await update.message.reply_text("تم إلغاء العملية.")
    return ConversationHandler.END


onboarding_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(onboarding_entry, pattern=r"^co:new$"),
        CallbackQueryHandler(invite_entry, pattern=r"^co:join$"),
        CallbackQueryHandler(solo_entry, pattern=r"^co:solo$"),
    ],
    states={
        ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
        ASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_desc)],
        ASK_TYPE: [CallbackQueryHandler(got_type, pattern=r"^co:t:.+$")],
        ASK_CURRENCY: [CallbackQueryHandler(got_currency, pattern=r"^co:c:.+$")],
        CONFIRM: [
            CallbackQueryHandler(got_confirm, pattern=r"^co:(confirm|cancel)$"),
        ],
        ASK_INVITE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_invite_code)],
    },
    fallbacks=[CommandHandler("cancel", onboarding_cancel)],
    allow_reentry=True,
    per_message=False,
    name="onboarding_flow",
)
