"""
تهيئة قاعدة البيانات عبر Alembic migrations (بدلاً من create_all).

`alembic upgrade head` يطبّق جميع الـ migrations المعلّقة على قاعدة البيانات
القائمة من دون فقدان بيانات، وهو الطريقة الصحيحة لتطوير الـ schema مستقبلًا
(إضافة/حذف أعمدة عبر revisions جديدة).

مثال الاستخدام:
    python -m app.database.init_db
"""

import os
import sys

from alembic.config import Config
from alembic import command

# جذر المشروع — لأن alembic.ini و migrations/ في الجذر
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ALEMBIC_INI = os.path.join(BASE_DIR, "alembic.ini")


def init_db():
    cfg = Config(ALEMBIC_INI)
    command.upgrade(cfg, "head")
    print("تم تحديث قاعدة البيانات إلى آخر إصدار (alembic upgrade head)")


if __name__ == "__main__":
    init_db()
