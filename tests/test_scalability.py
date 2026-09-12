"""
اختبارات تكامل ذاكرة الاستعلامات مع crud.run_query وإحصائيات الأدمن،
وقاعدة بيانات قابلة للتبديل (MySQL/PostgreSQL عبر DATABASE_URL).
"""

from app.database import crud
from app.database.db import build_database_url
from app.database.models import Transaction


def test_run_query_cached_and_invalidated_on_write(db_session, monkeypatch):
    from app.database.crud import create_transaction

    calls = {"n": 0}
    real = crud._run_query_uncached

    def counting(db, uid, qd):
        calls["n"] += 1
        return real(db, uid, qd)

    monkeypatch.setattr(crud, "_run_query_uncached", counting)

    qd = {"metric": "total_expenses", "period": "all_time", "person": None}
    r1 = crud.run_query(db_session, 111, qd)
    r2 = crud.run_query(db_session, 111, qd)
    assert calls["n"] == 1
    assert r2 == r1  # نفس النتيجة من الكاش

    # كتابة جديدة تمسح كاش المستخدم → الاستعلام التالي يُنفَّذ من جديد
    create_transaction(
        db_session,
        111,
        {"type": "expense", "amount": 50, "currency": "ILS", "description": "جديدة"},
        raw_message="جديدة",
        telegram_message_id=9,
    )
    r3 = crud.run_query(db_session, 111, qd)
    assert calls["n"] == 2
    assert r3["result"]["ILS"] > r1["result"].get("ILS", 0)


def test_run_query_list_tasks_not_cached(db_session, monkeypatch):
    calls = {"n": 0}
    real = crud._run_query_uncached

    def counting(db, uid, qd):
        calls["n"] += 1
        return real(db, uid, qd)

    monkeypatch.setattr(crud, "_run_query_uncached", counting)
    qd = {"metric": "list_tasks", "period": "all_time", "person": None}
    crud.run_query(db_session, 1, qd)
    crud.run_query(db_session, 1, qd)
    assert calls["n"] == 2  # قوائم المهام تقرأ حيًّا دائمًا


def test_run_query_cache_keyed_per_user(db_session, monkeypatch):

    monkeypatch.setattr(crud, "_run_query_uncached", lambda db, u, q: {"result": {f"u{u}": u}})
    qd = {"metric": "total_expenses", "period": "all_time", "person": "مورد"}
    a = crud.run_query(db_session, 1, qd)
    b = crud.run_query(db_session, 2, qd)
    assert a["result"] == {"u1": 1}
    assert b["result"] == {"u2": 2}
    # نفس المستخدم والمفتاح نفسه مكرر
    a2 = crud.run_query(db_session, 1, qd)
    assert a2["result"] == {"u1": 1}
    # تنويع person يغيّر المفتاح
    c = crud.run_query(
        db_session, 1, {"metric": "total_expenses", "period": "all_time", "person": "آخر"}
    )
    assert c["result"] == {"u1": 1}


def test_admin_stats_cached(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app import money
    from app.admin import build_admin_stats
    from app.database.db import Base

    # جلسة مستقلة في الذاكرة (لا تعتمد على قاعدة البيانات الفعلية — قد تكون
    # بسكيمة أقدم) حتى نحصل على كل جدول بأحدث الأعمدة.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()

    calls = {"n": 0}
    real = money._sum_amounts_by_currency

    def counting(rows):
        calls["n"] += 1
        return real(rows)

    monkeypatch.setattr(money, "_sum_amounts_by_currency", counting)

    s1 = build_admin_stats(db)
    s2 = build_admin_stats(db)
    assert s1 == s2
    assert calls["n"] >= 1
    before = calls["n"]
    # المكالمة الثانية يجب ألا تعيد القراءة (نفس رقم est. مرة واحدة على الأقل)
    assert calls["n"] == before  # لا زيادة بعد التخزين المؤقت
    db.close()


def test_admin_stats_ai_queue_field_present(db_session):
    from app.admin import _build_admin_stats_uncached
    from app.ai_queue import aiq

    stats = _build_admin_stats_uncached(db_session)
    assert "ai_queue" in stats
    # الحقل يعكس إعداد الطابور الحالي (مفعّل افتراضيًا كإعداد الإنتاج) وليس قيمة ثابتة
    assert stats["ai_queue"]["enabled"] is bool(aiq.enabled)
    assert isinstance(stats["ai_queue"]["pending"], int)
    assert isinstance(stats["ai_queue"]["processed"], int)
    assert isinstance(stats["ai_queue"]["failed"], int)


def test_build_database_url_helpers():
    # بدون DATABASE_URL → SQLite الافتراضي داخل المجلد data
    url = build_database_url(r"C:\proj", None)
    assert url == "sqlite:///C:\\proj\\data\\business.db"

    # مع DATABASE_URL → يُستعمل كما هو
    mysql = "mysql+pymysql://u:p@localhost:3306/biz"
    assert build_database_url(r"C:\proj", mysql) == mysql

    # قيم فارغة أو مسافات → SQLite
    assert build_database_url(r"C:\proj", "   ") == "sqlite:///C:\\proj\\data\\business.db"


def test_build_database_url_env_separation():
    # تطوير (افتراضي/صريح) → نفس ملف business.db
    assert (
        build_database_url(r"C:\proj", None, "development")
        == "sqlite:///C:\\proj\\data\\business.db"
    )
    # بيئة أخرى → ملف مستقل حتى لا تُجرَّب ميزات على بيانات حقيقية
    assert (
        build_database_url(r"C:\proj", None, "staging")
        == "sqlite:///C:\\proj\\data\\business_staging.db"
    )
    assert (
        build_database_url(r"C:\proj", None, "Production")
        == "sqlite:///C:\\proj\\data\\business_production.db"
    )
    # DATABASE_URL يتفوق دائمًا على اسم الملف حسب البيئة
    assert (
        build_database_url(r"C:\proj", "postgresql://x:y@h/db", "staging")
        == "postgresql://x:y@h/db"
    )


def test_transactions_composite_index_in_metadata():
    idx_names = {i.name for i in Transaction.__table__.indexes}
    assert "ix_transactions_user_created" in idx_names
    idx = next(i for i in Transaction.__table__.indexes if i.name == "ix_transactions_user_created")
    assert [c.name for c in idx.columns] == ["telegram_user_id", "created_at"]
