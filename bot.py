"""
نقطة تشغيل البوت (تبقى كما كانت: python bot.py / start_bot.cmd).

كل المنطق فُصل إلى حزمة bot/ (bot.handlers, bot.conversation, bot.formatters,
bot.ratelimit) — هذا الملف مجرد تجميع وتسجيل وبدء الحلقة.
"""

import logging

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
    ensure_env_or_exit()
    startup_diagnostics()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    register_handlers(app)

    start_cleanup()
    try:
        logger.info("البوت يعمل الآن... اضغط CTRL+C للإيقاف")
        app.run_polling()
    finally:
        stop_cleanup()


if __name__ == "__main__":
    main()
