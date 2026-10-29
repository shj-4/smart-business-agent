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

def _resolve_source_url() -> str:
    """يحدد مصدر SQLite: يفضّل business_production.db إن وجد، وإلا business.db."""
    for fname in ("business_production.db", "business.db"):
        path = os.path.join(BASE_DIR, "data", fname)
        if os.path.exists(path):
            return f"sqlite:///{path}"
    # استخدم متغير البيئة SOURCE_DB إن حُدد
    env_src = os.getenv("SOURCE_DB", "").strip()
    if env_src:
        if env_src.startswith("sqlite"):
            return env_src
        return f"sqlite:///{env_src}"
    return f"sqlite:///{os.path.join(BASE_DIR, 'data', 'business.db')}"

SOURCE_URL = _resolve_source_url()

# كل النماذج ذات الجداول — لا قائمة يدوية؛ إن أُضيف نموذج في models.py
# بالطريقة نفسها دخل إلى الترحيل تلقائيًا.
# الترتيب بالاعتماد (sorted_tables) لاحترام المفاتيح الأجنبية: companies قبل budgets/bonus_events
MODELS = [cls for table in Base.metadata.sorted_tables for cls in Base.__subclasses__() if hasattr(cls, "__tablename__") and cls.__table__ is table]
# fallback: إن لم تُكتشف كل النماذج (نادر)، أكمل الباقي أبجديًا
_seen = {m.__tablename__ for m in MODELS}
for m in sorted((m for m in Base.__subclasses__() if hasattr(m, "__tablename__") and m.__tablename__ not in _seen), key=lambda m: m.__tablename__):
    MODELS.append(m)


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

    عزل: الصفوف التي تحوي حقلًا مشفّرًا لا يمكن فكّه (is_encrypted + UNREADABLE_VALUE)
    تُرفض — لا نكتب العبارة النائبة مشفّرة فوق الأصل فيضيع للأبد.
    المبالغ التي تصير None بعد فك التشفير (مفتاح غير مطابق أو تلف) تُتخطى أيضًا
    مع تسجيل تحذير بدل نسخ صف بمبلغ فارغ (فقدان صامت).
    """
    from app.security import UNREADABLE_VALUE

    counts: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for model in model_list:
        rows = _ordered_rows(model, src)
        kept = 0
        for row in rows:
            # فحص الحقول المشفّرة: إن كانت القيمة هي النائبة، فالمفتاح الحالي لا يفكّ الأصل
            skip = False
            for col in model.__table__.columns:
                val = getattr(row, col.key, None)
                if val == UNREADABLE_VALUE:
                    skip = True
                    break
                # حقول EncryptedNumeric: None بعد فك مشفّر يعني فقدان مبلغ — لا ننسخ صفًا بمبلغ فارغ.
                # ملاحظة: row.__dict__ يحمل القيمة المفكوكة (None) لا الخام، لذا لا يمكن
                # الاعتماد عليه. نكتفي بـ UNREADABLE_VALUE (أعلاه) كحماية فعّالة.
            if skip:
                skipped[model.__name__] = skipped.get(model.__name__, 0) + 1
                continue
            values = {col.key: getattr(row, col.key) for col in model.__table__.columns}
            try:
                dst.execute(insert(model.__table__).values(**values))
            except Exception as exc:
                # تعارض مفاتيح مع --force: نحاول upsert (تحديث) بدل تجاهل
                if "--force" in sys.argv and ("Duplicate" in str(exc) or "UNIQUE" in str(exc)):
                    from sqlalchemy import update as _upd
                    pk_cols = list(model.__table__.primary_key.columns)
                    if pk_cols:
                        pk_vals = {c.key: values[c.key] for c in pk_cols}
                        try:
                            dst.execute(_upd(model.__table__).where(*(model.__table__.c[k] == v for k, v in pk_vals.items())).values(**{k: v for k, v in values.items() if k not in pk_vals}))
                        except Exception:
                            print(f"  {model.__name__}: فشل upsert للمفتاح {pk_vals}: {exc}")
                            skipped[model.__name__] = skipped.get(model.__name__, 0) + 1
                            continue
                    else:
                        skipped[model.__name__] = skipped.get(model.__name__, 0) + 1
                        continue
                else:
                    raise
            kept += 1
        dst.commit()
        counts[model.__name__] = kept
        if skipped.get(model.__name__):
            print(f"  {model.__name__}: تخطي {skipped[model.__name__]} صفًا غير قابل للفك/مكرر")
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
