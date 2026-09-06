"""
إعداد تسجيل (Logging) موحّد مع تدوير الملفات.

الغرض: بدلًا من تشتت السجلات في ملفات متعددة وامتلاء المساحة مع الوقت،
نوفّر مصدرًا واحدًا للتسجيل بتدوير تلقائي (RotatingFileHandler):
- ملف سجلّ واحد بحجم أقصى محدد، وعند تجاوزه يُنشأ ملف قديم محفوظ (backup) بعدد محدد.
- نفس الإعداد يُستخدم من bot.py و app/main.py (uvicorn) بإعداد root logger.
- التنسيق يتضمن الطابع الزمني والمنسّق ومستوى السجل، مع دعم النصوص العربية.
"""

import os
import logging
from logging.handlers import RotatingFileHandler

# مجلد السجلات داخل جذر المشروع (موجود افتراضيًا)
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# المسار الموحّد لملف السجل الرئيسي
MAIN_LOG = os.path.join(LOGS_DIR, "app.log")

# التكوين الافتراضي للتدوير
MAX_BYTES = 5 * 1024 * 1024  # 5MB
BACKUP_COUNT = 5              # يحتفظ بـ 5 ملفات قديمة (app.log.1 .. app.log.5)


def configure_logging(level: int = logging.INFO, service: str = "") -> None:
    """
    يهيّئ root logger بمعالجة ملف دوّار + إخراج للطرفية.

    - service: اسم يظهر في بداية كل سطر لتمييز المصدر (مثل "bot" أو "api").
    - استدعاؤها أكثر من مرة لا يضيف معالجات مكررة (idempotent).
    """
    # نطبّق الإخراج على root logger ليشمل جميع المكتبات (httpx, telegram, uvicorn...)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - " + (f"[{service}] " if service else "") + "%(message)s"
    )

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
