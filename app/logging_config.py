"""
إعداد تسجيل (Logging) موحّد مع تدوير الملفات.

الغرض: بدلًا من تشتت السجلات في ملفات متعددة وامتلاء المساحة مع الوقت،
نوفّر مصدرًا واحدًا للتسجيل بتدوير تلقائي (RotatingFileHandler):
- ملف سجلّ واحد بحجم أقصى محدد، وعند تجاوزه يُنشأ ملف قديم محفوظ (backup) بعدد محدد.
- نفس الإعداد يُستخدم من bot.py و app/main.py (uvicorn) بإعداد root logger.
- التنسيق يتضمن الطابع الزمني والمنسّق ومستوى السجل، مع دعم النصوص العربية.
"""

import json
import logging
import os
from logging.handlers import RotatingFileHandler

# مجلد السجلات داخل جذر المشروع (موجود افتراضيًا)
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# المسار الموحّد لملف السجل الرئيسي
MAIN_LOG = os.path.join(LOGS_DIR, "app.log")

# التكوين الافتراضي للتدوير
MAX_BYTES = 5 * 1024 * 1024  # 5MB
BACKUP_COUNT = 5  # يحتفظ بـ 5 ملفات قديمة (app.log.1 .. app.log.5)


class JsonFormatter(logging.Formatter):
    """مُنسّق سجلات JSON (سطر واحد لكل حدث) — للتجميع المركزي والمعالجة الآلية."""

    def __init__(self, service: str = ""):
        super().__init__()
        self.service = service

    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service or "",
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _make_formatter(service: str) -> logging.Formatter:
    """يختار المُنسّق حسب LOG_FORMAT (text افتراضيًا / json للتجميع المركزي)."""
    if os.environ.get("LOG_FORMAT", "").strip().lower() == "json":
        return JsonFormatter(service)
    return logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - "
        + (f"[{service}] " if service else "")
        + "%(message)s"
    )


def configure_logging(level: int = logging.INFO, service: str = "") -> None:
    """
    يهيّئ root logger بمعالجة ملف دوّار + إخراج للطرفية.

    - service: اسم يظهر في بداية كل سطر لتمييز المصدر (مثل "bot" أو "api").
    - تنسيق JSON عبر LOG_FORMAT=json (انظر _make_formatter).
    - استدعاؤها أكثر من مرة لا يضيف معالجات مكررة (idempotent).
    """
    # نطبّق الإخراج على root logger ليشمل جميع المكتبات (httpx, telegram, uvicorn...)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = _make_formatter(service)

    # تجنّب إضافة معالجات مكررة عند إعادة الاستدعاء
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = RotatingFileHandler(
        MAIN_LOG, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # تصعيد مكتبات خارجية مزعجة إلى مستوى أعلى لو أردنا ضبطها لاحقًا
    logging.getLogger("httpx").setLevel(logging.WARNING)
