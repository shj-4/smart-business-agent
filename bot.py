import json
import logging
import time
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from app.ai_service import analyze_message, transcribe_audio
from app.config import TELEGRAM_BOT_TOKEN as TOKEN
from app.database.db import SessionLocal
from app.database.crud import (
    create_transaction, create_task, create_note,
    run_query, find_pending_task, complete_task,
)
from app.logging_config import configure_logging

configure_logging(service="bot")
logger = logging.getLogger(__name__)

RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60.0
_RATE_LOCK = threading.Lock()
_rate_buckets: dict = {}


def is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        window_start = now - RATE_LIMIT_WINDOW
        stamps = [t for t in _rate_buckets.get(user_id, []) if t > window_start]
        if len(stamps) >= RATE_LIMIT_MAX:
            _rate_buckets[user_id] = stamps
            return True
        stamps.append(now)
        _rate_buckets[user_id] = stamps
        return False

ASK_MISSING = 1
AWAITING_CONFIRM = 2

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
    "count_transactions": "عدد العمليات",
    "list_tasks": "قائمة المهام",
    "list_overdue_tasks": "المهام المتأخرة",
}

FIELD_LABELS = {
    "amount": "المبلغ",
    "currency": "العملة",
    "person": "الشخص",
    "description": "الوصف",
    "date": "الموعد",
}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلًا بك\nأنا مساعدك الذكي لإدار�� أعمالك.\n"
        "يمكنك إرسال عملية (مثل: دفعت 300 شيكل لمحمد)\n"
        "أو سؤال (مثل: كم صرفت هذا الشهر؟)\n"
        "أو أمر: /done لإنهاء مهمة"
    )


def format_record_result(data: dict, saved: bool) -> str:
    if data.get("type") == "unknown":
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
                lines.append(f"{i}. {task.get('description') or '(بدون وصف)'} (الموعد: {due})")
            return "\n".join(lines)
        lines = [f"{METRIC_NAMES.get(metric, metric)}:\n"]
        for item in result:
            status = "مكتمل" if item.get("status") == "done" else "قيد الانتظار"
            desc = item.get("description") or "(بدون وصف)"
            person_txt = f" - {item['person']}" if item.get("person") else ""
            due = f" (موعد: {item['due_date']})" if item.get("due_date") else ""
            lines.append(f"• {desc}{person_txt}{due} [{status}]")
        return "\n".join(lines)

    metric_label = METRIC_NAMES.get(metric, metric)
    period_label = PERIOD_NAMES.get(period, period)

    line = f"{metric_label} {period_label}"
    if person:
        line += f" (خاص بـ {person})"
    if isinstance(result, dict):
        if not result:
            line += ": 0"
        else:
            currency_lines = [f"  {cur}: {amount}" for cur, amount in result.items()]
            line += ":\n" + "\n".join(currency_lines)
    else:
        line += f": {result}"
    return line


def handle_query_intent(result: dict, telegram_user_id: int) -> str:
    db = SessionLocal()
    try:
        query_result = run_query(db, telegram_user_id, result.get("query_details") or {})
    finally:
        db.close()
    return format_query_result(query_result)


def can_save_record(result: dict) -> bool:
    data_type = result.get("type")
    if data_type in ("expense", "income"):
        return result.get("amount") is not None
    if data_type == "task":
        return bool(result.get("description") or result.get("person"))
    if data_type == "complete_task":
        return bool(result.get("description"))
    if data_type in ("order", "note"):
        return bool(result.get("description") or result.get("person"))
    return False


def _do_save(db, telegram_user_id, result, raw_message, telegram_message_id=None):
    data_type = result.get("type")

    if data_type in ("expense", "income") and result.get("amount") is not None:
        obj = create_transaction(db, telegram_user_id, result, raw_message, telegram_message_id)
        if obj is None:
            return "تم تسجيل هذه العملية مسبقًا (تكرار)."
        return "تم حفظ العملية."

    if data_type == "task" and (result.get("description") or result.get("person")):
        obj = create_task(db, telegram_user_id, result, raw_message, telegram_message_id)
        if obj is None:
            return "تم تسجيل هذه المهمة مسبقًا (تكرار)."
        return "تم حفظ المهمة."

    if data_type == "complete_task":
        description_hint = result.get("description")
        if description_hint:
            task = find_pending_task(db, telegram_user_id, description_hint)
            if task:
                complete_task(db, telegram_user_id, task.id)
                return f"تم إنجاز المهمة: {task.description}"
            return "لم أجد مهمة مطابقة ضمن مهامك المعلّقة."
        return "حدد المهمة التي تريد إنجازها (مثلاً: أنجزت مهمة الاتصال بسامر)"

    if data_type in ("order", "note"):
        obj = create_note(db, telegram_user_id, result, raw_message, telegram_message_id)
        if obj is None:
            return "تم تسجيل هذا السجل مسبقًا (تكرار)."
        type_ar = "الطلبية" if data_type == "order" else "الملاحظة"
        return f"تم حفظ{type_ar}."

    return None


