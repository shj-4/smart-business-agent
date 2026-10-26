"""
سيناريوهات محاسبية لدفتر الصندوق (P&L) مقابل دفتر الذمم (charge/settlement).

النموذج الجديد:
- ledger_entry=None (الافتراضي) → حركة نقدية (P&L) — ما لم يُذكر "آجل/بالدين".
- ledger_entry="charge" → قيد آجل (ذمة) يؤثر على رصيد الشخص، لا P&L (استحقاق).
- ledger_entry="settlement" → تسديد نقدي يُعدِّل رصيد الشخص ويُحتسب في P&L.
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
    """مبيعة آجلة (charge) لا تدخل P&L، وقبضها (settlement) يُحتسب نقدًا ويخفض الرصيد."""
    # 1) بيع آجل 500 لسامر: income charge → ذمة، ليس P&L
    _tx(
        db_session,
        {"type": "income", "amount": 500, "currency": "ILS", "person": "سامر", "ledger_entry": "charge"},
        "بعت لسامر بضاعة بالأجل 500",
    )
    # 2) دفع نقدي إيجار 300: expense cash → P&L
    _tx(
        db_session,
        {
            "type": "expense",
            "amount": 300,
            "currency": "ILS",
            "ledger_entry": None,
            "description": "إيجار",
        },
        "دفعت إيجار 300",
    )
    # 3) قبض من سامر 300: income settlement → نقد (P&L) ويُخفض الرصيد
    _tx(
        db_session,
        {"type": "income", "amount": 300, "currency": "ILS", "person": "سامر", "ledger_entry": "settlement"},
        "قبضت من سامر 300",
    )

    # P&L: إجمالي الإيرادات 300 (القبض)، المصاريف 300
    inc = run_query(db_session, USER, {"metric": "total_income", "period": "all_time"})
    exp = run_query(db_session, USER, {"metric": "total_expenses", "period": "all_time"})
    assert inc["result"].get("ILS", Decimal("0")) == Decimal("300"), "القبض (settlement) يدخل P&L"
    assert exp["result"].get("ILS", Decimal("0")) == Decimal("300"), "المصاريف النقدية"

    # الذمة: سامر مدين 200 (500 - 300)
    debts = {d["person"]: d for d in person_debts(db_session, USER)}
    assert "سامر" in debts
    assert debts["سامر"]["by_currency"]["ILS"]["balance"] == Decimal("200.00")
    bal = run_query(db_session, USER, {"metric": "person_balance", "person": "سامر"})
    assert bal["result"]["ILS"]["balance"] == Decimal("200.00")

    months = monthly_totals(db_session, USER, months=1)
    cur = months[-1]["by_currency"].get("ILS", {})
    assert cur.get("expense", Decimal("0")) == Decimal("300")
    assert cur.get("income", Decimal("0")) == Decimal("300")


def test_cash_with_person_does_not_create_debt(db_session):
    """دفع نقدي لشخص (ledger_entry=None مع person) لا يظهر في الذمم."""
    _tx(
        db_session,
        {"type": "expense", "amount": 300, "currency": "ILS", "person": "محمد", "ledger_entry": None},
        "دفعت 300 لمحمد نقداً",
    )
    assert person_debts(db_session, USER) == []
    exp = run_query(db_session, USER, {"metric": "total_expenses", "period": "all_time"})
    assert exp["result"]["ILS"] == Decimal("300")
    bal = run_query(db_session, USER, {"metric": "person_balance", "person": "محمد"})
    assert bal["result"] == {}


def test_default_cash_for_null_person_and_legacy(db_session):
    """الافتراضي "نقدي": صف person NOT NULL و is_credit/ledger_entry NULL يُعتبر نقدي."""
    tx = Transaction(
        telegram_user_id=USER,
        type="income",
        amount=Decimal("400"),
        currency="ILS",
        person="قديم",
        description="قديم",
        raw_message="x",
        is_credit=None,
        debt_direction=None,
        ledger_entry=None,
    )
    db_session.add(tx)
    db_session.commit()
    assert person_debts(db_session, USER) == []
    inc = run_query(db_session, USER, {"metric": "total_income", "period": "all_time"})
    assert inc["result"].get("ILS", Decimal("0")) == Decimal("400")


def test_budget_counts_only_cash(db_session, monkeypatch):
    """الميزانية شهرية P&L فقط — charge لا تُحتسب."""
    monkeypatch.setattr("app.exchange.convert", lambda a, f, t: {"result": Decimal(str(a))})
    budget = create_budget(db_session, USER, "currency", "ILS", "1000")
    _tx(
        db_session,
        {"type": "expense", "amount": 500, "currency": "ILS", "person": "سامر", "ledger_entry": "charge"},
        "charge",
    )
    _tx(
        db_session,
        {"type": "expense", "amount": 200, "currency": "ILS", "ledger_entry": None},
        "إيجار",
    )
    usage = budget_usage(db_session, budget)
    assert usage["spent"] == Decimal("200.00")


def test_budget_person_counts_debt(db_session, monkeypatch):
    """ميزانية شخص (تعامل بالدين) تُحسب على القيود الآجلة (charge)."""
    monkeypatch.setattr("app.exchange.convert", lambda a, f, t: {"result": Decimal(str(a))})
    budget = create_budget(db_session, USER, "person", "خالد", "1000")
    _tx(
        db_session,
        {"type": "expense", "amount": 150, "currency": "ILS", "person": "خالد", "ledger_entry": "charge"},
        "دين",
    )
    _tx(
        db_session,
        {"type": "expense", "amount": 50, "currency": "ILS", "person": "خالد", "ledger_entry": None},
        "نقدي لخالد — لا يُحتسب",
    )
    usage = budget_usage(db_session, budget)
    assert usage["spent"] == Decimal("150.00")


def test_credit_limit_uses_debt_only(db_session):
    """حد ائتماني يُقاس على صافي الذمم فقط."""
    _tx(
        db_session,
        {"type": "income", "amount": 600, "currency": "ILS", "person": "علي", "ledger_entry": "charge"},
        "آجل",
    )
    _tx(
        db_session,
        {"type": "expense", "amount": 200, "currency": "ILS", "person": "علي", "ledger_entry": None},
        "نقدي — لا يؤثر",
    )
    row = set_credit_limit(db_session, USER, "علي", "1000")
    usage = credit_usage(db_session, row)
    assert usage["outstanding"] == Decimal("600.00")
    assert usage["side"] == "receivable"


def test_supplier_settlement_reduces_debt(db_session):
    """سداد مورد (expense settlement) يُنقص ما عليك له (لا يزيد)."""
    _tx(
        db_session,
        {"type": "expense", "amount": 500, "currency": "ILS", "person": "محمد", "ledger_entry": "charge"},
        "شراء بالآجل",
    )
    _tx(
        db_session,
        {"type": "expense", "amount": 200, "currency": "ILS", "person": "محمد", "ledger_entry": "settlement"},
        "دفعت لمحمد",
    )
    debts = {d["person"]: d for d in person_debts(db_session, USER)}
    assert debts["محمد"]["by_currency"]["ILS"]["balance"] == Decimal("-300.00")


def test_compare_periods_filters_cash(db_session):
    """مقارنة الفترات P&L فقط."""
    _tx(
        db_session,
        {"type": "expense", "amount": 999, "currency": "ILS", "person": "سامر", "ledger_entry": "charge"},
        "charge هذا الشهر",
    )
    _tx(
        db_session,
        {"type": "expense", "amount": 100, "currency": "ILS", "ledger_entry": None},
        "نقدي هذا الشهر",
    )
    res = run_query(db_session, USER, {"metric": "compare_periods", "period": "this_month"})
    assert res["result"]["current"]["expense"].get("ILS", Decimal("0")) == Decimal("100")