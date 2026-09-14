"""
فحوصات صحة النظام (/health + تشخيص عند الإقلاع).

كلها بلا شبكة: تعتمد فقط على قاعدة البيانات، إصدارات المكتبات، والمتغيرات
المضبوطة في Settings. لا تُطبع أي أسرار (توكنات/مفاتيح) في المخرجات.
"""

import logging
import sys

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _versions() -> list[dict]:
    import alembic
    import sqlalchemy
    import telegram

    return [
        {"name": "Python", "version": sys.version.split()[0]},
        {"name": "python-telegram-bot", "version": telegram.__version__},
        {"name": "SQLAlchemy", "version": sqlalchemy.__version__},
        {"name": "alembic", "version": alembic.__version__},
    ]


def _db_check(db_session: Session | None) -> dict:
    from sqlalchemy import text as sqlalchemy_text

    ok = False
    detail = "تعذّر الاتصال"
    try:
        if db_session is None or db_session.bind is None:
            return {
                "ok": False,
                "warn": True,
                "label": "قاعدة البيانات",
                "detail": "لا توجد جلسة للتحقق",
            }
        with db_session.bind.connect() as conn:
            conn.execute(sqlalchemy_text("SELECT 1"))
            ok = True
            detail = "متصل"
    except Exception as exc:  # noqa: BLE001
        detail = f"فشل الاتصال: {exc.__class__.__name__}"
    return {"ok": ok, "label": "قاعدة البيانات", "detail": detail}


def _migration_check(db_session: Session | None) -> dict:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from app.database.init_db import ALEMBIC_INI

    try:
        script = ScriptDirectory.from_config(Config(ALEMBIC_INI))
        head = script.get_current_head()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "warn": True,
            "label": "الهجرات",
            "detail": f"لا يمكن قراءة سلسلة الهجرات: {exc.__class__.__name__}",
        }

    current = None
    try:
        if db_session is not None and db_session.bind is not None:
            with db_session.bind.connect() as conn:
                row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
                current = row[0] if row else None
    except Exception:  # noqa: BLE001
        current = None

    if current is None:
        return {
            "ok": False,
            "warn": True,
            "label": "الهجرات",
            "detail": f"لم تُطبَّق على قاعدة بيانات الاختبار (الهدف: {head})",
        }
    if current == head:
        return {"ok": True, "label": "الهجرات", "detail": f"محدّثة ({head})"}
    return {
        "ok": False,
        "label": "الهجرات",
        "detail": f"متأخرة: الحالية {current} والهدف {head} — شغّل alembic upgrade head",
    }


def _env_check() -> dict:
    from app.config import validate_env

    missing = validate_env()
    if not missing:
        return {"ok": True, "label": "المتغيرات المطلوبة", "detail": "موجودة"}
    return {
        "ok": False,
        "label": "المتغيرات المطلوبة",
        "detail": f"ناقص: {'; '.join(m.strip() for m in missing)}",
    }


def _encryption_check() -> dict:
    from app.config import settings
    from app.security import encryption_key_problem

    raw = (settings.encryption_key or "").strip()
    if not raw:
        return {
            "ok": True,
            "warn": True,
            "label": "التشفير",
            "detail": "ENCRYPTION_KEY غير مضبوط — تُخزَّن الحقول كما هي (وضع توافق/تطوير)",
        }
    problem = encryption_key_problem(raw)
    if problem:
        return {
            "ok": False,
            "warn": True,
            "label": "التشفير",
            "detail": f"ENCRYPTION_KEY {problem}",
        }
    return {"ok": True, "label": "التشفير", "detail": "مفتاح التشفير مضبوط وصالح"}


def _size_check() -> dict:
    from app.database.crud import db_size_bytes

    size = db_size_bytes()
    if size is None:
        return {
            "ok": True,
            "warn": True,
            "label": "حجم قاعدة البيانات",
            "detail": "غير قابل للقياس (غير SQLite أو ملف غير موجود)",
        }
    value = float(size)
    for unit in ("بايت", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return {"ok": True, "label": "حجم قاعدة البيانات", "detail": f"{value:.1f} {unit}"}
        value /= 1024
    return {"ok": True, "label": "حجم قاعدة البيانات", "detail": "—"}


def run_health_checks(db_session: Session | None = None) -> list[dict]:
    """يعيد قائمة فحوصات {ok, warn?, label, detail} — كل بند مستقل بلا شبكة."""
    checks = [
        _db_check(db_session),
        _migration_check(db_session),
        _env_check(),
        _encryption_check(),
        {
            "ok": True,
            "label": "الإصدارات",
            "detail": " · ".join(f"{v['name']} {v['version']}" for v in _versions()),
        },
        _size_check(),
    ]
    return checks


def startup_diagnostics() -> None:
    """فحص خفيف عند الإقلاع: يسجّل تحذيرات فقط ولا يوقف التشغيل أبدًا."""
    try:
        from app.database.db import SessionLocal

        db = SessionLocal()
        try:
            checks = run_health_checks(db)
        finally:
            db.close()
        for c in checks:
            if not c.get("ok") or c.get("warn"):
                logger.warning("فحص الإقلاع — %s: %s", c["label"], c.get("detail", ""))
    except Exception:  # noqa: BLE001
        logger.exception("فشل الفحص الصحي عند الإقلاع")