def save_record(db, telegram_user_id, result, raw_message, telegram_message_id=None):
    return _do_save(db, telegram_user_id, result, raw_message, telegram_message_id)


def missing_fields_for(result: dict) -> list:
    data_type = result.get("type")
    missing = []

    if data_type in ("expense", "income"):
        if result.get("amount") is None:
            missing.append("amount")
    elif data_type == "task":
        if not result.get("description") and not result.get("person"):
            missing.append("description")
    elif data_type == "complete_task":
        if not result.get("description"):
            missing.append("description")

    return missing


def ask_for_field_prompt(data_type: str, field: str) -> str:
    label = FIELD_LABELS.get(field, field)
    type_label = TYPE_NAMES.get(data_type, data_type)
    if field == "amount":
        return f"عملية {type_label}: ما هو المبلغ؟ (مثال: 300 أو 300 شيكل)"
    if field == "currency":
        return f"ما هي العملة؟ (مثال: شيكل، دولار، دينار)"
    if field == "person":
        return f"ما اسم الشخص (المورد/العميل)؟ (مثال: محمد)"
    if field == "date":
        return f"ما هو الموعد؟ (مثال: غدًا الساعة 10)"
    return f"أرسل {label} من فضلك."


def handle_record_with_missing(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict):
    data_type = result.get("type")
    missing = missing_fields_for(result)

    context.user_data["pending_record"] = result
    context.user_data["pending_type"] = data_type
    context.user_data["pending_raw"] = update.message.text
    context.user_data["pending_missing"] = missing

    first = missing[0]
    reply = (
        f"سجّلت لك {TYPE_NAMES.get(data_type, data_type)} لكن تنقصه بيانات:\n"
        f"• {FIELD_LABELS.get(first, first)}"
    )
    if len(missing) > 1:
        reply += " (وبعدها أرجو إكمال بقية البنود)"
    reply += "\n" + ask_for_field_prompt(data_type, first)
    reply += "\n\n(لإلغاء الأمر أرسل /cancel)"
    update.message.reply_text(reply)


def fill_field_from_reply(partial: dict, reply_result: dict, field: str):
    if field == "amount":
        if reply_result.get("amount") is not None:
            partial["amount"] = reply_result["amount"]
        if reply_result.get("currency") and not partial.get("currency"):
            partial["currency"] = reply_result["currency"]
    elif field == "currency":
        if reply_result.get("currency"):
            partial["currency"] = reply_result["currency"]
    elif field == "person":
        if reply_result.get("person"):
            partial["person"] = reply_result["person"]
    elif field == "description":
        if reply_result.get("description"):
            partial["description"] = reply_result["description"]
        elif reply_result.get("person"):
            partial["person"] = reply_result["person"]
    elif field == "date":
        if reply_result.get("date"):
            partial["date"] = reply_result["date"]


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
    if data.get("description"):
        lines.append(f"الوصف: {data['description']}")
    if data.get("date"):
        lines.append(f"الموعد: {data['date']}")
    return "\n".join(lines)


def _build_confirm_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تأكيد", callback_data="confirm_yes"),
            InlineKeyboardButton("❌ إلغاء", callback_data="confirm_no"),
        ]
    ])


