import os
import sys
from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": "Bot token من @BotFather",
    "GEMINI_API_KEY": "مفتاح Google Gemini API",
}


def validate_env():
    missing = []
    for var, description in REQUIRED_ENV.items():
        if not os.getenv(var):
            missing.append(f"  {var} — {description}")
    if missing:
        print("ERROR: متغيرات بيئة مطلوبة غير موجودة:", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        print("\nتأكد من وجود ملف .env في جذر المشروع يحتوي على هذه المتغيرات.", file=sys.stderr)
        sys.exit(1)


validate_env()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
