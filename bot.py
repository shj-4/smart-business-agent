import json
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from app.ai_service import analyze_message, transcribe_audio
from app.config import TELEGRAM_BOT_TOKEN as TOKEN
from app.database.db import SessionLocal
from app.database.crud import create_transaction, create_task, run_query, find_pending_task, complete_task

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# حالة المحادثة: بانتظار إكمال بيانات ناقصة
ASK_MISSING = 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلًا بك\nأنا مساعدك الذكي لإدارة أعمالك.\n"
        "يمكنك إرسال عملية (مثل: دفعت 300 شيكل لمحمد)\n"
        "أو سؤال (مثل: كم صرفت هذا الشهر؟)"
    )

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

# تسمية الحقول بلغة مفهومة عند المطالبة بإكمالها
FIELD_LABELS = {
    "amount": "المبلغ",
    "currency": "العملة",
    "person": "الشخص",
    "description": "الوصف",
    "date": "الموعد",
}


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

    # نتائج قائمة (مثل المهام)
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
    """هل يحتوي السجل على الحد الأدنى من البيانات للحفظ؟"""
    data_type = result.get("type")
    if data_type in ("expense", "income"):
        return result.get("amount") is not None
    if data_type == "task":
        return bool(result.get("description") or result.get("person"))
    if data_type == "complete_task":
        return bool(result.get("description"))
    return False


def save_record(db, telegram_user_id, result, raw_message) -> str | None:
    """يحفظ السجل ويُرجع رسالة تأكيد، أو None إذا لم يكن قابلاً للحفظ."""
    data_type = result.get("type")

    if data_type in ("expense", "income") and result.get("amount") is not None:
        create_transaction(db, telegram_user_id, result, raw_message)
        return "تم حفظ العملية."

    if data_type == "task" and (result.get("description") or result.get("person")):
        create_task(db, telegram_user_id, result, raw_message)
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

    return None


def missing_fields_for(result: dict) -> list:
    """يرجع الحقول الإجبارية المتبقية (بالترتيب) اللازمة لإتمام الحفظ."""
    data_type = result.get("type")
    missing = []

    if data_type in ("expense", "income"):
        if result.get("amount") is None:
            missing.append("amount")

    elif data_type == "task":
        # مهمة تحتاج وصفًا أو شخصًا على الأقل
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
    """يبدأ محادثة لإكمال الحقول الناقصة بدلًا من إضاعة السجل."""
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
    """يملأ الحقل الناقص بالقيمة المستخرجة من ردّ المستخدم."""
    if field == "amount":
        if reply_result.get("amount") is not None:
            partial["amount"] = reply_result["amount"]
        # إن ذكر المستخدم عملة ضمن ردّه نملأها أيضًا
        if reply_result.get("currency") and not partial.get("currency"):
            partial["currency"] = reply_result["currency"]
    elif field == "currency":
        if reply_result.get("currency"):
            partial["currency"] = reply_result["currency"]
    elif field == "person":
        if reply_result.get("person"):
            partial["person"] = reply_result["person"]
    elif field == "description":
        # للمهام: نقبل الوصف أو الشخص كمُحدِّد كافٍ للحفظ
        if reply_result.get("description"):
            partial["description"] = reply_result["description"]
        elif reply_result.get("person"):
            partial["person"] = reply_result["person"]
    elif field == "date":
        if reply_result.get("date"):
            partial["date"] = reply_result["date"]


async def conversation_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نقطة الدخول: تحلل الرسالة وتقرر بدء محادثة إكمال أو إنهاء فورًا."""
    user_text = update.message.text
    telegram_user_id = update.effective_user.id
    result = analyze_message(user_text)
    logger.info(f"نتيجة التحليل: {json.dumps(result, ensure_ascii=False)}")

    intent = result.get("intent")

    if intent == "record":
        data_type = result.get("type")

        if data_type in ("complete_task",):
            db = SessionLocal()
            try:
                reply = save_record(db, telegram_user_id, result, user_text)
            finally:
                db.close()
            update.message.reply_text(reply)
            return ConversationHandler.END

        missing = missing_fields_for(result)
        if missing:
            handle_record_with_missing(update, context, result)
            return ASK_MISSING

        db = SessionLocal()
        try:
            reply = save_record(db, telegram_user_id, result, user_text) or "تم الحفظ."
        finally:
            db.close()
        update.message.reply_text(reply)
        return ConversationHandler.END

    elif intent == "query":
        reply = handle_query_intent(result, telegram_user_id)
        update.message.reply_text(reply)
        return ConversationHandler.END

    else:
        update.message.reply_text("أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك.")
        return ConversationHandler.END


async def collect_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستلم ردّ المستخدم لإكمال الحقل الناقص."""
    telegram_user_id = update.effective_user.id
    partial = context.user_data.get("pending_record")
    data_type = context.user_data.get("pending_type")
    raw = context.user_data.get("pending_raw")

    if not partial or not data_type:
        update.message.reply_text("أعتذر، انتهت جلسة الإكمال. أرسل العملية من جديد من فضلك.")
        context.user_data.clear()
        return ConversationHandler.END

    # تحليل ردّ المستخدم لاستخراج الحقل الناقص
    reply_result = analyze_message(update.message.text)

    missing = context.user_data.get("pending_missing") or missing_fields_for(partial)
    # نملأ الحقل الحالي (والأكثر أهمية أولًا)
    field = missing[0]
    fill_field_from_reply(partial, reply_result, field)

    # نعيد حساب المتبقي
    remaining = missing_fields_for(partial)
    context.user_data["pending_missing"] = remaining

    if remaining:
        first = remaining[0]
        update.message.reply_text(
            "لا يزال يلزم:\n"
            f"• {FIELD_LABELS.get(first, first)}\n"
            + ask_for_field_prompt(data_type, first)
        )
        return ASK_MISSING

    # اكتملت البيانات — نحفظ
    db = SessionLocal()
    try:
        reply = save_record(db, telegram_user_id, partial, raw) or "تم الحفظ."
    finally:
        db.close()
    context.user_data.clear()
    update.message.reply_text(reply)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("أُلغيت العملية. يمكنك إرسال عملية جديدة متى شئت.")
    return ConversationHandler.END


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_user_id = update.effective_user.id
    await update.message.chat.send_action(action="typing")

    try:
        voice = await update.message.voice.get_file()
        audio_bytes = await voice.download_as_bytearray()
        text = transcribe_audio(bytes(audio_bytes), mime_type="audio/ogg")
        logger.info(f"نص صوتي من المستخدم {telegram_user_id}: {text}")

        if not text:
            await update.message.reply_text("لم أستطع فهم الصوت، حاول مرة أخرى أو أرسل نصًا.")
            return

        result = analyze_message(text)
        intent = result.get("intent")

        if intent == "record":
            db = SessionLocal()
            try:
                reply = save_record(db, telegram_user_id, result, text) or "تم الحفظ."
            finally:
                db.close()
        elif intent == "query":
            reply = handle_query_intent(result, telegram_user_id)
        else:
            reply = "أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك."
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"خطأ في معالجة الصوت: {e}")
        reply = "حدث خطأ أثناء معالجة الرسالة الصوتية، حاول مرة أخرى"
        await update.message.reply_text(reply)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, conversation_entry)],
            states={
                ASK_MISSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_missing)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
        )
    )
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("البوت يعمل الآن... اضغط CTRL+C للإيقاف")
    app.run_polling()


if __name__ == "__main__":
    main()
