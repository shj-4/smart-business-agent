"""
سكربت التعافي/التهيئة: يعيد تطبيق كل migrations على قاعدة SQLite من الصفر.

أمان: لا يحذف قاعدة موجودة إلا بتمرير --yes صراحةً؛ وdry-run يعرض الخطة فقط.

أمثلة:
    python scripts/reapply_migrations.py --dry-run
    python scripts/reapply_migrations.py --db sqlite:///C:/tmp/recovery.db --yes
"""

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.database.db import DATABASE_URL  # noqa: E402
from app.database.migration_replay import migration_plan, reapply_migrations  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="إعادة تطبيق هجرات قاعدة SQLite من الصفر")
    parser.add_argument("--db", default=None, help="DATABASE_URL كامل (افتراضي: ما في الإعدادات)")
    parser.add_argument("--yes", action="store_true", help="احذف قاعدة البيانات الموجودة إن وُجدت")
    parser.add_argument("--dry-run", action="store_true", help="اعرض الخطة فقط دون تنفيذ")
    args = parser.parse_args()

    url = args.db or DATABASE_URL
    if args.dry_run:
        for step in migration_plan(url):
            print("-", step)
        return

    try:
        steps = reapply_migrations(db_url=url, drop_existing=args.yes)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    for step in steps:
        print("-", step)
    print("تمت إعادة التطبيق بنجاح.")


if __name__ == "__main__":
    main()