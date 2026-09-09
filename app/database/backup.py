"""
خدمة النسخ الاحتياطي القابل لإعادة الاستخدام (تُشغَّل من مهام البوت الدورية ومن
سكربت سطر الأوامر scripts/backup_db.py على حدٍّ سواء).

- نسخ آمن عبر SQLite backup API (لا يوقف قاعدة نشطة).
- احتفاظ بأحدث KEEP_COUNT نسخ (rotation).
- تسجيل في logs/backup.log.
- على قاعدة ليست SQLite (MySQL/PostgreSQL) تُسجَّل رسالة وتعيد False بلا ضرر.
"""

import logging
import os
import sqlite3
from datetime import datetime

from app.database.db import DATABASE_URL

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKUP_DIR = os.path.join(ROOT, "data", "backups")
KEEP_COUNT = 7  # عدد النسخ المحتفظ بها
LOG_FILE = os.path.join(ROOT, "logs", "backup.log")

DB_PATH = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL.startswith("sqlite") else ""


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    logger.info(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _list_backups() -> list[str]:
    if not os.path.exists(BACKUP_DIR):
        return []
    return sorted(
        f for f in os.listdir(BACKUP_DIR) if f.startswith("business_") and f.endswith(".db")
    )


def _rotate_backups() -> None:
    """يحذف النسخ الأقدم من KEEP_COUNT."""
    for old in _list_backups()[:-KEEP_COUNT]:
        path = os.path.join(BACKUP_DIR, old)
        try:
            os.remove(path)
            _log(f"حُذفت نسخة قديمة: {old}")
        except OSError as exc:
            logger.warning("فشل حذف نسخة قديمة %s: %s", old, exc)


def _verify_backup(backup_path: str) -> bool:
    """يفحص سلامة نسخة SQLite عبر PRAGMA integrity_check. يعيد True إن كانت سليمة."""
    try:
        with sqlite3.connect(backup_path) as check:
            rows = check.execute("PRAGMA integrity_check").fetchall()
        return bool(rows) and rows[0][0] == "ok"
    except sqlite3.Error as exc:
        logger.warning("تعذّر فحص سلامة النسخة %s: %s", backup_path, exc)
        return False


def run_backup() -> bool:
    """ينفّذ نسخة احتياطية واحدة من قاعدة SQLite. يعيد True عند النجاح."""
    if not DB_PATH or not os.path.exists(DB_PATH):
        _log(f"قاعدة SQLite غير موجودة أو أن القاعدة ليست SQLite: {DB_PATH}")
        return False

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"business_{timestamp}.db")

    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(backup_path)
        src.backup(dst)
        dst.close()
        src.close()
    except Exception as exc:  # noqa: BLE001 — أي خطأ في النسخ لا يُسقط البوت
        _log(f"خطأ في النسخ الاحتياطي: {exc}")
        return False

    # تحقق من سلامة النسخة قبل اعتمادها — نسخة صامتة تالفة أسوأ من عدم وجودها
    if not _verify_backup(backup_path):
        try:
            os.remove(backup_path)
        except OSError:
            pass
        _log(f"فشل فحص سلامة النسخة {backup_path} — حُذفت النسخة غير السليمة")
        return False

    _log(f"تم النسخ الاحتياطي وسلامته موثّقة: {backup_path}")
    _rotate_backups()
    _log(f"النسخ المحتفظ بها: {len(_list_backups())}")
    return True
