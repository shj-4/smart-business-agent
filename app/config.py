import os
from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": "Bot token من @BotFather",
    "GEMINI_API_KEY": "مفتاح Google Gemini API",
}


def validate_env() -> list[str]:
    """يرجّع قائمة المتغيرات المطلوبة غير الموجودة (لا يخرج من البرنامج).

    فصل التحقق عن الخروج حتى يظل استيراد config آمنًا في الاختبارات والسياقات
    غير التشغيلية. الخروج الفعلي (sys.exit) يُترك لنقاط الدخول (bot.py / app.main).
    """
    missing = []
    for var, description in REQUIRED_ENV.items():
        if not os.getenv(var):
            missing.append(f"  {var} — {description}")
    return missing


def ensure_env_or_exit() -> None:
    """يعرض الأخطاء ويخرج بالكود 1 إذا نُقصت متغيرات مطلوبة (لنقاط الدخول فقط)."""
    missing = validate_env()
    if missing:
        print("ERROR: متغيرات بيئة مطلوبة غير موجودة:", file=os.sys.stderr)
        print("\n".join(missing), file=os.sys.stderr)
        print("\nتأكد من وجود ملف .env في جذر المشروع يحتوي على هذه المتغيرات.", file=os.sys.stderr)
        raise SystemExit(1)


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
