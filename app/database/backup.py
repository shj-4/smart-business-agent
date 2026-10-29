"""
خدمة النسخ الاحتياطي القابل لإعادة الاستخدام (تُشغَّل من مهام البوت الدورية ومن
سكربت سطر الأوامر scripts/backup_db.py على حدٍّ سواء).

- نسخ آمن عبر SQLite backup API (لا يوقف قاعدة نشطة).
- احتفاظ بأحدث 7 أيام (وليس 7 ملفات فقط) — يحذف النسخ الأقدم من 7 أيام.
- يضمن نسخة واحدة فقط في اليوم (لا تكرار يومي).
- تسجيل في logs/backup.log.
- على قاعدة ليست SQLite (MySQL/PostgreSQL) تُسجَّل رسالة وتعيد False بلا ضرر.

⚠️ ملاحظة: النسخ الاحتياطي الحالي على نفس القرص فقط — يحمي من تلف قاعدة
البيانات/الحذف العرضي لكن لا يحمي من فشل القرص الكامل. يُنصح بنسخ خارجي
دوري (قرص خارجي/سحابة) لحماية كاملة.
"""

import logging
import os
import sqlite3

from app.database.db import DATABASE_URL
from app.timeutil import now_local

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKUP_DIR = os.path.join(ROOT, "data", "backups")
KEEP_COUNT = 7  # عدد الأيام المحتفظ بها (كان 7 ملفات — الآن 7 أيام)
KEEP_DAYS = 7  # احتفاظ 7 أيام
LOG_FILE = os.path.join(ROOT, "logs", "backup.log")

DB_PATH = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL.startswith("sqlite") else ""


def _log(msg: str) -> None:
    line = f"[{now_local().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
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
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith("business_") and (f.endswith(".db") or f.endswith(".sql.gz"))
    )


def _today_prefix() -> str:
    return now_local().strftime("%Y%m%d")


def _has_backup_for_today() -> bool:
    """هل توجد نسخة لنفس اليوم بالفعل؟ (يمنع التكرار اليومي)."""
    prefix = f"business_{_today_prefix()}"
    return any(f.startswith(prefix) for f in _list_backups())


def _rotate_backups() -> None:
    """يحذف النسخ الأقدم من KEEP_DAYS أيام (وليس KEEP_COUNT ملفات).

    أيضاً يحافظ على حد KEEP_COUNT كاحتياطي ثانوي (الأقدم من العدد).
    """
    from datetime import timedelta

    now = now_local()
    cutoff = now - timedelta(days=KEEP_DAYS)
    cutoff_str = cutoff.strftime("%Y%m%d")
    for fname in _list_backups():
        # استخرج التاريخ من اسم الملف business_YYYYMMDD_HHMMSS.db
        try:
            date_part = fname.split("_")[1]
            if date_part < cutoff_str:
                path = os.path.join(BACKUP_DIR, fname)
                try:
                    os.remove(path)
                    _log(f"حُذفت نسخة قديمة (>{KEEP_DAYS} أيام): {fname}")
                except OSError as exc:
                    logger.warning("فشل حذف نسخة قديمة %s: %s", fname, exc)
                continue
        except Exception:
            pass
    # احتياطي: إن بقي أكثر من KEEP_COUNT ملفات رغم قاعدة الأيام، احذف الأقدم
    remaining = _list_backups()
    if len(remaining) > KEEP_COUNT:
        for old in remaining[:-KEEP_COUNT]:
            path = os.path.join(BACKUP_DIR, old)
            try:
                os.remove(path)
                _log(f"حُذفت نسخة قديمة (تجاوز العدد): {old}")
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
    """ينفّذ نسخة احتياطية (SQLite: نسخ ملف، MySQL: mysqldump إن توفر) — يعيد True عند النجاح."""
    if DATABASE_URL.startswith("mysql"):
        if _has_backup_for_today():
            _log("تُوجد نسخة احتياطية لنفس اليوم — تخطي التكرار اليومي")
            return True
        return _run_mysql_backup()
    if not DB_PATH or not os.path.exists(DB_PATH):
        _log(f"قاعدة SQLite غير موجودة أو أن القاعدة ليست SQLite: {DB_PATH}")
        return False

    os.makedirs(BACKUP_DIR, exist_ok=True)
    # لا تنشئ نسخة ثانية في نفس اليوم — واحدة يوميًا تكفي
    if _has_backup_for_today():
        _log("تُوجد نسخة احتياطية لنفس اليوم — تخطي التكرار اليومي")
        return True

    timestamp = now_local().strftime("%Y%m%d_%H%M%S")
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


def _run_mysql_backup() -> bool:
    """نسخ MySQL عبر mysqldump إلى ملف .sql.gz (إن توفر الأداة)."""
    import gzip
    import shutil
    import subprocess
    from urllib.parse import unquote, urlparse

    if shutil.which("mysqldump") is None:
        _log("mysqldump غير متوفر — تخطي نسخ MySQL")
        return False
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if _has_backup_for_today():
        _log("تُوجد نسخة احتياطية لنفس اليوم — تخطي التكرار اليومي")
        return True
    timestamp = now_local().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"business_{timestamp}.sql.gz")
    # DATABASE_URL = mysql+pymysql://user:pass@host:port/db
    try:
        parsed = urlparse(DATABASE_URL.replace("mysql+pymysql://", "mysql://"))
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        host = parsed.hostname or "localhost"
        port = str(parsed.port or 3306)
        db_name = (parsed.path or "").lstrip("/")
        if not db_name:
            _log("اسم قاعدة MySQL غير موجود في DATABASE_URL")
            return False

        env = dict(os.environ)
        if password:
            env["MYSQL_PWD"] = password  # avoid exposing password in process list

        args = [
            "mysqldump",
            f"--host={host}",
            f"--port={port}",
            f"--user={user}",
            "--single-transaction",
            "--default-character-set=utf8mb4",
            db_name,
        ]
        proc = subprocess.run(args, capture_output=True, env=env, timeout=120)
        if proc.returncode != 0:
            _log(f"فشل mysqldump: {proc.stderr.decode(errors='ignore')[:200]}")
            return False
        with open(backup_path, "wb") as out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=out) as gz:
                gz.write(proc.stdout)
        _log(f"تم نسخ MySQL: {backup_path}")
        _rotate_backups()
        return True
    except Exception as exc:
        _log(f"خطأ نسخ MySQL: {exc}")
        try:
            os.remove(backup_path)
        except OSError:
            pass
        return False
