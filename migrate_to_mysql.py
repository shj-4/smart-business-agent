"""
ينسخ البيانات من قاعدة SQLite الحالية إلى MySQL المعيّن في DATABASE_URL.

العملية تعمل عبر ORM من الجهتين: القراءة من SQLite تفكّ التشفير للحقول
الحساسة، والكتابة إلى MySQL تشفّرها بالمفتاح نفسه من .env — فتُنقل البيانات
مشفّرةً كما كانت تمامًا دون أي فقدان.

الاستخدام:
  1. عيّن DATABASE_URL لـ MySQL في .env.
  2. python init_mysql_schema.py   (إنشاء الجداول + stamp)
  3. python migrate_to_mysql.py    (نسخ البيانات)

التحقق: يعيد الجداول المُرحَّلة وعدد الصفوف، ويرفض العمل إذا كان الهدف
غير فارغ إلا مع --force.
"""

import os
import sys

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database.models import Budget, Note, ReportPref, Task, Transaction

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'data', 'business.db')}"

MODELS = [Transaction, Task, Note, Budget, ReportPref]


def _session(url: str):
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)(), engine


def main() -> int:
    target_url = (settings.database_url or "").strip()
    if not target_url or target_url.startswith("sqlite"):
        print("خطأ: عيّن DATABASE_URL لـ MySQL في .env أولًا.", file=sys.stderr)
        return 1

    force = "--force" in sys.argv

    src, _ = _session(SOURCE_URL)
    dst, dst_engine = _session(target_url)

    # عدم نسخ فوق بيانات موجودة
    if not force:
        insp = inspect(dst_engine)
        for model in MODELS:
            if model.__tablename__ in insp.get_table_names():
                count = dst.query(model).count()
                if count:
                    print(
                        f"الهدف يحتوي بيانات في {model.__tablename__} ({count} صفًا) "
                        f"— أضف --force للكتابة فوقها.",
                        file=sys.stderr,
                    )
                    return 1

    try:
        for model in MODELS:
            rows = src.query(model).order_by(model.id.asc()).all()
            for row in rows:
                # فصل الصف عن جلسة المصدر لإضافته إلى جلسة الهدف
                src.expunge(row)
                dst.add(row)
            dst.commit()
            print(f"  {model.__name__}: {len(rows)} صفًا")
    finally:
        src.close()
        dst.close()
        dst_engine.dispose()

    total = sum(dst.query(m).count() for m in (Transaction, Task, Note, Budget, ReportPref))
    print(f"اكتمل الترحيل إلى MySQL: {total} صفًا في الجداول الخمسة.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
