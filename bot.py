"""
نقطة تشغيل البوت (تبقى كما كانت: python bot.py / start_bot.cmd).

كل المنطق فُصل إلى حزمة bot/ (bot.handlers, bot.conversation, bot.formatters,
bot.ratelimit) — هذا الملف مجرد تجميع وتسجيل وبدء الحلقة.
"""

import logging
import os

from telegram.ext import ApplicationBuilder

from app.ai_queue import aiq
from app.audit import setup_audit_log
from app.cache import start_sweeper
from app.config import TELEGRAM_BOT_TOKEN, ensure_env_or_exit
from app.logging_config import configure_logging
from app.sentry import install_sentry
from bot.diagnostics import startup_diagnostics
from bot.handlers import register_handlers
from bot.ratelimit import start_cleanup, stop_cleanup

configure_logging(service="bot")
setup_audit_log()
start_sweeper()
install_sentry()
logger = logging.getLogger(__name__)


async def _post_init(application) -> None:
    """يُستدعى بعد بناء التطبيق وقبل حلقة الاستطلاع — يشغّل طابور الـ AI إن فُعّل."""
    await aiq.ensure_started()


def main():
    from bot.single_instance import BotAlreadyRunning, acquire_polling_lock

    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot-polling.lock")
    try:
        acquire_polling_lock(lock_path)
    except BotAlreadyRunning:
        logger.error("يوجد مثيل آخر للبوت يستطلع Telegram بالفعل؛ لن يبدأ هذا المثيل.")
        return

    ensure_env_or_exit()
    startup_diagnostics()
    # استمرارية المحادثات: PicklePersistence يحفظ user_data/ConversationHandler عبر إعادة التشغيل
    # لا نخزن ORM في user_data (تم إصلاح editing.py) لذا التخزين آمن.
    from bot.persistence import FilteredPicklePersistence, scrub_existing_pickle

    persist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "conversation.pickle")
    try:
        scrub_existing_pickle(persist_path)
    except Exception:
        pass
    os.makedirs(os.path.dirname(persist_path), exist_ok=True)
    persistence = FilteredPicklePersistence(filepath=persist_path, single_file=False, on_flush=False)
    # concurrent_updates=False يمنع سباق رسالتين متتاليتين على user_data المشتركة (كان True)
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).concurrent_updates(False).post_init(_post_init).build()
    register_handlers(app)

    start_cleanup()
    try:
        logger.info("البوت يعمل الآن... اضغط CTRL+C للإيقاف")
        app.run_polling()
    finally:
        stop_cleanup()


if __name__ == "__main__":
    main()
