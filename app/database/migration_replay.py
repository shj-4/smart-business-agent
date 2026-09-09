"""
إعادة تطبيق سلسلة migrations بالكامل على قاعدة بيانات SQLite (لأغراض التعافي/
التهيئة من الصفر). يُستخدم بواسطة scripts/reapply_migrations.py واختبارات.

السلوك: إن وُجد ملف قاعدة البيانات ولم يُمرَّر drop_existing=True يُرفض ذلك —
لا تُحذف قاعدة موجودة إلا بقرار صريح.
"""

import os


def _sqlite_path(db_url: str) -> str:
    if not db_url.startswith("sqlite"):
        raise ValueError("تدعم إعادة التطبيق قواعد SQLite فقط (sqlite:///...)")
    return db_url.replace("sqlite:///", "", 1)


def _delete_sqlite_sidecars(path: str) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = path + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)


def migration_plan(db_url: str) -> list[str]:
    """خطة نصية لما سيفعله السكربت (للعرض قبل التنفيذ — dry-run)."""
    steps = [f"القاعدة المستهدفة: {db_url}"]
    path = _sqlite_path(db_url)
    steps.append(f"مسار الملف: {path}")
    if os.path.exists(path):
        steps.append("الملف موجود — سيُحذف (يتطلب --yes).")
    else:
        steps.append("الملف غير موجود — ستُنشأ قاعدة جديدة.")
    steps.append("ثم: alembic upgrade head (يومي التطبيق الفعلي للهجرات).")
    return steps


def reapply_migrations(
    db_url: str | None = None,
    drop_existing: bool = False,
) -> list[str]:
    """يعيد بناء قاعدة SQLite عبر تطبيق كل الهجرات من الصفر.

    يضبط app.database.db.DATABASE_URL على الهدف قبل تشغيل alembic (env.py يقرأ
    منه الرابط) ثم يعيده إلى قيمته الأصلية في النهاية.
    """
    from alembic import command
    from alembic.config import Config

    from app.database.db import DATABASE_URL
    from app.database.init_db import ALEMBIC_INI

    url = (db_url or DATABASE_URL).strip()
    path = _sqlite_path(url)

    steps: list[str] = []
    if os.path.exists(path):
        if not drop_existing:
            raise RuntimeError(
                "قاعدة البيانات موجودة ولن تُلمس بدون موافقة صريحة — أعد التشغيل بـ --yes"
            )
        os.remove(path)
        _delete_sqlite_sidecars(path)
        steps.append(f"حُذفت القاعدة السابقة ({path})")

    import app.database.db as db_mod

    original_url = db_mod.DATABASE_URL
    db_mod.DATABASE_URL = url
    try:
        cfg = Config(ALEMBIC_INI)
        command.upgrade(cfg, "head")
        steps.append("alembic upgrade head — تم بنجاح")
    finally:
        db_mod.DATABASE_URL = original_url
    return steps
