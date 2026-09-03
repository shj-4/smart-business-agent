import os
import json
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from app.ai_service import analyze_message, transcribe_audio
from app.database.db import SessionLocal
from app.database.crud import create_transaction, create_task, run_query

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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
    line += f": {result}"
    return line


def process_user_text(user_text: str, telegram_user_id: int) -> str:
    result = analyze_message(user_text)
    logger.info(f"نتيجة التحليل: {json.dumps(result, ensure_ascii=False)}")

    intent = result.get("intent")

    if intent == "record":
        saved = False
        data_type = result.get("type")

        if data_type in ("expense", "income") and result.get("amount") is not None:
            db = SessionLocal()
            try:
                create_transaction(db, telegram_user_id, result, user_text)
                saved = True
            finally:
                db.close()

        elif data_type == "task" and (result.get("description") or result.get("person")):
            db = SessionLocal()
            try:
                create_task(db, telegram_user_id, result, user_text)
                saved = True
            finally:
                db.close()

        reply = format_record_result(result, saved)

    elif intent == "query":
        db = SessionLocal()
        try:
            query_result = run_query(db, telegram_user_id, result.get("query_details") or {})
        finally:
            db.close()
        reply = format_query_result(query_result)

    elif intent == "chat":
        reply = "أهلًا! يمكنك إرسال عملية أو سؤال عن بياناتك."

    else:
        reply = "لم أفهم رسالتك، حاول مرة أخرى."

    return reply


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    telegram_user_id = update.effective_user.id
    logger.info(f"رسالة من المستخدم {telegram_user_id}: {user_text}")

    await update.message.chat.send_action(action="typing")

    try:
        reply = process_user_text(user_text, telegram_user_id)
    except Exception as e:
        logger.error(f"خطأ في معالجة الرسالة: {e}")
        reply = "حدث خطأ أثناء معالجة رسالتك، حاول مرة أخرى"

    await update.message.reply_text(reply)


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

        reply = process_user_text(text, telegram_user_id)
    except Exception as e:
        logger.error(f"خطأ في معالجة الصوت: {e}")
        reply = "حدث خطأ أثناء معالجة الرسالة الصوتية، حاول مرة أخرى"

    await update.message.reply_text(reply)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("البوت يعمل الآن... اضغط CTRL+C للإيقاف")
    app.run_polling()


if __name__ == "__main__":
    main()