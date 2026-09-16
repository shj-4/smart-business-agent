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
import re
from logging.handlers import RotatingFileHandler

# مجلد السجلات داخل جذر المشروع (موجود افتراضيًا)
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# المسار الموحّد لملف السجل الرئيسي
MAIN_LOG = os.path.join(LOGS_DIR, "app.log")

# التكوين الافتراضي للتدوير
MAX_BYTES = 5 * 1024 * 1024  # 5MB
BACKUP_COUNT = 5  # يحتفظ بـ 5 ملفات قديمة (app.log.1 .. app.log.5)

# أنماط أسرار تُقصّ من أي رسالة سجل قبل كتابتها — لا تُكتَب نصوص رسائل
# Telegram الحساسة ولا المفاتيح أبدًا (تُستبدل بعلامة حجب).
_SECRET_PATTERNS = (
    re.compile(r"(TELEGRAM_BOT_TOKEN|BOT_TOKEN|API_KEY|APIKEY|GEMINI_API_KEY)[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"(ENCRYPTION_KEY)[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"(DASHBOARD_PASSWORD|PASSWORD)[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"(Authorization|authorization)[=:]\s*(?:Basic|Bearer|Token)\s+\S+"),
    re.compile(r"(token[=:]\s*)\S+", re.IGNORECASE),
)


def _scrub(text: str) -> str:
    """يحجب الأسرار المعروفة في نص السجل (بند مركزي يمر عليه كل سطر)."""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(r"\1=***REDACTED***", out)
    return out


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
            "message": _scrub(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = _scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def _make_formatter(service: str) -> logging.Formatter:
    """يختار المُنسّق حسب LOG_FORMAT (text افتراضيًا / json للتجميع المركزي)."""
    if os.environ.get("LOG_FORMAT", "").strip().lower() == "json":
        return JsonFormatter(service)
    return ScrubbingFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - "
        + (f"[{service}] " if service else "")
        + "%(message)s"
    )


class ScrubbingFormatter(logging.Formatter):
    """مُنسّق نصي يحجب الأسرار في رسالة/استثناء السجل قبل الإخراج.

    يحرص على عدم إتلاف عناصر التنسيق %s — يمرّ على الرسالة النهائية بعد اكتمال
    استبدال العناصر بدلًا من الرسالة الخام التي قد تحتوي %s.
    """

    def format(self, record: logging.LogRecord) -> str:
        if record.args:
            record.args = tuple(_scrub(str(a)) if isinstance(a, str) else a for a in record.args)
        rendered = super().format(record)
        return _scrub(rendered)


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