async def conversation_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    telegram_user_id = update.effective_user.id
    msg_id = update.message.message_id

    if is_rate_limited(telegram_user_id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return ConversationHandler.END

    result = analyze_message(user_text)
    logger.info(f"نتيجة التحليل: {json.dumps(result, ensure_ascii=False)}")

    intent = result.get("intent")

    if intent == "record":
        data_type = result.get("type")

        if data_type in ("complete_task",):
            db = SessionLocal()
            try:
                reply = save_record(db, telegram_user_id, result, user_text, msg_id)
            finally:
                db.close()
            await update.message.reply_text(reply or "تم الحفظ.")
            return ConversationHandler.END

        missing = missing_fields_for(result)
        if missing:
            handle_record_with_missing(update, context, result)
            context.user_data["pending_message_id"] = msg_id
            return ASK_MISSING

        if data_type in ("expense", "income", "task", "order", "note"):
            context.user_data["confirm_result"] = result
            context.user_data["confirm_raw"] = user_text
            context.user_data["confirm_message_id"] = msg_id
            text = _build_confirm_text(result)
            kb = _build_confirm_keyboard()
            await update.message.reply_text(text, reply_markup=kb)
            return AWAITING_CONFIRM

        db = SessionLocal()
        try:
            reply = save_record(db, telegram_user_id, result, user_text, msg_id) or "تم الحفظ."
        finally:
            db.close()
        await update.message.reply_text(reply)
        return ConversationHandler.END

    elif intent == "query":
        reply = handle_query_intent(result, telegram_user_id)
        await update.message.reply_text(reply)
        return ConversationHandler.END

    else:
        await update.message.reply_text("أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك.")
        return ConversationHandler.END


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    result = context.user_data.get("confirm_result")
    raw = context.user_data.get("confirm_raw")
    msg_id = context.user_data.get("confirm_message_id")
    telegram_user_id = query.from_user.id

    if data == "confirm_yes":
        db = SessionLocal()
        try:
            reply = save_record(db, telegram_user_id, result, raw, msg_id) or "تم الحفظ."
        finally:
            db.close()
        await query.edit_message_text(reply)
    elif data == "confirm_no":
        await query.edit_message_text("تم الإلغاء. لم يُحفظ شيء.")

    context.user_data.pop("confirm_result", None)
    context.user_data.pop("confirm_raw", None)
    context.user_data.pop("confirm_message_id", None)
    return ConversationHandler.END


async def collect_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    partial = context.user_data.get("pending_record")
    data_type = context.user_data.get("pending_type")
    raw = context.user_data.get("pending_raw")
    pending_msg_id = context.user_data.get("pending_message_id")

    if not partial or not data_type:
        await update.message.reply_text("أعتذر، انتهت جلسة الإكمال. أرسل العملية من جديد من فضلك.")
        context.user_data.clear()
        return ConversationHandler.END

    reply_result = analyze_message(update.message.text)

    missing = context.user_data.get("pending_missing") or missing_fields_for(partial)
    field = missing[0]
    fill_field_from_reply(partial, reply_result, field)

    remaining = missing_fields_for(partial)
    context.user_data["pending_missing"] = remaining

    if remaining:
        first = remaining[0]
        await update.message.reply_text(
            "لا يزال يلزم:\n"
            f"• {FIELD_LABELS.get(first, first)}\n"
            + ask_for_field_prompt(data_type, first)
        )
        return ASK_MISSING

    context.user_data.pop("pending_missing", None)
    context.user_data.pop("pending_type", None)
    context.user_data.pop("pending_raw", None)

    if data_type in ("expense", "income", "task", "order", "note"):
        context.user_data["confirm_result"] = partial
        context.user_data["confirm_raw"] = raw
        context.user_data["confirm_message_id"] = pending_msg_id
        text = _build_confirm_text(partial)
        kb = _build_confirm_keyboard()
        await update.message.reply_text(text, reply_markup=kb)
        context.user_data.pop("pending_record", None)
        return AWAITING_CONFIRM

    db = SessionLocal()
    try:
        reply = save_record(db, telegram_user_id, partial, raw, pending_msg_id) or "تم الحفظ."
    finally:
        db.close()
    context.user_data.clear()
    await update.message.reply_text(reply)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("أُلغيت العملية. يمكنك إرسال عملية جديدة متى شئت.")
    return ConversationHandler.END


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    text = (update.message.text or "").replace("/done", "").strip()

    if not text:
        await update.message.reply_text(
            "استخدم: /done وصف المهمة\nمثال: /done الاتصال بسامر"
        )
        return

    db = SessionLocal()
    try:
        task = find_pending_task(db, telegram_user_id, text)
        if task:
            complete_task(db, telegram_user_id, task.id)
            await update.message.reply_text(f"تم إنجاز المهمة: {task.description}")
        else:
            await update.message.reply_text("لم أجد مهمة مطابقة ضمن مهامك المعلّقة.")
    finally:
        db.close()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    msg_id = update.message.message_id

    if is_rate_limited(telegram_user_id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return

    await update.message.chat.send_action(action="typing")

    try:
        voice = await update.message.voice.get_file()
        audio_bytes = await voice.download_as_bytearray()
        text = transcribe_audio(bytes(audio_bytes), mime_type="audio/ogg")
        logger.info(f"نص صوتي (voice) من المستخدم {telegram_user_id}: {text}")

        if not text:
            await update.message.reply_text("لم أستطع فهم الصوت، حاول مرة أخرى أو أرسل نصًا.")
            return

        result = analyze_message(text)
        intent = result.get("intent")

        if intent == "record":
            data_type = result.get("type")
            if data_type in ("expense", "income", "task", "order", "note"):
                context.user_data["confirm_result"] = result
                context.user_data["confirm_raw"] = text
                context.user_data["confirm_message_id"] = msg_id
                confirm_text = _build_confirm_text(result)
                kb = _build_confirm_keyboard()
                await update.message.reply_text(confirm_text, reply_markup=kb)
                return AWAITING_CONFIRM
            db = SessionLocal()
            try:
                reply = save_record(db, telegram_user_id, result, text, msg_id) or "تم الحفظ."
            finally:
                db.close()
        elif intent == "query":
            reply = handle_query_intent(result, telegram_user_id)
        else:
            reply = "أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك."
        await update.message.reply_text(reply)
    except Exception:
        logger.exception("خطأ في معالجة الرسالة الصوتية")
        await update.message.reply_text("حدث خطأ أثناء معالجة الرسالة الصوتية، حاول مرة أخرى")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    msg_id = update.message.message_id

    if is_rate_limited(telegram_user_id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return

    await update.message.chat.send_action(action="typing")

    try:
        audio = update.message.audio
        if not audio:
            await update.message.reply_text("لم أستطع استلام الملف الصوتي، حاول مرة أخرى.")
            return

        file = await audio.get_file()
        audio_bytes = await file.download_as_bytearray()

        if audio.mime_type:
            mime = audio.mime_type
        elif audio.file_name and audio.file_name.endswith(".ogg"):
            mime = "audio/ogg"
        elif audio.file_name and audio.file_name.endswith(".mp3"):
            mime = "audio/mpeg"
        elif audio.file_name and audio.file_name.endswith(".m4a"):
            mime = "audio/mp4"
        else:
            mime = "audio/ogg"

        text = transcribe_audio(bytes(audio_bytes), mime_type=mime)
        logger.info(f"نص صوتي (audio) من المستخدم {telegram_user_id}: {text}")

        if not text:
            await update.message.reply_text("لم أستطع فهم الملف الصوتي، حاول مرة أخرى أو أرسل نصًا.")
            return

        result = analyze_message(text)
        intent = result.get("intent")

        if intent == "record":
            data_type = result.get("type")
            if data_type in ("expense", "income", "task", "order", "note"):
                context.user_data["confirm_result"] = result
                context.user_data["confirm_raw"] = text
                context.user_data["confirm_message_id"] = msg_id
                confirm_text = _build_confirm_text(result)
                kb = _build_confirm_keyboard()
                await update.message.reply_text(confirm_text, reply_markup=kb)
                return AWAITING_CONFIRM
            db = SessionLocal()
            try:
                reply = save_record(db, telegram_user_id, result, text, msg_id) or "تم الحفظ."
            finally:
                db.close()
        elif intent == "query":
            reply = handle_query_intent(result, telegram_user_id)
        else:
            reply = "أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك."
        await update.message.reply_text(reply)
    except Exception:
        logger.exception("خطأ في معالجة الملف الصوتي")
        await update.message.reply_text("حدث خطأ أثناء معالجة الملف الصوتي، حاول مرة أخرى")


async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    msg_id = update.message.message_id

    if is_rate_limited(telegram_user_id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return

    await update.message.chat.send_action(action="typing")

    try:
        video_note = update.message.video_note
        if not video_note:
            await update.message.reply_text("لم أستطع استلام المقطع المرئي، حاول مرة أخرى.")
            return

        file = await video_note.get_file()
        video_bytes = await file.download_as_bytearray()

        text = transcribe_audio(bytes(video_bytes), mime_type="video/mp4")
        logger.info(f"نص صوتي (video_note) من المستخدم {telegram_user_id}: {text}")

        if not text:
            await update.message.reply_text("لم أستطع فهم المقطع المرئي، حاول مرة أخرى أو أرسل نصًا.")
            return

        result = analyze_message(text)
        intent = result.get("intent")

        if intent == "record":
            data_type = result.get("type")
            if data_type in ("expense", "income", "task", "order", "note"):
                context.user_data["confirm_result"] = result
                context.user_data["confirm_raw"] = text
                context.user_data["confirm_message_id"] = msg_id
                confirm_text = _build_confirm_text(result)
                kb = _build_confirm_keyboard()
                await update.message.reply_text(confirm_text, reply_markup=kb)
                return AWAITING_CONFIRM
            db = SessionLocal()
            try:
                reply = save_record(db, telegram_user_id, result, text, msg_id) or "تم الحفظ."
            finally:
                db.close()
        elif intent == "query":
            reply = handle_query_intent(result, telegram_user_id)
        else:
            reply = "أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك."
        await update.message.reply_text(reply)
    except Exception:
        logger.exception("خطأ في معالجة المقطع المرئي")
        await update.message.reply_text("حدث خطأ أثناء معالجة المقطع المرئي، حاول مرة أخرى")


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("done", done_command))

    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, conversation_entry)],
            states={
                ASK_MISSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_missing)],
                AWAITING_CONFIRM: [CallbackQueryHandler(confirm_callback, pattern="^confirm_")],
            },
            fallbacks=[
                CommandHandler("cancel", cancel),
                MessageHandler(filters.Regex("^/cancel$"), cancel),
            ],
            allow_reentry=True,
        )
    )

    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))

    logger.info("البوت يعمل الآن... اضغط CTRL+C للإيقاف")
    app.run_polling()


if __name__ == "__main__":
    main()
