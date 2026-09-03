"""
سكربت تصحيح يدوي: يعرض كل المعاملات (المصاريف والإيرادات) من قاعدة البيانات.

خارج حزمة app/ عمدًا — لهو سكربت تشغيل مستقل (scripts/) وليس جزءًا من الـ API.
يُشغَّل يدويًا:  python scripts/check_db.py
"""

import os
import sys

# إضافة جذر المشروع إلى مسار الاستيراد كي نستطيع استيراد app.*
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.database.db import SessionLocal  # noqa: E402
from app.database.models import Transaction  # noqa: E402


def main():
    db = SessionLocal()
    try:
        rows = db.query(Transaction).all()
        if not rows:
            print("لا توجد معاملات مسجلة.")
            return
        for r in rows:
            print(r.id, r.type, r.amount, r.currency, r.person, r.description, r.created_at)
    finally:
        db.close()


if __name__ == "__main__":
    main()
