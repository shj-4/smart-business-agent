import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_database_url(base_dir: str, env_url: str | None = None) -> str:
    """يرجع رابط الاتصال الفعال: DATABASE_URL من .env/البيئة إن وُجد وإلا SQLite الافتراضي.

    هذا هو المكان الوحيد الذي يُقرر فيه أي قاعدة بيانات تُستخدم — الانتقال إلى
    MySQL/PostgreSQL يتم فقط بتعيين DATABASE_URL بدون أي تعديل كود.
    """
    if env_url and env_url.strip():
        return env_url.strip()
    return f"sqlite:///{os.path.join(base_dir, 'data', 'business.db')}"


DATABASE_URL = build_database_url(BASE_DIR, settings.database_url)

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
