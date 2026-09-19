"""
اختبارات migrate_to_mysql: ألا تُفقد أي جداول عند الترحيل (كانت القائمة
اليدوية تنسى CreditLimit/Invoice/CorrectionFeedback/WorkspaceMember/UserPref).

- تغطية MODELS لكل جدول مسجّل على Base (بلا قائمة يدوية).
- ترحيل فعلي بين قاعدتي SQLite مؤقتتين يتحقق من العبور المشفّر والقيم.
"""

import base64
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import models as M
from app.database.db import Base
from app.security import settings as security_settings
from migrate_to_mysql import MODELS, _migrate

_VALID_KEY = base64.b64encode(b"0" * 32).decode("ascii")


@pytest.fixture
def enc_key(monkeypatch):
    monkeypatch.setattr(security_settings, "encryption_key", _VALID_KEY)


def _file_session(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)(), engine


@pytest.fixture
def src(tmp_path, enc_key):
    session, engine = _file_session(str(tmp_path / "src.db"))
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def dst(tmp_path):
    session, engine = _file_session(str(tmp_path / "dst.db"))
    yield session
    session.close()
    engine.dispose()


def _seed(session):
    session.add_all(
        [
            M.Transaction(
                telegram_user_id=111,
                type="expense",
                amount=Decimal("50.00"),
                currency="ILS",
                description="سرّ",
                raw_message="نص خام",
            ),
            M.Note(telegram_user_id=111, note_type="note", description="ملاحظة", raw_message="نص"),
            M.Task(
                telegram_user_id=111,
                description="مهمة",
                priority="high",
                status="pending",
                reminder_sent=False,
                raw_message="تذكير",
            ),
            M.CorrectionFeedback(
                telegram_user_id=111, source="cancel", raw_message="نص خاطئ", reviewed=False
            ),
            M.WorkspaceMember(telegram_user_id=111, workspace_id=1, status="active"),
            M.WorkspaceMember(telegram_user_id=222, workspace_id=1, status="pending"),
            M.Budget(
                telegram_user_id=111, scope="category", category="مشتريات",
                monthly_limit=Decimal("1000.00"),
            ),
            M.CreditLimit(telegram_user_id=111, person="مورّد", limit_amount=Decimal("5000.00")),
            M.Invoice(
                telegram_user_id=111,
                person="مورّد",
                amount=Decimal("300.00"),
                currency="ILS",
                description="فاتورة آجلة",
                status="pending",
                alerted=False,
            ),
            M.ReportPref(telegram_user_id=111, frequency="daily", deliver_time="19:00"),
            M.UserPref(telegram_user_id=111, lang="ar"),
            M.BonusEvent(
                telegram_user_id=111,
                name="عروض رمضان",
                budget=Decimal("500.00"),
                currency="ILS",
                status="planned",
            ),
            M.EmployeeBonusPlan(
                telegram_user_id=111,
                person="موظف 1",
                amount=Decimal("200.00"),
                currency="ILS",
                frequency="monthly",
                enabled=True,
            ),
            M.LoyaltyAccount(
                telegram_user_id=111,
                person="عميل 1",
                points_balance=10,
                total_earned=20,
                total_redeemed=10,
            ),
            M.LoyaltyConfig(
                telegram_user_id=111,
                points_rate=Decimal("1"),
                points_value=Decimal("0.01"),
                min_redeem_points=0,
            ),
        ]
    )
    session.commit()


class TestModelCoverage:
    def test_models_cover_every_table(self):
        tables = set(Base.metadata.tables.keys()) - {"alembic_version"}
        assert {m.__tablename__ for m in MODELS} == tables

    def test_previously_missing_tables_are_covered(self):
        names = {m.__tablename__ for m in MODELS}
        for table in ("credit_limits", "invoices", "correction_feedback", "workspace_members", "user_prefs"):
            assert table in names


class TestMigration:
    def test_every_model_copied(self, src, dst, enc_key):
        _seed(src)
        counts = _migrate(src, dst, MODELS)
        assert counts == {
            "BonusEvent": 1,
            "Budget": 1,
            "Company": 0,
            "CompanyMember": 0,
            "CorrectionFeedback": 1,
            "CreditLimit": 1,
            "EmployeeBonusPlan": 1,
            "InviteLink": 0,
            "Invoice": 1,
            "LoyaltyAccount": 1,
            "LoyaltyConfig": 1,
            "Note": 1,
            "ReportPref": 1,
            "Task": 1,
            "Transaction": 1,
            "UserPref": 1,
            "WorkspaceMember": 2,
        }
        # الهدف نفسه وُضعت فيه الصفوف (كان expunge/add يكتب صفرًا)
        expected = {
            m: counts[m.__name__] for m in MODELS
        }
        for model in MODELS:
            assert dst.query(model).count() == expected[model]

    def test_values_survive_encrypted(self, src, dst, enc_key):
        _seed(src)
        _migrate(src, dst, MODELS)

        raw = dst.execute(text("SELECT description FROM transactions")).one()
        assert str(raw[0]).startswith("v1$")  # كُتب مشفّرًا في الهدف

        tx = dst.query(M.Transaction).one()
        assert tx.amount == Decimal("50.00")
        assert tx.description == "سرّ"
        assert tx.raw_message == "نص خام"

        invoice = dst.query(M.Invoice).one()
        assert invoice.description == "فاتورة آجلة"
        assert invoice.amount == Decimal("300.00")

        members = dst.query(M.WorkspaceMember).order_by(M.WorkspaceMember.telegram_user_id).all()
        assert [w.telegram_user_id for w in members] == [111, 222]
        assert dst.query(M.CreditLimit).one().limit_amount == Decimal("5000.00")
        assert dst.query(M.UserPref).one().lang == "ar"
        assert dst.query(M.ReportPref).one().frequency == "daily"

    def test_workspace_member_without_id_column_is_ordered(self, src, enc_key):
        src.add_all(
            [
                M.WorkspaceMember(telegram_user_id=222, workspace_id=1, status="active"),
                M.WorkspaceMember(telegram_user_id=111, workspace_id=1, status="active"),
            ]
        )
        src.commit()
        from migrate_to_mysql import _ordered_rows

        rows = _ordered_rows(M.WorkspaceMember, src)
        assert [w.telegram_user_id for w in rows] == [111, 222]
