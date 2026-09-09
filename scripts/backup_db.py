"""
نسخ احتياطي لقاعدة SQLite (data/business.db) بشكل آمن عبر SQLite backup API.

يُشغَّل كمهمة مجدولة يوميًا (Windows Task Scheduler أو cron):
  python scripts/backup_db.py

المميزات:
- نسخ آمن عبر SQLite backup API (لا ي足足ق قاعدة نشطة).
- احتفاظ بأحدث 7 نسخ احتياطية (cat rotation).
- تسجيل في logs/backup.log.
"""

import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.database.db import DATABASE_URL

BACKUP_DIR = os.path.join(ROOT, "data", "backups")
KEEP_COUNT = 7  # عدد النسخ المحتفظ بها
LOG_FILE = os.path.join(ROOT, "logs", "backup.log")

# استخراج مسار قاعدة البيانات من DATABASE_URL (format: sqlite:///path)
DB_PATH = DATABASE_URL.replace("sqlite:///", "")


def _log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _rotate_backups():
    """يحذف النسخ الأقدم من KEEP_COUNT."""
    if not os.path.exists(BACKUP_DIR):
        return
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("business_") and f.endswith(".db")],
    )
    for old in files[:-KEEP_COUNT]:
        path = os.path.join(BACKUP_DIR, old)
        os.remove(path)
        _log(f"حُذفت نسخة قديمة: {old}")


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)

    if not os.path.exists(DB_PATH):
        _log(f"قاعدة البيانات غير موجودة: {DB_PATH}")
        return False

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"business_{timestamp}.db")

    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(backup_path)
        src.backup(dst)
        dst.close()
        src.close()
        _log(f"تم النسخ الاحتياطي: {backup_path}")
    except Exception as exc:
        _log(f"خطأ في النسخ الاحتياطي: {exc}")
        return False

    _rotate_backups()
    _log(f"النسخ المحتفظ بها: {len(os.listdir(BACKUP_DIR))}")
    return True


if __name__ == "__main__":
    success = backup()
    sys.exit(0 if success else 1)
