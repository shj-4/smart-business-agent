"""
ينشئ مخطط MySQL الجديد كاملًا من النماذج الحالية (Base.metadata.create_all)

لماذا ليس alembic upgrade head بدلًا منه؟
سلسلة الهجرات القديمة كُتبت زمن SQLite وبها أعمدة String() بلا طول وamount
كنوع Float — لا تصلح كـ DDL على MySQL مباشرةً. النماذج الحالية (models.py)
هي المصدر الصحيح لكل أعمدة الجداول، لذا ننشأ منها النسخة الكاملة ثم نثبّت
رقم الهجرة (stamp head) كي تُصبح الشجرة جاهزة للهجرات القادمة.

الاستخدام:
  1. عيّن DATABASE_URL في .env: mysql+pymysql://user:pass@localhost:3306/business
  2. python init_mysql_schema.py
  3. python migrate_to_mysql.py   (ينسخ البيانات من SQLite الحالية)
"""

import os
import sys

from alembic import command
from alembic.config import Config

from app.config import settings
from app.database import models  # noqa: F401  (يسجّل الجداول على Base.metadata)
from app.database.db import Base

SQLALCHEMY_URL = "sqlalchemy.url"

# مسار مطلق (كما يفعل app/database/init_db.py) — حتى يعمل السكربت من أي مجلد
# عمل، لا من جذر المشروع فقط.
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def main() -> int:
    url = (settings.database_url or "").strip()
    if not url or url.startswith("sqlite"):
        print(
            "خطأ: عيّن DATABASE_URL لـ MySQL في .env أولًا. مثال: "
            "mysql+pymysql://user:pass@localhost:3306/business",
            file=sys.stderr,
        )
        return 1

    from sqlalchemy import create_engine

    engine = create_engine(url)
    with engine.begin() as conn:
        try:
            Base.metadata.create_all(conn)
        except Exception as exc:
            if "already exists" in str(exc).lower():
                print("الجداول موجودة مسبقًا (تم إنشاؤها سابقًا).")
            else:
                raise
    print("تم إنشاء مخطط MySQL من النماذج.")

    # نُثبّت رقم هجرة "head" حتى يعتبر alembic الشجرة محقّقة
    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS_DIR)
    cfg.set_main_option(SQLALCHEMY_URL, url)
    command.stamp(cfg, "head")
    print("تم تثبيت رقم الهجرة head على alembic_version.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
