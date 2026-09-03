import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# مسار قاعدة البيانات - SQLite الآن
# عند الانتقال لـ PostgreSQL لاحقًا، فقط نغيّر هذا السطر إلى:
# DATABASE_URL = "postgresql://user:password@localhost/dbname"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'data', 'business.db')}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # مطلوب فقط لـ SQLite
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()