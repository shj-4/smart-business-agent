"""
ترحيل البيانات الحالية إلى التشفير (يعمل مرة واحدة).

فحص إلزامي:
  - يجب ضبط ENCRYPTION_KEY في .env قبل التشغيل (حتى لا نكتب نصًا واضحًا).
  - يعيد كتابة كل صف بحيث تُمرَّر قيمه الحالية عبر TypeDecorator → تُشفَّر.

الاستخدام:
  powershell -ExecutionPolicy Bypass -File .\\encrypt_backfill.py   (أو)
  python -X utf8 encrypt_backfill.py
"""

import sys

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.db import SessionLocal
from app.database.models import Note, Task, Transaction

CONFIG = Settings()


def _confirm_key() -> None:
    key = (CONFIG.encryption_key or "").strip()
    if not key:
        sys.exit(
            "خطأ: ENCRYPTION_KEY غير مضبوط في .env. عيّنه أولاً قبل ترحيل أي بيانات.\n"
            'مثال توليد مفتاح:  python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"'
        )


def _encrypt_row(db: Session, model, obj) -> None:
    """يعيد كتابة الحقول الحساسة للصف عبر Core UPDATE (passes عبر TypeDecorator).

    نستخدم UPDATE صريح — تعيين نفس القيمة عبر ORM لا يُعدّ تغييرًا ولا يُكتب.
    """
    values: dict = {}
    # القراءة عبر ORM تفكّ التشفير (أو تُرجع القديم الواضح) → نعيد الكتابة مشفّرًا
    if hasattr(obj, "description") and obj.description is not None:
        values["description"] = obj.description
    if hasattr(obj, "raw_message") and obj.raw_message is not None:
        values["raw_message"] = obj.raw_message
    if hasattr(obj, "amount") and obj.amount is not None:
        values["amount"] = obj.amount
    if values:
        db.execute(update(model.__table__).where(model.__table__.c.id == obj.id).values(**values))
        db.flush()


def main() -> int:
    _confirm_key()

    db = SessionLocal()
    total = 0
    try:
        for model in (Transaction, Task, Note):
            objs = db.query(model).all()
            for o in objs:
                _encrypt_row(db, model, o)
            db.commit()
            total += len(objs)
            print(f"  {model.__name__}: {len(objs)} صفًا معاد كتابته")
    finally:
        db.close()

    print(f"تمت إعادة كتابة {total} صفًا (الآن بالحقول المشفرة).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
