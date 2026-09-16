"""
 معالج أمر /edit لتعديل السجلات الموجودة.

التسلسل:
1. المستخدم يرسل /edit → تظهر آخر 10 سجلات كأزرار InlineKeyboard.
2. المستخدم يضغط زر → يظهر الحقول الحالية مع خيارات التعديل.
3. المستخدم يرسل قيمة جديدة → يُحدّث ويُرجّع القائمة.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.database.crud import (
    get_record_by_id,
    list_recent_records,
    update_note,
    update_task,
    update_transaction,
)
from app.database.db import SessionLocal
from app.database.models import Note, Task, Transaction
from app.normalize import normalize_priority
from bot.icons import EXPENSE, INCOME, TASK

logger = logging.getLogger(__name__)

# حالات محادثة التعديل
EDIT_SELECT = 10  # اختيار السجل من القائمة
EDIT_FIELD = 11  # إدخال القيمة الجديدة

# الحقول القابلة للتعديل لكل نوع
EDITABLE_FIELDS = {
    "Transaction": ["amount", "currency", "person", "category", "description"],
    "Task": ["description", "person", "due_date", "priority"],
    "Note": ["description", "person", "category"],
}

FIELD_LABELS_AR = {
    "amount": "المبلغ",
    "currency": "العملة",
    "person": "الشخص",
    "category": "التصنيف",
    "description": "الوصف",
    "due_date": "الموعد",
    "priority": "الأولوية",
}

PRIORITY_LABELS_AR = {"high": "عالية ⚡", "normal": "عادية", "low": "منخفضة"}


# مرادفات حقول للمستخدم في نمط /fix
FIX_FIELD_SYNONYMS = {
    "المبلغ": "amount",
    "العملة": "currency",
    "الشخص": "person",
    "التصنيف": "category",
    "الوصف": "description",
    "الموعد": "due_date",
    "الأولوية": "priority",
    "أولوية": "priority",
    "الاولوية": "priority",
}


def _build_records_keyboard(records: list[dict]) -> InlineKeyboardMarkup:
    """يُنشئ أزرار InlineKeyboard لعرض آخر السجلات."""
    buttons = []
    for i, rec in enumerate(records):
        kind_emoji = {
            "expense": EXPENSE,
            "income": INCOME,
            "task": TASK,
            "order": "🛒",
            "note": "📝",
        }.get(rec["kind"], "📄")
        label = rec["label"][:40]
        text = f"{kind_emoji} {i + 1}. {label}"
        cb_data = f"edit:{rec['model']}:{rec['id']}"
        buttons.append([InlineKeyboardButton(text, callback_data=cb_data)])

    return InlineKeyboardMarkup(buttons)


def _build_fields_keyboard(record: Task | Transaction | Note) -> InlineKeyboardMarkup:
    """يُنشئ أزرار InlineKeyboard لاختيار الحقول القابلة للتعديل."""
    model_name = type(record).__name__
    fields = EDITABLE_FIELDS.get(model_name, [])
    buttons = []
    for f in fields:
        label = FIELD_LABELS_AR.get(f, f)
        buttons.append([InlineKeyboardButton(label, callback_data=f"editfield:{f}")])
    buttons.append([InlineKeyboardButton("↩ العودة للقائمة", callback_data="edit_back")])
    return InlineKeyboardMarkup(buttons)


def _get_current_value(record: Task | Transaction | Note, field: str) -> str:
    """يجلب القيمة الحالية للحقل."""
    val = getattr(record, field, None)
    if val is None:
        return "(فارغ)"
    if field == "amount":
        return f"{val} {record.currency or ''}"
    if field == "priority":
        return PRIORITY_LABELS_AR.get(val, str(val))
    return str(val)


def _format_record_info(record: Task | Transaction | Note) -> str:
    """يعرض معلومات السجل الحالي مع الحقول القابلة للتعديل."""
    model_name = type(record).__name__
    lines = [f"**{model_name}** — اختر حقلًا للتعديل:\n"]

    if model_name == "Transaction":
        lines.append(f"المبلغ: {_get_current_value(record, 'amount')}")
        lines.append(f"العملة: {_get_current_value(record, 'currency')}")
        lines.append(f"الشخص: {_get_current_value(record, 'person')}")
        lines.append(f"التصنيف: {_get_current_value(record, 'category')}")
        lines.append(f"الوصف: {_get_current_value(record, 'description')}")
    elif model_name == "Task":
        lines.append(f"الوصف: {_get_current_value(record, 'description')}")
        lines.append(f"الشخص: {_get_current_value(record, 'person')}")
        lines.append(f"الموعد: {_get_current_value(record, 'due_date')}")
        lines.append(f"الأولوية: {_get_current_value(record, 'priority')}")
        if getattr(record, "recurrence_rule", None):
            recur = {"daily": "يومية", "weekly": "أسبوعية", "monthly": "شهرية"}.get(
                record.recurrence_rule, record.recurrence_rule
            )
            lines.append(f"التكرار: {recur}")
    elif model_name == "Note":
        lines.append(f"الوصف: {_get_current_value(record, 'description')}")
        lines.append(f"الشخص: {_get_current_value(record, 'person')}")
        lines.append(f"التصنيف: {_get_current_value(record, 'category')}")

    lines.append("\nاضغط على الحقل الذي تريد تعديله:")
    return "\n".join(lines)


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """أمر /edit — يعرض آخر 10 سجلات كأزرار."""
    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        records = list_recent_records(db, telegram_user_id, limit=10)
    finally:
        db.close()

    if not records:
        await update.message.reply_text("لا توجد سجلات لتعديلها.")
        return ConversationHandler.END

    context.user_data["edit_records"] = records
    kb = _build_records_keyboard(records)
    await update.message.reply_text("اختر السجل الذي تريد تعديله:", reply_markup=kb)
    return EDIT_SELECT


async def fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """أمر /fix — تعديل سريع لآخر سجل.

    - /fix → يعرض حقول آخر سجل للتعديل (كما /edit بدون اختيار).
    - /fix المبلغ 600 → يعدّل المبلغ مباشرة.
    - /fix الشخص محمد → يعدّل الشخص مباشرة.
    """
    telegram_user_id = update.effective_user.id
    args = context.args or []

    db = SessionLocal()
    try:
        records = list_recent_records(db, telegram_user_id, limit=1)
        if not records:
            await update.message.reply_text("لا يوجد سجلات لتعديلها. أرسل سجلًا جديدًا أولًا.")
            return ConversationHandler.END
        rec = records[0]
        model_name, record_id = rec["model"], rec["id"]
        record, _ = get_record_by_id(db, telegram_user_id, model_name, record_id)
    finally:
        db.close()

    if record is None:
        await update.message.reply_text("تعذر العثور على آخر سجل.")
        return ConversationHandler.END

    # نمط مباشر: /fix <حقل> <قيمة>
    field_key = args[0] if args else ""
    field = FIX_FIELD_SYNONYMS.get(field_key)
    if field and len(args) >= 2:
        new_value = " ".join(args[1:]).strip()
        if new_value:
            valid_fields = EDITABLE_FIELDS.get(model_name, [])
            if field not in valid_fields:
                await update.message.reply_text(
                    f"هذا الحقل غير قابل للتعديل.\nالحقول المتاحة: {', '.join(FIELD_LABELS_AR.get(f, f) for f in valid_fields)}"
                )
                return ConversationHandler.END
            db = SessionLocal()
            try:
                from app.database.crud import can_manage_records

                if not can_manage_records(db, telegram_user_id):
                    await update.message.reply_text(
                        "أعضاء المساحة المشتركة لا يعدّلون السجلات — المرتكز (المالك) فقط."
                    )
                    return ConversationHandler.END
                fields = {field: new_value}
                if field == "priority":
                    fields["priority"] = normalize_priority(new_value)
                if model_name == "Transaction":
                    update_transaction(db, record, fields)
                elif model_name == "Task":
                    update_task(db, record, fields)
                elif model_name == "Note":
                    update_note(db, record, fields)
            finally:
                db.close()
            label = FIELD_LABELS_AR.get(field, field)
            await update.message.reply_text(
                f"تم تحديث {label} لآخر سجل بنجاح: {new_value}\nالسجل: {rec['label'][:80]}"
            )
            return ConversationHandler.END

    # وإلا اعرض حقول آخر سجل للاختيار
    context.user_data["edit_record"] = record
    context.user_data["edit_model"] = model_name
    context.user_data["edit_id"] = rec["id"]

    info_text = _format_record_info(record)
    kb = _build_fields_keyboard(record)
    await update.message.reply_text(info_text, reply_markup=kb, parse_mode="Markdown")
    return EDIT_FIELD


async def edit_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """معالجة ضغط الزر على سجل محدد."""
    query = update.callback_query
    await query.answer()

    cb_data = query.data  # edit:Model:ID
    parts = cb_data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("خطأ في البيانات، حاول مجددًا.")
        return ConversationHandler.END

    _, model_name, record_id_str = parts
    try:
        record_id = int(record_id_str)
    except ValueError:
        await query.edit_message_text("خطأ في البيانات، حاول مجددًا.")
        return ConversationHandler.END

    telegram_user_id = query.from_user.id
    db = SessionLocal()
    try:
        record, _ = get_record_by_id(db, telegram_user_id, model_name, record_id)
    finally:
        db.close()

    if record is None:
        await query.edit_message_text("لم أجد هذا السجل. ربما حُذف.")
        return ConversationHandler.END

    context.user_data["edit_record"] = record
    context.user_data["edit_model"] = model_name
    context.user_data["edit_id"] = record_id

    info_text = _format_record_info(record)
    kb = _build_fields_keyboard(record)
    await query.edit_message_text(info_text, reply_markup=kb, parse_mode="Markdown")
    return EDIT_FIELD


async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """معالجة ضغط الزر على حقل محدد."""
    query = update.callback_query
    await query.answer()

    if query.data == "edit_back":
        records = context.user_data.get("edit_records", [])
        if not records:
            await query.edit_message_text("انتهت الجلسة. أرسل /edit للبدء مجددًا.")
            return ConversationHandler.END
        kb = _build_records_keyboard(records)
        await query.edit_message_text("اختر السجل الذي تريد تعديله:", reply_markup=kb)
        return EDIT_SELECT

    field = query.data.replace("editfield:", "")
    record = context.user_data.get("edit_record")
    if not record:
        await query.edit_message_text("انتهت الجلسة. أرسل /edit للبدء مجددًا.")
        return ConversationHandler.END

    current = _get_current_value(record, field)
    label = FIELD_LABELS_AR.get(field, field)
    context.user_data["edit_field"] = field

    await query.edit_message_text(
        f"أرسل القيمة الجديدة للحقل **{label}**:\n"
        f"القيمة الحالية: {current}\n\n"
        f"(أرسل /cancel لإلغاء)",
        parse_mode="Markdown",
    )
    return EDIT_FIELD


async def edit_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """يستقبل القيمة الجديدة ويُحدّث السجل."""
    field = context.user_data.get("edit_field")
    model_name = context.user_data.get("edit_model")
    record_id = context.user_data.get("edit_id")
    telegram_user_id = update.effective_user.id

    if not field or not model_name or not record_id:
        await update.message.reply_text("انتهت الجلسة. أرسل /edit للبدء مجددًا.")
        return ConversationHandler.END

    new_value = update.message.text.strip()
    if not new_value:
        await update.message.reply_text("القيمة فارغة. أرسل قيمة صالحة أو /cancel للإلغاء.")
        return EDIT_FIELD

    db = SessionLocal()
    try:
        record, _ = get_record_by_id(db, telegram_user_id, model_name, record_id)
        if record is None:
            await update.message.reply_text("لم أجد هذا السجل.")
            return ConversationHandler.END

        from app.database.crud import can_manage_records

        if not can_manage_records(db, telegram_user_id):
            await update.message.reply_text(
                "أعضاء المساحة المشتركة لا يعدّلون السجلات — المرتكز (المالك) فقط."
            )
            return ConversationHandler.END

        fields = {field: new_value}
        if model_name == "Transaction":
            update_transaction(db, record, fields)
        elif model_name == "Task":
            if field == "priority":
                fields["priority"] = normalize_priority(new_value)
            update_task(db, record, fields)
        elif model_name == "Note":
            update_note(db, record, fields)

        label = FIELD_LABELS_AR.get(field, field)
        await update.message.reply_text(f"تم تحديث {label} بنجاح.\nأرسل /edit لعرض السجلات مجددًا.")
    finally:
        db.close()

    context.user_data.pop("edit_record", None)
    context.user_data.pop("edit_model", None)
    context.user_data.pop("edit_id", None)
    context.user_data.pop("edit_field", None)
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """إلغاء محادثة التعديل."""
    for key in ("edit_record", "edit_model", "edit_id", "edit_field", "edit_records"):
        context.user_data.pop(key, None)
    await update.message.reply_text("تم إلغاء التعديل.")
    return ConversationHandler.END


# ---------- تعريف محادثة التعديل ----------

edit_conversation = ConversationHandler(
    entry_points=[
        CommandHandler("edit", edit_command),
        CommandHandler("fix", fix_command),
    ],
    states={
        EDIT_SELECT: [
            CallbackQueryHandler(edit_select_callback, pattern=r"^edit:.+:\d+$"),
        ],
        EDIT_FIELD: [
            CallbackQueryHandler(edit_field_callback, pattern=r"^editfield:.+$|^edit_back$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_value),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", edit_cancel),
    ],
    allow_reentry=True,
    per_message=False,
    name="edit_flow",
)
