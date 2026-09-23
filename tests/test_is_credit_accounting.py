"""
سيناريوهات محاسبية لدفتر الصندوق (P&L نقدي) مقابل دفتر الذمم (is_credit).

النموذج الموثّق في prompts/system_general.md و app/database/models.py:
- is_credit=True  → ذمة (آجل/سداد) تؤثر على رصيد الشخص، لا على إجمالي P&L
- is_credit=False → نقدي يؤثر على P&L، لا على الرصيد
- للتوافق: person IS NOT NULL و is_credit IS NULL يُعتبر ذمة
"""

from decimal import Decimal

from app.database.crud import create_transaction, person_debts
from app.database.crud.budgets import budget_usage, create_budget
from app.database.crud.credit import credit_usage, set_credit_limit
from app.database.crud.records import run_query
from app.database.crud.reports import monthly_totals
from app.database.models import Transaction

USER = 9999


def _tx(db, data, raw="raw"):
    return create_transaction(db, USER, data, raw)


def test_cash_vs_credit_separation(db_session):
    """مبيعة آجلة (ذمة) لا تدخل P&L، وسدادها يخفض الرصيد دون زيادة المصاريف."""
    # 1) بيع آجل 500 لسامر: income is_credit=true → ذمة، ليس P&L
    _tx(
        db_session,
        {"type": "income", "amount": 500, "currency": "ILS", "person": "سامر", "is_credit": True},
        "بعت لسامر بضاعة بالأجل 500",
    )
    # 2) دفع نقدي إيجار 300: expense is_credit=false → P&L
    _tx(
        db_session,
        {
            "type": "expense",
            "amount": 300,
            "currency": "ILS",
            "is_credit": False,
            "description": "إيجار",
        },
        "دفعت إيجار 300",
    )
    # 3) سداد من سامر 300: expense is_credit=true → ذمة، ليس مصروف P&L
    _tx(
        db_session,
        {"type": "expense", "amount": 300, "currency": "ILS", "person": "سامر", "is_credit": True},
        "سدد سامر 300",
    )

    # P&L: إجمالي الإيرادات يجب أن يكون 0 (الآجل لا يُحتسب)، المصاريف 300 فقط
    inc = run_query(db_session, USER, {"metric": "total_income", "period": "all_time"})
    exp = run_query(db_session, USER, {"metric": "total_expenses", "period": "all_time"})
    assert inc["result"].get("ILS", Decimal("0")) == Decimal("0"), "الآجل لا يدخل P&L"
    assert exp["result"].get("ILS", Decimal("0")) == Decimal("300"), "السداد الآجل لا يزيد المصاريف"

    # الذمة: سامر مدين 200 (500 - 300)
    debts = {d["person"]: d for d in person_debts(db_session, USER)}
    assert "سامر" in debts
    assert debts["سامر"]["by_currency"]["ILS"]["balance"] == Decimal("200.00")
    # رصيد عبر run_query person_balance يجب أن يطابق
    bal = run_query(db_session, USER, {"metric": "person_balance", "person": "سامر"})
    assert bal["result"]["ILS"]["balance"] == Decimal("200.00")

    # monthly_totals P&L يجب أن يطابق: مصروف 300 فقط
    months = monthly_totals(db_session, USER, months=1)
    cur = months[-1]["by_currency"].get("ILS", {})
    assert cur.get("expense", Decimal("0")) == Decimal("300")
    assert cur.get("income", Decimal("0")) == Decimal("0")


def test_cash_with_person_does_not_create_debt(db_session):
    """دفع نقدي لشخص (is_credit=False مع person) لا يظهر في الذمم."""
    _tx(
        db_session,
        {"type": "expense", "amount": 300, "currency": "ILS", "person": "محمد", "is_credit": False},
        "دفعت 300 لمحمد نقداً",
    )
    # الذمم يجب أن تكون فارغة (لا دين)
    assert person_debts(db_session, USER) == []
    # P&L يجب أن يحتويها
    exp = run_query(db_session, USER, {"metric": "total_expenses", "period": "all_time"})
    assert exp["result"]["ILS"] == Decimal("300")
    # person_balance لذمة محمد يجب أن يكون فارغًا (لا ذمة) — لا يُحتسب النقدي
    bal = run_query(db_session, USER, {"metric": "person_balance", "person": "محمد"})
    assert bal["result"] == {}  # لا ذمة → لا عملات


