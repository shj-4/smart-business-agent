import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_database_url(
    base_dir: str, env_url: str | None = None, app_env: str | None = None
) -> str:
    """يرجع رابط الاتصال الفعال: DATABASE_URL إن وُجد وإلا SQLite لكل بيئة.

    فكرة الفصل بين البيئات: في التطوير نستخدم business.db مباشرة (توافق مع ما هو
    موجود)، بينما staging/production يحصلان على اسم ملف مستقل
    (business_staging.db / business_production.db) حتى لا تلمس الثلاث نسخ أي بيانات
    حقيقية. إعداد DATABASE_URL يتفوّق دائمًا (مسار MySQL/PostgreSQL).
    """
    if env_url and env_url.strip():
        return env_url.strip()
    env = (app_env or "").strip().lower()
    filename = "business.db" if not env or env == "development" else f"business_{env}.db"
    return f"sqlite:///{os.path.join(base_dir, 'data', filename)}"


DATABASE_URL = build_database_url(BASE_DIR, settings.database_url, settings.app_env)

_is_sqlite = DATABASE_URL.startswith("sqlite")

# خيارات خاصة بـ SQLite (check_same_thread) لا تنطبق على خوادم قواعد البيانات
# الكاملة؛ وعلى MySQL/PostgreSQL نفعّل pool_pre_ping لتجنّب الاتصالات المقطوعة.
_engine_kwargs: dict = {}
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(DATABASE_URL, **_engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
