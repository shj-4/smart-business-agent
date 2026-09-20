"""
ترحيل البيانات الحالية إلى التشفير (يعمل مرة واحدة).

فحص إلزامي:
  - يجب ضبط ENCRYPTION_KEY في .env قبل التشغيل (حتى لا نكتب نصًا واضحًا) —
    والمفتاح ذاته يجب أن يفك إلى 16/24/32 بايت لئلا تُكتب البيانات واضحة باسم
    "تشفير" (يُرفض الترحيل عندها حتى يُصحَّح المفتاح).
  - يعيد كتابة كل صف بحيث تُمرَّر قيمه الحالية عبر TypeDecorator → تُشفَّر.
  - يكتشف الحقول المشفّرة تلقائيًا من النماذج (EncryptedString/EncryptedNumeric)
    ولا يعتمد قائمة يدوية قد تنسى حقولًا (مثل amount_in_base_currency) أو
    نماذج كاملة (مثل Invoice.description و CorrectionFeedback.raw_message).

الاستخدام:
  powershell -ExecutionPolicy Bypass -File .\\encrypt_backfill.py   (أو)
  python -X utf8 encrypt_backfill.py
"""

import sys

from sqlalchemy import inspect, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.database.db import SessionLocal
from app.database.models import (
    BonusEvent,
    Budget,
    CorrectionFeedback,
    CreditLimit,
    EmployeeBonusPlan,
    Invoice,
    Note,
    Task,
    Transaction,
)
from app.security import EncryptedNumeric, EncryptedString

CONFIG = Settings()

# كل النماذج التي تحمل حقولًا مشفّرة — النموذج الجديد يُضاف هنا فقط،
# وأما حقوله المشفّرة فتُكتشف تلقائيًا أدناه.
_BACKFILL_MODELS = (
    Transaction,
    Note,
    Task,
    CorrectionFeedback,
    Invoice,
    Budget,
    CreditLimit,
    EmployeeBonusPlan,
    BonusEvent,
)


def _confirm_key() -> None:
    key = (CONFIG.encryption_key or "").strip()
    if not key:
        sys.exit(
            "خطأ: ENCRYPTION_KEY غير مضبوط في .env. عيّنه أولاً قبل ترحيل أي بيانات.\n"
            'مثال توليد مفتاح:  python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"'
        )
    from app.security import encryption_key_problem

    problem = encryption_key_problem(key)
    if problem:
        sys.exit(f"خطأ: ENCRYPTION_KEY {problem} — لن يُكتب أي نص واضح بدل 'تشفير'.")


def _encrypted_columns(model) -> list[str]:
    """أسماء الخصائص/الأعمدة الفعلية من نوع EncryptedString/EncryptedNumeric."""
    names = []
    for attr in inspect(model).column_attrs:
        if isinstance(attr.columns[0].type, (EncryptedString, EncryptedNumeric)):
            names.append(attr.key)
    return names


def _encrypt_row(db: Session, model, obj) -> None:
    """يعيد كتابة الحقول الحساسة للصف عبر Core UPDATE (passes عبر TypeDecorator).

    نستخدم UPDATE صريح — تعيين نفس القيمة عبر ORM لا يُعدّ تغييرًا ولا يُكتب.
    القراءة عبر ORM تفكّ التشفير (أو تُرجع القديم الواضح) → نعيد الكتابة مشفّرًا.
    القيم None تُترك كما هي (لا نكتب NULL مكان نص غير مقروء بالمفتاح الحالي).
    الصفوف التي فشل فكّ تشفيرها (قيمة "غير قابلة للقراءة" أو is_encrypted ولا
    يمكن فكّها) تُرفض — لا نعيد تشفير العبارة النائبة فوق النص الأصلي فيضيع.
    """

    from app.security import UNREADABLE_VALUE, is_encrypted

    # احصل على القيم الخام من DB (قبل فك التشفير) لتمييز المشفّر الفاشل
    # نستخدم inspect لقراءة القيمة المخزنة مباشرة إن أمكن
    raw_values: dict = {}
    try:
        # حاول قراءة الصف الخام عبر Core SELECT لنفس id
        table = model.__table__
        pk_col = list(table.primary_key.columns)[0] if list(table.primary_key.columns) else None
        if pk_col is not None:
            raw_row = db.execute(
                table.select().where(pk_col == obj.id)
            ).first()
            if raw_row is not None:
                raw_values = dict(raw_row._mapping)
    except Exception:
        raw_values = {}

    values: dict = {}
    for name in _encrypted_columns(model):
        value = getattr(obj, name)
        if value is None:
            continue
        # إذا كانت القيمة المُفككة هي العبارة النائبة، فالمفتاح الحالي لا يفكّ الأصل
        if value == UNREADABLE_VALUE:
            continue
        # إذا كانت القيمة الخام مشفّرة (v1$) لكن المفككة هي نفسها الخام أو نائبة، فشل
        raw = raw_values.get(name)
        if raw is not None and is_encrypted(raw) and value in (UNREADABLE_VALUE, raw):
            continue
        # تحقق إضافي: إذا كانت القيمة الخام مشفّرة وفشل فكّها (decrypt_text يعيد None)
        if raw is not None and is_encrypted(raw):
            from app.security import decrypt_text
            if decrypt_text(raw) is None:
                continue
        values[name] = value
    if values:
        db.execute(update(model.__table__).where(model.__table__.c.id == obj.id).values(**values))
        db.flush()


def main() -> int:
    _confirm_key()

    db = SessionLocal()
    total = 0
    try:
        for model in _BACKFILL_MODELS:
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