def test_legacy_null_is_credit_with_person_is_debt(db_session):
    """للتوافق: صف قديم person NOT NULL و is_credit NULL يُعتبر ذمة."""
    # إدخال مباشر بدون is_credit (يترك NULL) — يحاكي بيانات قديمة قبل الهجرة
    tx = Transaction(
        telegram_user_id=USER,
        type="income",
        amount=Decimal("400"),
        currency="ILS",
        person="قديم",
        description="قديم",
        raw_message="x",
        is_credit=None,
    )
    db_session.add(tx)
    db_session.commit()
    # يجب أن يُحتسب في الذمم
    debts = {d["person"]: d for d in person_debts(db_session, USER)}
    assert "قديم" in debts
    assert debts["قديم"]["by_currency"]["ILS"]["balance"] == Decimal("400.00")
    # ولا يُحتسب في P&L
    inc = run_query(db_session, USER, {"metric": "total_income", "period": "all_time"})
    assert inc["result"].get("ILS", Decimal("0")) == Decimal("0")


def test_budget_counts_only_cash(db_session, monkeypatch):
    """الميزانية شهرية P&L فقط — ذمم لا تُحتسب."""
    monkeypatch.setattr("app.exchange.convert", lambda a, f, t: {"result": Decimal(str(a))})
    budget = create_budget(db_session, USER, "currency", "ILS", "1000")
    # ذمة 500 ILS لسامر — لا يجب أن تُحتسب في ميزانية العملة
    _tx(
        db_session,
        {"type": "expense", "amount": 500, "currency": "ILS", "person": "سامر", "is_credit": True},
        "ذمة",
    )
    # نقدي 200 ILS إيجار — يُحتسب
    _tx(
        db_session,
        {"type": "expense", "amount": 200, "currency": "ILS", "is_credit": False},
        "إيجار",
    )
    usage = budget_usage(db_session, budget)
    assert usage["spent"] == Decimal("200.00")


def test_budget_person_counts_debt(db_session, monkeypatch):
    """ميزانية شخص (تعامل بالدين) تُحسب على الذمم."""
    monkeypatch.setattr("app.exchange.convert", lambda a, f, t: {"result": Decimal(str(a))})
    budget = create_budget(db_session, USER, "person", "خالد", "1000")
    _tx(
        db_session,
        {"type": "expense", "amount": 150, "currency": "ILS", "person": "خالد", "is_credit": True},
        "دين",
    )
    _tx(
        db_session,
        {"type": "expense", "amount": 50, "currency": "ILS", "person": "خالد", "is_credit": False},
        "نقدي لخالد — لا يُحتسب في ميزانية الشخص",
    )
    usage = budget_usage(db_session, budget)
    # ميزانية الشخص تُحسب ذمم فقط → 150
    assert usage["spent"] == Decimal("150.00")


def test_credit_limit_uses_debt_only(db_session):
    """حد ائتماني يُقاس على صافي الذمم فقط."""
    _tx(
        db_session,
        {"type": "income", "amount": 600, "currency": "ILS", "person": "علي", "is_credit": True},
        "آجل",
    )
    _tx(
        db_session,
        {"type": "expense", "amount": 200, "currency": "ILS", "person": "علي", "is_credit": False},
        "نقدي — لا يؤثر",
    )
    row = set_credit_limit(db_session, USER, "علي", "1000")
    usage = credit_usage(db_session, row)
    # صافي ذمة = 600 (دين) - 0 (نقدي لا يُحتسب) = 600
    assert usage["outstanding"] == Decimal("600.00")
    assert usage["side"] == "receivable"


def test_compare_periods_filters_cash(db_session):
    """مقارنة الفترات P&L فقط."""
    # ننشئ ذمة قديمة في هذا الشهر — يجب ألا تؤثر على compare
    _tx(
        db_session,
        {"type": "expense", "amount": 999, "currency": "ILS", "person": "سامر", "is_credit": True},
        "ذمة هذا الشهر",
    )
    _tx(
        db_session,
        {"type": "expense", "amount": 100, "currency": "ILS", "is_credit": False},
        "نقدي هذا الشهر",
    )
    res = run_query(db_session, USER, {"metric": "compare_periods", "period": "this_month"})
    # current expense يجب أن يكون 100 فقط (نقدي)
    assert res["result"]["current"]["expense"].get("ILS", Decimal("0")) == Decimal("100")
