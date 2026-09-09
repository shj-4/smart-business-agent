"""
Fixtures مشتركة لكل الاختبارات (unit tests بلا شبكة).

قاعدة بيانات SQLite في الذاكرة (sqlite:///:memory:) مع StaticPool حتى يبقى
الاتصال نفسه صالحًا لكل الجلسة (لأن SQLite :memory: في اتصال واحد فقط).
تُنشأ الجداول عبر Base.metadata.create_all وتُفضَّ بالكامل بعد كل اختبار.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import models  # noqa: F401  (يُسجّل الجداول الثلاثة على Base.metadata)
from app.database.db import Base


@pytest.fixture(autouse=True)
def _clear_ttl_cache():
    """يمسح ذاكرة الاستعلامات المؤقتة بين الاختبارات حتى لا تتداخل القيم."""
    from app.cache import clear_all

    clear_all()
    yield
    clear_all()


@pytest.fixture
def db_session():
    """جلسة قاعدة بيانات نظيفة (في الذاكرة) لكل اختبار."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
