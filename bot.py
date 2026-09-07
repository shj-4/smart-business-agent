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
    ContextTypes,
    filters,
)
from app.ai_service import analyze_message, transcribe_audio
from app.config import TELEGRAM_BOT_TOKEN as TOKEN, ensure_env_or_exit
from app.database.db import SessionLocal
from app.database.crud import (
    create_transaction, create_task, create_note,
    run_query, find_pending_task, complete_task, undo_last_record,
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

NULL = None

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
                chunks.append(line[i:i + limit])
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
    for i, chunk in enumerate(parts):
        await message.reply_text(chunk, **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلًا بك\nأنا مساعدك الذكي لإدارة أعمالك.\n"
        "يمكنك إرسال عملية (مثل: دفعت 300 شيكل لمحمد)\n"
        "أو سؤال (مثل: كم صرفت هذا الشهر؟)\n"
        "أو أمر: /done لإنهاء مهمة"
    )


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

    if not can_save_record(result):
        return None

    if data_type in ("expense", "income"):
        obj = create_transaction(db, telegram_user_id, result, raw_message, telegram_message_id)
        if obj is None:
            return "تم تسجيل هذه العملية مسبقًا (تكرار)."
        return "تم حفظ العملية."

    if data_type == "task":
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
        return f"تم حفظ {type_ar}."

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


async def handle_record_with_missing(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, raw_text: str | None = None):
    data_type = result.get("type")
    missing = missing_fields_for(result)

    context.user_data["pending_record"] = result
    context.user_data["pending_type"] = data_type
    context.user_data["pending_raw"] = raw_text if raw_text is not None else update.message.text
    context.user_data["pending_missing"] = missing
    context.user_data["mode"] = "collect"

    first = missing[0]
    reply = (
        f"سجّلت لك {TYPE_NAMES.get(data_type, data_type)} لكن تنقصه بيانات:\n"
        f"• {FIELD_LABELS.get(first, first)}"
    )
    if len(missing) > 1:
        reply += " (وبعدها أرجو إكمال بقية البنود)"
    reply += "\n" + ask_for_field_prompt(data_type, first)
    reply += "\n\n(لإلغاء الأمر أرسل /cancel)"
    await update.message.reply_text(reply)


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


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج النص الوحيد — يدير الحالات يدويًا عبر user_data (بلا ConversationHandler)."""
    try:
        if context.user_data.get("mode") == "collect":
            return await _collect_missing(update, context)
        return await _conversation_entry(update, context)
    except Exception:
        logger.exception("خطأ غير متوقع في معالجة الرسالة النصية")
        try:
            await update.message.reply_text("حدث خطأ أثناء معالجة رسالتك، حاول مرة أخرى.")
        except Exception:
            pass
        context.user_data.clear()
        return NULL


async def _conversation_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    telegram_user_id = update.effective_user.id
    msg_id = update.message.message_id

    if is_rate_limited(telegram_user_id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return NULL

    result = analyze_message(user_text)
    logger.info(f"نتيجة التحليل: {json.dumps(result, ensure_ascii=False)}")

    intent = result.get("intent")

    if intent == "record":
        data_type = result.get("type")

        if not data_type or data_type == "unknown" or data_type not in TYPE_NAMES:
            await update.message.reply_text(format_record_result(result, False))
            return NULL

        if data_type == "complete_task":
            db = SessionLocal()
            try:
                reply = save_record(db, telegram_user_id, result, user_text, msg_id)
            finally:
                db.close()
            await update.message.reply_text(reply or "لم أستطع فهم مهمة محددة لإنجازها.")
            return NULL

        missing = missing_fields_for(result)
        if missing:
            await handle_record_with_missing(update, context, result, user_text)
            context.user_data["pending_message_id"] = msg_id
            return NULL

        if data_type in ("expense", "income", "task", "order", "note"):
            context.user_data["confirm_result"] = result
            context.user_data["confirm_raw"] = user_text
            context.user_data["confirm_message_id"] = msg_id
            text = _build_confirm_text(result)
            kb = _build_confirm_keyboard()
            await update.message.reply_text(text, reply_markup=kb)
            return NULL

        db = SessionLocal()
        try:
            reply = save_record(db, telegram_user_id, result, user_text, msg_id)
        finally:
            db.close()
        await update.message.reply_text(reply or "لم أستطع حفظ هذه العملية، حاول توضيحها أكثر.")
        return NULL

    elif intent == "query":
        reply = handle_query_intent(result, telegram_user_id)
        await safe_reply(update.message, reply)
        return NULL

    else:
        await update.message.reply_text("أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك.")
        return NULL


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    result = context.user_data.get("confirm_result")
    raw = context.user_data.get("confirm_raw")
    msg_id = context.user_data.get("confirm_message_id")
    telegram_user_id = query.from_user.id

    if data == "confirm_yes":
        data_type = (result or {}).get("type")
        if not data_type or data_type == "unknown" or data_type not in TYPE_NAMES:
            await query.edit_message_text("لم أفهم نوع العملية بوضوح، لم يُحفظ شيء.")
            context.user_data.clear()
            return NULL
        db = SessionLocal()
        try:
            reply = save_record(db, telegram_user_id, result, raw, msg_id)
        finally:
            db.close()
        await query.edit_message_text(reply or "أعتذر، لم أستطع حفظ هذه العملية.")
    elif data == "confirm_no":
        await query.edit_message_text("تم الإلغاء. لم يُحفظ شيء.")

    context.user_data.pop("confirm_result", None)
    context.user_data.pop("confirm_raw", None)
    context.user_data.pop("confirm_message_id", None)
    context.user_data.pop("mode", None)
    return NULL


async def collect_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        return await _collect_missing(update, context)
    except Exception:
        logger.exception("خطأ غير متوقع في إكمال البيانات")
        try:
            await update.message.reply_text("حدث خطأ أثناء معالجة إكمال البيانات، حاول مرة أخرى.")
        except Exception:
            pass
        context.user_data.clear()
        return NULL


async def _collect_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    partial = context.user_data.get("pending_record")
    data_type = context.user_data.get("pending_type")
    raw = context.user_data.get("pending_raw")
    pending_msg_id = context.user_data.get("pending_message_id")

    if not partial or not data_type:
        await update.message.reply_text("أعتذر، انتهت جلسة الإكمال. أرسل العملية من جديد من فضلك.")
        context.user_data.clear()
        return NULL

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
        return NULL

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
        context.user_data.pop("mode", None)
        return NULL

    db = SessionLocal()
    try:
        reply = save_record(db, telegram_user_id, partial, raw, pending_msg_id)
    finally:
        db.close()
    context.user_data.clear()
    await update.message.reply_text(reply or "أعتذر، لم أستطع حفظ هذه العملية.")
    return NULL


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("أُلغيت العملية. يمكنك إرسال عملية جديدة متى شئت.")
    return NULL


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    # context.args يلتقط الوسائط النصية للأمر بشكل صحيح، ويعمل مع /done@BotUsername
    text = " ".join(context.args).strip() if context.args else (update.message.text or "").replace("/done", "").strip()

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


async def _handle_record_media(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, text: str, msg_id: int):
    """معالجة مشتركة لرسائل السجل القادمة من الوسائط (صوت/ملف/مقطع)."""
    data_type = result.get("type")
    telegram_user_id = update.effective_user.id

    if not data_type or data_type == "unknown" or data_type not in TYPE_NAMES:
        await update.message.reply_text(format_record_result(result, False))
        return

    if data_type == "complete_task":
        db = SessionLocal()
        try:
            reply = save_record(db, telegram_user_id, result, text, msg_id)
        finally:
            db.close()
        await update.message.reply_text(reply or "لم أستطع فهم مهمة محددة لإنجازها.")
        return

    missing = missing_fields_for(result)
    if missing:
        await handle_record_with_missing(update, context, result, text)
        context.user_data["pending_message_id"] = msg_id
        return

    context.user_data["confirm_result"] = result
    context.user_data["confirm_raw"] = text
    context.user_data["confirm_message_id"] = msg_id
    confirm_text = _build_confirm_text(result)
    kb = _build_confirm_keyboard()
    await update.message.reply_text(confirm_text, reply_markup=kb)


async def _handle_media_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, media_kind: str):
    """معالجة موحّدة لنص الوسائط (صوت/ملف/مقطع) بلا تكرار بين handlers."""
    msg_id = update.message.message_id
    if not text:
        await update.message.reply_text(
            f"لم أستطع فهم الـ{media_kind}، حاول مرة أخرى أو أرسل نصًا."
        )
        return

    result = analyze_message(text)
    intent = result.get("intent")

    if intent == "record":
        await _handle_record_media(update, context, result, text, msg_id)
    elif intent == "query":
        reply = handle_query_intent(result, update.effective_user.id)
        await safe_reply(update.message, reply)
    else:
        await update.message.reply_text("أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك.")


async def _guard_rate_limited(update: Update, telegram_user_id: int) -> bool:
    """يعيد True إذا كان المستخدم مقيّدًا (أُرسل رد التنبيه وسُحبت المعالجة)."""
    if is_rate_limited(telegram_user_id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return True
    await update.message.chat.send_action(action="typing")
    return False


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    if await _guard_rate_limited(update, telegram_user_id):
        return
    try:
        voice = await update.message.voice.get_file()
        audio_bytes = await voice.download_as_bytearray()
        text = transcribe_audio(bytes(audio_bytes), mime_type="audio/ogg")
        logger.info(f"نص صوتي (voice) من المستخدم {telegram_user_id}: {text}")
        await _handle_media_text(update, context, text, "صوت")
    except Exception:
        logger.exception("خطأ في معالجة الرسالة الصوتية")
        await update.message.reply_text("حدث خطأ أثناء معالجة الرسالة الصوتية، حاول مرة أخرى")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    if await _guard_rate_limited(update, telegram_user_id):
        return
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
        await _handle_media_text(update, context, text, "ملف صوتي")
    except Exception:
        logger.exception("خطأ في معالجة الملف الصوتي")
        await update.message.reply_text("حدث خطأ أثناء معالجة الملف الصوتي، حاول مرة أخرى")


async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    if await _guard_rate_limited(update, telegram_user_id):
        return
    try:
        video_note = update.message.video_note
        if not video_note:
            await update.message.reply_text("لم أستطع استلام المقطع المرئي، حاول مرة أخرى.")
            return

        file = await video_note.get_file()
        video_bytes = await file.download_as_bytearray()

        text = transcribe_audio(bytes(video_bytes), mime_type="video/mp4")
        logger.info(f"نص صوتي (video_note) من المستخدم {telegram_user_id}: {text}")
        await _handle_media_text(update, context, text, "مقطع مرئي")
    except Exception:
        logger.exception("خطأ في معالجة المقطع المرئي")
        await update.message.reply_text("حدث خطأ أثناء معالجة المقطع المرئي، حاول مرة أخرى")


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /undo — يحذف (soft-delete) آخر سجل أضافه المستخدم."""
    telegram_user_id = update.effective_user.id
    db = SessionLocal()
    try:
        undone = undo_last_record(db, telegram_user_id)
    finally:
        db.close()
    if undone:
        await update.message.reply_text(
            f"تم التراجع عن آخر سجل:\n{undone['kind']}: {undone['label'] or '(بدون وصف)'}"
        )
    else:
        await update.message.reply_text("لا يوجد سجلات سابقة يمكن التراجع عنها.")


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


def main():
    ensure_env_or_exit()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("undo", undo_command))

    text_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler)
    app.add_handler(text_handler)

    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_"))
    app.add_error_handler(system_error_handler)

    logger.info("البوت يعمل الآن... اضغط CTRL+C للإيقاف")
    app.run_polling()


if __name__ == "__main__":
    main()
