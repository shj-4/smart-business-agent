"""
إعدادات التطبيق عبر Pydantic Settings (قراءة تلقائية من .env + متغيرات البيئة).

يحل بدل os.getenv المباشر: تحقق أنواع، رسائل خطأ أوضح، وقراءة .env
تلقائيًا عبر Pydantic Settings مع load_dotenv كطبقة توافقية.
"""

import sys
from typing import Annotated

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# تحميل .env مباشرة (للتوافق مع كود يعتمد os.getenv في أماكن أخرى)
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(default="", description="Bot token من @BotFather")
    gemini_api_key: str = Field(default="", description="مفتاح Google Gemini API")
    app_env: str = Field(
        default="development",
        description="البيئة: development | staging | production — تُغيّر قاعدة SQLite الافتراضية "
        "حتى لا تُجرَّب ميزات جديدة على بيانات حقيقية (database_url إن وُجد يتفوق عليها).",
    )
    log_format: str = Field(
        default="text",
        description="تنسيق سجلات الخادم: text | json — json يعطي سطر JSON منظمًا لكل حدث "
        "لتجميع مركزي (مثل Loki/CloudWatch).",
    )
    timezone: str = Field(default="Asia/Gaza", description="المنطقة الزمنية المحلية")
    first_day_of_week: int = Field(default=6, description="أول يوم في الأسبوع (6=الأحد)")
    base_currency: str = Field(default="ILS", description="العملة الأساسية للمجموع الموحّد")
    report_time: str = Field(
        default="19:00", description="وقت إرسال التقارير الدورية (HH:MM بالتوقيت المحلي)"
    )
    chart_months: int = Field(default=6, description="عدد الأشهر المرسومة في رسم /chart")

    # --- قاعدة البيانات ---
    database_url: str | None = Field(
        default=None,
        description="رابط الاتصال DATABASE_URL — إن تُرك فارغًا يُستخدم SQLite الافتراضي. "
        "مثال MySQL: mysql+pymysql://user:pass@host:3306/business",
    )

    # --- الأداء ---
    cache_ttl_seconds: int = Field(
        default=60,
        description="فترة بقاء ذاكرة التخزين المؤقت للاستعلامات المتكررة (بالثواني)",
    )
    ai_queue_enabled: bool = Field(
        default=False,
        description="تفعيل طابور المعالجة خلف تحليل الـ AI بدل الطلب المتزامن المباشر",
    )
    ai_queue_maxsize: int = Field(
        default=64,
        description="الحد الأقصى لمهام AI المنتظرة في الطابور (فوق ذلك: ضغط رجعي)",
    )
    ai_queue_concurrency: int = Field(
        default=2,
        description="عدد العمال الذين يعالجون مهام AI بالتوازي",
    )

    # --- الأمان ---
    encryption_key: str = Field(
        default="",
        description="مفتاح تشفير الحقول الحساسة (ENCRYPTION_KEY) — base64 لـ 32 بايت. "
        "إذا تُرك فارغًا تُخزَّن النصوص كما هي (وضع توافق/تطوير مع تحذير).",
    )
    admin_user_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        description="معرّفات Telegram للأدمن المسموح لها بأوامر مثل /admin_stats",
    )
    max_voice_file_mb: int = Field(
        default=20,
        description="الحد الأقصى بحجم ملفات الصوت/الصوتيات المقبولة (ميجابايت) قبل التحويل",
    )
    sentry_dsn: str = Field(
        default="",
        description="DSN من Sentry — تُفعَّل مراقبة الأخطاء تلقائيًا عند تعبئته. "
        "إن تُرك فارغًا لا يُحمَّل أي كود Sentry (صفر تكلفة في التطوير).",
    )
    dashboard_username: str = Field(
        default="admin",
        description="اسم مستخدم لوحة التحكم وواجهات /api/* (Basic Auth).",
    )
    dashboard_password: str = Field(
        default="",
        description="كلمة مرور لوحة التحكم و /api/* — إن تُركت فارغة تُولَّد كلمة "
        "مرور مؤقتة عند كل إقلاع وتُطبع في سجل التشغيل.",
    )

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            ids: list[int] = []
            for p in parts:
                try:
                    ids.append(int(p))
                except ValueError:
                    continue
            return ids
        return v


settings = Settings()

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
    if not settings.telegram_bot_token:
        missing.append(f"  TELEGRAM_BOT_TOKEN — {REQUIRED_ENV['TELEGRAM_BOT_TOKEN']}")
    if not settings.gemini_api_key:
        missing.append(f"  GEMINI_API_KEY — {REQUIRED_ENV['GEMINI_API_KEY']}")
    return missing


def ensure_env_or_exit() -> None:
    """يعرض الأخطاء ويخرج بالكود 1 إذا نُقصت متغيرات مطلوبة (لنقاط الدخول فقط)."""
    missing = validate_env()
    if missing:
        print("ERROR: متغيرات بيئة مطلوبة غير موجودة:", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        print("\nتأكد من وجود ملف .env في جذر المشروع يحتوي على هذه المتغيرات.", file=sys.stderr)
        raise SystemExit(1)


# أسماء مباشرة للتوافق مع الكود الحالي (app.main.py, bot.py القديم, ai_service.py)
TELEGRAM_BOT_TOKEN = settings.telegram_bot_token or None
GEMINI_API_KEY = settings.gemini_api_key or None
