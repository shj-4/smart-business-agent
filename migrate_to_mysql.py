"""
ينسخ البيانات من قاعدة SQLite الحالية إلى MySQL المعيّن في DATABASE_URL.

العملية تعمل عبر ORM من الجهتين: القراءة من SQLite تفكّ التشفير للحقول
الحساسة، والكتابة إلى MySQL تشفّرها بالمفتاح نفسه من .env — فتُنقل البيانات
مشفّرةً كما كانت تمامًا دون أي فقدان.

تُكتشف الجداول المرحَّلة تلقائيًا من النماذج المسجّلة على Base — فتغطي كل
الجداول (بما فيها CreditLimit/Invoice/CorrectionFeedback/WorkspaceMember/
UserPref التي كانت مفقودة) دون قائمة يدوية تُفقدها عند إضافة نموذج جديد.

الاستخدام:
  1. عيّن DATABASE_URL لـ MySQL في .env.
  2. python init_mysql_schema.py   (إنشاء الجداول + stamp)
  3. python migrate_to_mysql.py    (نسخ البيانات)

التحقق: يعيد الجداول المُرحَّلة وعدد الصفوف، ويرفض العمل إذا كان الهدف
غير فارغ إلا مع --force.
"""

import os
import sys

from sqlalchemy import create_engine, insert, inspect
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import models  # noqa: F401  (يسجّل كل الجداول على Base.metadata)
from app.database.db import Base

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'data', 'business.db')}"

# كل النماذج ذات الجداول — لا قائمة يدوية؛ إن أُضيف نموذج في models.py
# بالطريقة نفسها دخل إلى الترحيل تلقائيًا.
MODELS = sorted(
    (m for m in Base.__subclasses__() if hasattr(m, "__tablename__")),
    key=lambda m: m.__tablename__,
)


def _session(url: str):
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)(), engine


def _ordered_rows(model, session):
    """يعيد صفوف النموذج مرتبة بأول عمود مفتاح أساسي.

    ليس كل نموذج يملك عمود id (WorkspaceMember مفتاحه telegram_user_id) —
    لذا نعتمد على PK الأول بدل افتراض id.
    """
    pk = list(model.__table__.primary_key.columns)
    if pk:
        return session.query(model).order_by(pk[0].asc()).all()
    return session.query(model).all()


def _migrate(src, dst, model_list: list) -> dict[str, int]:
    """ينسخ صفوف كل نموذج من src إلى dst ويعيد عددًا لكل نموذج.

    نكتب عبر Core INSERT صريح بدل src.expunge()/dst.add(): الكائن المفصول
    (detached) يحمل مفتاحًا أساسيًا فيعامل في الجلسة الجديدة ككائن "موجود
    مسبقًا" فينتج صفر INSERT — كانت القيمة تُقرأ والجلسة تُرتّب الالتزام
    دون أن يُكتب أي صف إلى الهدف إطلاقًا. القيم المقروءة من المصدر مفكوكة
    التشفير، وتمريرها عبر insert يعيد تشفيرها بنوع العمود في الهدف، مع حفظ
    المفاتيح الأساسية ذاتها (بما فيها WorkspaceMember بلا عمود id).
    """
    counts: dict[str, int] = {}
    for model in model_list:
        rows = _ordered_rows(model, src)
        for row in rows:
            values = {col.key: getattr(row, col.key) for col in model.__table__.columns}
            dst.execute(insert(model.__table__).values(**values))
        dst.commit()
        counts[model.__name__] = len(rows)
    return counts


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
        counts = _migrate(src, dst, MODELS)
    finally:
        src.close()
        dst.close()
        dst_engine.dispose()

    for name, count in counts.items():
        print(f"  {name}: {count} صفًا")
    total = sum(counts.values())
    print(f"اكتمل الترحيل إلى MySQL: {total} صفًا عبر {len(MODELS)} جدولًا.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
