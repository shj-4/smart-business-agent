"""
نسخ احتياطي لقاعدة SQLite — غلاف لسطر الأوامر حول خدمة app.database.backup.

يُشغَّل كمهمة مجدولة يوميًا (Windows Task Scheduler أو cron):
  python scripts/backup_db.py

الاحتفاظ: أحدث 7 نسخ في data/backups. السجل في logs/backup.log.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    from app.database.backup import run_backup

    return 0 if run_backup() else 1


if __name__ == "__main__":
    sys.exit(main())
