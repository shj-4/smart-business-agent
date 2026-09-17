"""
اختبارات ميزة «البونس والمكافآت» (Bonus & rewards):

- تسجيل بونس/مكافآت كمعاملات بتصنيف بونس + تقارير مخصصة.
- فعاليات ترويجية (BonusEvent): إنشاء/قائمة/حالات/خلاصة.
- مكافآت موظفين (EmployeeBonusPlan): خطط/دورية/سقف شهري/تذكيرات.
- نقاط الولاء (LoyaltyAccount + LoyaltyConfig): تفعيل/تراكم/استبدال/إلغاء.
- أدوات مساعدة bot.bonus (تنسيق + تحليل وسيطات).

تعتمد على fixture `db_session` من tests/conftest.py (SQLite في الذاكرة).
"""

from datetime import datetime, timedelta
from decimal import Decimal

from app.database.crud import (
    accrue_loyalty_for_transaction,
    advance_employee_bonus_due,
    bonus_overview,
    bonus_report_summary,
    create_bonus_event,
    create_employee_bonus_plan,
    create_transaction,
    disable_employee_bonus_plan,
    disable_loyalty,
    due_employee_bonus_plans,
    employee_bonus_monthly_spent,
    employee_bonus_overview,
    end_bonus_event,
    event_bonus_summary,
    list_bonus_events,
    list_bonus_transactions,
    list_employee_bonus_plans,
    list_loyalty_accounts,
    loyalty_account_get,
    loyalty_add_points,
    loyalty_config_enable,
    loyalty_config_get,
    loyalty_redeem_points,
    record_bonus_grant,
    set_bonus_event_status,
    set_loyalty_config,
)
from app.database.models import LoyaltyAccount
from app.timeutil import now_utc
from bot.bonus import (
    _fmt_amount,
    _parse_optional_args,
    format_bonus_overview,
    format_bonus_report,
    format_events_list,
    format_loyalty_accounts,
    format_plans_list,
)

USER_A = 111
USER_B = 222


def _grant(db, uid=USER_A, amount="100", currency="ILS", direction="expense", person="محمد", desc="منحة"):
    return record_bonus_grant(db, uid, amount, currency, direction=direction, person=person, description=desc)


# ---------- تسجيل وتقارير البونس ----------


class TestBonusTransactions:
    def test_grant_creates_expense_with_bonus_category(self, db_session):
        tx = _grant(db_session, amount="250", person=" سامر ")
        assert tx is not None
        assert tx.type == "expense"
        assert tx.category == "بونس"
        assert tx.person == "سامر"
        assert Decimal(str(tx.amount)) == Decimal("250")

    def test_receive_creates_income_with_bonus_category(self, db_session):
        tx = _grant(db_session, amount="90", direction="income", person="أحمد")
        assert tx.type == "income"
        assert tx.category == "بونس"

    def test_grant_currency_normalized(self, db_session):
        tx = _grant(db_session, currency="شيكل")
        assert tx.currency == "ILS"

    def test_list_bonus_transactions_filters_other_categories(self, db_session):
        _grant(db_session, amount="100")
        create_transaction(
            db_session, USER_A,
            {"type": "expense", "amount": 500, "currency": "ILS", "category": "مشتريات"},
            raw_message="مشتريات",
            telegram_message_id=10,
        )
        rows = list_bonus_transactions(db_session, USER_A, period="all_time")
        assert len(rows) == 1
        assert rows[0].category == "بونس"

    def test_bonus_aliases_match_case_insensitively(self, db_session):
        for i, alias in enumerate(("بونس", "بونص", "مكافأة", "bonus", "gift")):
            create_transaction(
                db_session, USER_A,
                {"type": "expense", "amount": 10, "currency": "ILS", "category": alias},
                raw_message=f"alias {i}",
                telegram_message_id=100 + i,
            )
        labels = {r.category for r in list_bonus_transactions(db_session, USER_A, period="all_time")}
        assert labels == {"بونس", "بونص", "مكافأة", "bonus", "gift"}

    def test_report_summary_totals_by_currency_and_counts(self, db_session):
        _grant(db_session, amount="100", currency="ILS")
        _grant(db_session, amount="50", currency="USD", direction="income")
        report = bonus_report_summary(db_session, USER_A, period="all_time")
        assert report["count_expense"] == 1
        assert report["count_income"] == 1
        assert report["expense"]["ILS"] == Decimal("100")
        assert report["income"]["USD"] == Decimal("50")

    def test_report_summary_filters_by_person(self, db_session):
        _grant(db_session, amount="100", person="محمد")
        _grant(db_session, amount="200", person="سامر")
        report = bonus_report_summary(db_session, USER_A, period="all_time", person="محمد")
        assert report["count_expense"] == 1
        assert report["expense"]["ILS"] == Decimal("100")

    def test_workspace_transactions_are_visible(self, db_session):
        from app.database.crud import (
            accept_workspace_invite,
            create_workspace,
            invite_to_workspace,
        )

        _grant(db_session, uid=USER_B, amount="75")
        assert bonus_report_summary(db_session, USER_A, period="all_time")["expense"] == {}
        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        accept_workspace_invite(db_session, USER_B, USER_A)
        report = bonus_report_summary(db_session, USER_A, period="all_time")
        assert report["expense"]["ILS"] == Decimal("75")


# ---------- فعاليات ترويجية ----------


class TestBonusEvents:
    def test_create_event_without_start_is_planned(self, db_session):
        ev = create_bonus_event(db_session, USER_A, "عروض رمضان", budget="500", currency="ILS")
        assert ev is not None
        assert ev.status == "planned"
        assert ev.budget == Decimal("500")

    def test_create_event_with_start_is_active(self, db_session):
        ev = create_bonus_event(
            db_session, USER_A, "افتتاح الفرع",
            budget="1000", currency="USD", start_at=datetime(2026, 1, 1),
        )
        assert ev.status == "active"

    def test_empty_name_returns_none(self, db_session):
        assert create_bonus_event(db_session, USER_A, "  ") is None

    def test_negative_budget_coerced_to_none(self, db_session):
        ev = create_bonus_event(db_session, USER_A, "فعالية", budget="-50")
        assert ev.budget is None

    def test_list_filters_by_status_and_ownership(self, db_session):
        create_bonus_event(db_session, USER_A, "أ", start_at=datetime(2026, 1, 1))
        create_bonus_event(db_session, USER_A, "ب")
        create_bonus_event(db_session, USER_B, "ج")
        assert len(list_bonus_events(db_session, USER_A)) == 2
        assert len(list_bonus_events(db_session, USER_A, status="active")) == 1
        assert [e.name for e in list_bonus_events(db_session, USER_A, status="planned")] == ["ب"]

    def test_set_status_requires_valid_and_ownership(self, db_session):
        ev = create_bonus_event(db_session, USER_A, "أ")
        updated = set_bonus_event_status(db_session, USER_A, ev.id, "active")
        assert updated.status == "active"
        assert set_bonus_event_status(db_session, USER_A, ev.id, "made_up") is None
        assert set_bonus_event_status(db_session, USER_B, ev.id, "active") is None
        assert end_bonus_event(db_session, USER_A, ev.id).status == "ended"

    def test_event_bonus_summary_granted_sales_and_percent(self, db_session):
        now = now_utc()
        ev = create_bonus_event(
            db_session, USER_A, "صيف",
            budget="1000", currency="ILS",
            start_at=now - timedelta(days=10), end_at=now + timedelta(days=10),
        )
        _grant(db_session, amount="250", person="سامر")
        create_transaction(
            db_session, USER_A,
            {"type": "income", "amount": 800, "currency": "ILS", "person": "زبون"},
            raw_message="مبيع صيف",
            telegram_message_id=21,
        )
        s = event_bonus_summary(db_session, ev)
        assert s["granted"]["ILS"] == Decimal("250")
        assert s["sales"]["ILS"] == Decimal("800")
        assert s["percent"] == 25

    def test_event_summary_without_start_returns_placeholders(self, db_session):
        ev = create_bonus_event(db_session, USER_A, "مجدولة فقط", budget="100")
        s = event_bonus_summary(db_session, ev)
        assert s["window"] == (None, None)
        assert s["percent"] is None


# ---------- مكافآت موظفين دورية ----------


class TestEmployeeBonusPlans:
    def test_create_plan_defaults_monthly_enabled(self, db_session):
        plan = create_employee_bonus_plan(db_session, USER_A, "محمد", "1000", currency="ILS")
        assert plan.frequency == "monthly"
        assert plan.enabled is True
        assert plan.amount == Decimal("1000")

    def test_invalid_person_or_amount_returns_none(self, db_session):
        assert create_employee_bonus_plan(db_session, USER_A, "  ", "100") is None
        assert create_employee_bonus_plan(db_session, USER_A, "محمد", "-10") is None
        assert create_employee_bonus_plan(db_session, USER_A, "محمد", "ليس رقما") is None

    def test_custom_frequency_and_cap(self, db_session):
        plan = create_employee_bonus_plan(
            db_session, USER_A, "أحمد", "500", frequency="quarterly", monthly_cap="2000"
        )
        assert plan.frequency == "quarterly"
        assert plan.monthly_cap == Decimal("2000")

    def test_list_excludes_disabled_by_default(self, db_session):
        p1 = create_employee_bonus_plan(db_session, USER_A, "محمد", "100")
        create_employee_bonus_plan(db_session, USER_A, "سامر", "200")
        disable_employee_bonus_plan(db_session, USER_A, p1.id)
        assert [p.person for p in list_employee_bonus_plans(db_session, USER_A)] == ["سامر"]
        assert len(list_employee_bonus_plans(db_session, USER_A, include_disabled=True)) == 2

    def test_disable_plan_preserves_row(self, db_session):
        plan = create_employee_bonus_plan(db_session, USER_A, "محمد", "100")
        disabled = disable_employee_bonus_plan(db_session, USER_A, plan.id)
        assert disabled.enabled is False
        assert disable_employee_bonus_plan(db_session, USER_B, plan.id) is None

    def test_due_plans_only_for_due_and_enabled(self, db_session):
        past = now_utc() - timedelta(days=1)
        future = now_utc() + timedelta(days=10)
        create_employee_bonus_plan(db_session, USER_A, "محمد", "100", next_due_at=past)
        create_employee_bonus_plan(db_session, USER_A, "سامر", "200", next_due_at=future)
        create_employee_bonus_plan(db_session, USER_B, "خالد", "300", next_due_at=past)
        owed = {p.telegram_user_id: p.person for p in due_employee_bonus_plans(db_session)}
        assert {USER_A: "محمد", USER_B: "خالد"} == owed

    def test_due_plan_after_text_removed(self, db_session):
        past = now_utc() - timedelta(days=1)
        create_employee_bonus_plan(db_session, USER_A, "محمد", "100", next_due_at=past)
        _grant(db_session, uid=USER_B, amount="50", person="محمد")
        owed = due_employee_bonus_plans(db_session)
        assert owed[0].telegram_user_id == USER_A

    def test_advance_monthly_rolls_to_next_month(self, db_session):
        now = datetime(2026, 1, 31, 12, 0)
        plan = create_employee_bonus_plan(db_session, USER_A, "محمد", "100", next_due_at=now)
        advance_employee_bonus_due(db_session, plan, now=now)
        assert plan.next_due_at.year == 2026
        assert plan.next_due_at.month == 2
        assert plan.enabled is True

    def test_advance_quarterly(self, db_session):
        now = datetime(2026, 11, 15, 12, 0)
        plan = create_employee_bonus_plan(db_session, USER_A, "محمد", "100", frequency="quarterly", next_due_at=now)
        advance_employee_bonus_due(db_session, plan, now=now)
        assert plan.next_due_at.year == 2027
        assert plan.next_due_at.month == 2
        assert plan.enabled is True

    def test_advance_one_off_disables(self, db_session):
        now = datetime(2026, 1, 1)
        plan = create_employee_bonus_plan(db_session, USER_A, "محمد", "100", frequency="one_off", next_due_at=now)
        advance_employee_bonus_due(db_session, plan, now=now)
        assert plan.enabled is False

    def test_monthly_spent_considers_bonus_expense_only(self, db_session):
        plan = create_employee_bonus_plan(db_session, USER_A, "محمد", "1000")
        _grant(db_session, amount="300", person="محمد")
        _grant(db_session, amount="100", person="سامر")
        create_transaction(
            db_session, USER_A,
            {"type": "expense", "amount": 200, "currency": "ILS", "person": "محمد", "category": "مشتريات"},
            raw_message="مشتريات لمحمد",
            telegram_message_id=30,
        )
        spent = employee_bonus_monthly_spent(db_session, plan)
        assert spent == Decimal("300")

    def test_monthly_spent_boundary_is_local_not_utc(self, db_session):
        """بداية الشهر تُحسب بالتوقيت المحلي (Asia/Gaza) كبقية حسابات الشهر الحالي.

        لحظة منتصف الليل المحلي (أول يوم) تتقدم ساعةً/ساعتين على UTC، أي أنها
        لا تزال في نهاية الشهر السابق بتوقيت UTC. عملية عند تلك اللحظة بالضبط
        يجب أن تُحتسب ضمن هذا الشهر، لا أن تُستبعد بقرار UTC.
        """
        from app.timeutil import now_local, to_utc_naive

        plan = create_employee_bonus_plan(db_session, USER_A, "محمد", "1000")
        local_now = now_local().replace(day=1, hour=0, minute=1)
        month_start_utc = to_utc_naive(
            local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )
        tx = _grant(db_session, amount="250", person="محمد")
        tx.created_at = month_start_utc
        db_session.commit()

        spent = employee_bonus_monthly_spent(db_session, plan, now=local_now)
        assert spent == Decimal("250")

    def test_overview_detects_due_count_and_cap_status(self, db_session):
        now = now_utc()
        create_employee_bonus_plan(db_session, USER_A, "محمد", "1000", monthly_cap="2000", next_due_at=now - timedelta(days=1))
        create_employee_bonus_plan(db_session, USER_A, "سامر", "200", monthly_cap="500")
        _grant(db_session, amount="1800", person="محمد")
        data = employee_bonus_overview(db_session, USER_A)
        by_person = {p["person"]: p for p in data["plans"]}
        assert by_person["محمد"]["cap_status"] == "near"
        assert by_person["سامر"]["cap_status"] == "ok"
        overdue = [p["person"] for p in data["plans"] if p["next_due_at"] is not None and p["next_due_at"] <= now]
        assert overdue == ["محمد"]


# ---------- نقاط الولاء ----------


class TestLoyalty:
    def test_enable_creates_defaults(self, db_session):
        cfg = loyalty_config_enable(db_session, USER_A)
        assert float(cfg.points_rate) == 1.0
        assert float(cfg.points_value) == 0.01
        assert cfg.min_redeem_points == 0

    def test_config_is_per_user(self, db_session):
        loyalty_config_enable(db_session, USER_A, points_rate=2)
        assert loyalty_config_get(db_session, USER_A).points_rate == Decimal("2")
        assert loyalty_config_get(db_session, USER_B) is None

    def test_set_config_updates_existing_only(self, db_session):
        assert set_loyalty_config(db_session, USER_A, points_rate=5) is None
        loyalty_config_enable(db_session, USER_A, points_rate=1)
        cfg = set_loyalty_config(db_session, USER_A, points_rate=5, min_redeem_points=50)
        assert cfg.points_rate == Decimal("5")
        assert cfg.min_redeem_points == 50

    def test_disable_loyalty(self, db_session):
        loyalty_config_enable(db_session, USER_A)
        assert disable_loyalty(db_session, USER_A) is True
        assert disable_loyalty(db_session, USER_A) is False
        assert loyalty_config_get(db_session, USER_A) is None

    def test_add_points_creates_account_and_tallies(self, db_session):
        acc = loyalty_add_points(db_session, USER_A, "أحمد", 40)
        assert acc.points_balance == 40
        assert acc.total_earned == 40
        second = loyalty_add_points(db_session, USER_A, "أحمد", 10)
        assert second.points_balance == 50
        assert second.total_earned == 50
        assert db_session.query(LoyaltyAccount).filter_by(telegram_user_id=USER_A).count() == 1

    def test_manual_negative_points_clamped(self, db_session):
        acc = loyalty_add_points(db_session, USER_A, "أحمد", -5)
        assert acc.points_balance == 0

    def test_accrue_requires_config_and_income_person(self, db_session):
        tx = _grant(db_session, amount="100", direction="income", person="أحمد")
        accrue_loyalty_for_transaction(db_session, tx)
        assert list_loyalty_accounts(db_session, USER_A) == []

        loyalty_config_enable(db_session, USER_A, points_rate=2)
        accrue_loyalty_for_transaction(db_session, tx)
        assert list_loyalty_accounts(db_session, USER_A)[0].points_balance == 200

    def test_accrue_skips_expense(self, db_session):
        loyalty_config_enable(db_session, USER_A)
        tx = _grant(db_session, amount="100", direction="expense", person="أحمد")
        accrue_loyalty_for_transaction(db_session, tx)
        assert list_loyalty_accounts(db_session, USER_A) == []

    def test_accrue_via_create_transaction_hook(self, db_session):
        loyalty_config_enable(db_session, USER_A, points_rate=1)
        create_transaction(
            db_session, USER_A,
            {"type": "income", "amount": 300, "currency": "ILS", "person": "سامر"},
            raw_message="مبيع لسامر",
            telegram_message_id=40,
        )
        acc = list_loyalty_accounts(db_session, USER_A)[0]
        assert acc.person == "سامر"
        assert acc.points_balance == 300

    def test_redeem_success_computes_value(self, db_session):
        loyalty_config_enable(db_session, USER_A, points_rate=1, points_value=0.01)
        loyalty_add_points(db_session, USER_A, "أحمد", 100)
        res = loyalty_redeem_points(db_session, USER_A, "أحمد", 100)
        assert res["ok"] is True
        assert res["value"] == Decimal("1.00")
        assert res["balance"] == 0

    def test_redeem_failures(self, db_session):
        loyalty_config_enable(db_session, USER_A, min_redeem_points=50)
        loyalty_add_points(db_session, USER_A, "أحمد", 30)
        res = loyalty_redeem_points(db_session, USER_A, "أحمد", 10)
        assert res["ok"] is False
        assert "أقل عدد" in res["error"]

        loyalty_config_enable(db_session, USER_A, min_redeem_points=0)
        assert "الرصيد غير كاف" in loyalty_redeem_points(db_session, USER_A, "أحمد", 40)["error"]
        assert "غير مفعّلة" in loyalty_redeem_points(db_session, USER_B, "أحمد", 10)["error"]

    def test_list_orders_by_balance(self, db_session):
        loyalty_add_points(db_session, USER_A, "خالد", 5)
        loyalty_add_points(db_session, USER_A, "أحمد", 50)
        names = [a.person for a in list_loyalty_accounts(db_session, USER_A)]
        assert names == ["أحمد", "خالد"]


# ---------- مشاركة المساحة (workspace) ----------


def _share_workspace(db, owner=USER_A, member=USER_B):
    from app.database.crud import (
        accept_workspace_invite,
        create_workspace,
        invite_to_workspace,
    )

    create_workspace(db, owner)
    invite_to_workspace(db, owner, member)
    accept_workspace_invite(db, member, owner)


class TestBonusWorkspaceSharing:
    def test_events_visible_and_controllable_across_workspace(self, db_session):
        _share_workspace(db_session)
        ev = create_bonus_event(db_session, USER_A, "عروض")
        assert [e.id for e in list_bonus_events(db_session, USER_B)] == [ev.id]
        updated = set_bonus_event_status(db_session, USER_B, ev.id, "active")
        assert updated.status == "active"
        assert end_bonus_event(db_session, USER_B, ev.id).status == "ended"

    def test_employee_plans_shared_across_workspace(self, db_session):
        _share_workspace(db_session)
        create_employee_bonus_plan(db_session, USER_A, "محمد", "100")
        assert [p.person for p in list_employee_bonus_plans(db_session, USER_B)] == ["محمد"]
        plan = create_employee_bonus_plan(db_session, USER_A, "سامر", "200")
        assert disable_employee_bonus_plan(db_session, USER_B, plan.id).enabled is False

    def test_loyalty_config_shared_and_single_row(self, db_session):
        from app.database.models import LoyaltyConfig

        _share_workspace(db_session)
        cfg = loyalty_config_enable(db_session, USER_A, points_rate=3)
        assert cfg.points_rate == Decimal("3")
        assert loyalty_config_get(db_session, USER_B) is not None
        # تفعيل من عضو ثانٍ يحدّث نفس صف المالك (لا صف جديد — قيد uq_loyalty_user)
        again = loyalty_config_enable(db_session, USER_B, min_redeem_points=10)
        assert again.min_redeem_points == 10
        assert loyalty_config_get(db_session, USER_A).min_redeem_points == 10
        assert db_session.query(LoyaltyConfig).count() == 1

    def test_loyalty_account_single_row_across_workspace(self, db_session):
        from app.database.models import LoyaltyAccount

        _share_workspace(db_session)
        loyalty_add_points(db_session, USER_A, "أحمد", 30)
        loyalty_add_points(db_session, USER_B, "أحمد", 20)
        account = loyalty_account_get(db_session, USER_B, "أحمد")
        assert account.points_balance == 50
        assert db_session.query(LoyaltyAccount).filter_by(person="أحمد").count() == 1
        assert account.telegram_user_id == USER_A  # مُثبَّت على صف المالك

    def test_accrue_for_member_sale_uses_workspace_config(self, db_session):
        _share_workspace(db_session)
        loyalty_config_enable(db_session, USER_A, points_rate=2)
        # العضو يسجّل المبيع — tx.telegram_user_id = USER_B
        create_transaction(
            db_session, USER_B,
            {"type": "income", "amount": 100, "currency": "ILS", "person": "زبون"},
            raw_message="مبيع زبون",
            telegram_message_id=50,
        )
        acc = list_loyalty_accounts(db_session, USER_A)[0]
        assert acc.points_balance == 200
        assert list_loyalty_accounts(db_session, USER_B)[0].points_balance == 200

    def test_redeem_from_member_works(self, db_session):
        _share_workspace(db_session)
        loyalty_config_enable(db_session, USER_A, points_rate=1, points_value=0.01)
        loyalty_add_points(db_session, USER_A, "أحمد", 100)
        res = loyalty_redeem_points(db_session, USER_B, "أحمد", 100)
        assert res["ok"] is True
        assert res["value"] == Decimal("1.00")
        assert list_loyalty_accounts(db_session, USER_A)[0].points_balance == 0


# ---------- مسار الأمر الفعلي (_cmd_plan) ----------


class TestBonusPlanCommandPath:
    def _run_cmd_plan(self, db_session, args):
        import asyncio
        from unittest.mock import patch

        from sqlalchemy.orm import sessionmaker

        sent: list[str] = []

        class _Msg:
            async def reply_text(self, text, *a, **k):
                sent.append(text)

        class _Upd:
            def __init__(self):
                self.message = _Msg()

        from bot.bonus import _cmd_plan

        cmd = _cmd_plan
        fac = sessionmaker(bind=db_session.get_bind())
        with patch("bot.bonus.SessionLocal", fac):
            asyncio.run(cmd(_Upd(), USER_A, args))
        return sent

    def test_plan_add_sets_initial_next_due_at(self, db_session):
        """الخطة المنشأة عبر /bonus plan إضافة يجب أن تُستحق فورًا (next_due_at حاضر)."""
        self._run_cmd_plan(db_session, ["add", "محمد", "1000", "monthly"])
        plans = list_employee_bonus_plans(db_session, USER_A)
        assert len(plans) == 1
        assert plans[0].person == "محمد"
        assert plans[0].next_due_at is not None
        assert plans[0].next_due_at <= now_utc()

    def test_plan_add_appears_in_due_plans(self, db_session):
        """بعد إنشاء الخطة عبر الأمر لا بد أن تُلتقط بالفحص التلقائي وتُمنح."""
        self._run_cmd_plan(db_session, ["add", "سامر", "500", "monthly"])
        due = due_employee_bonus_plans(db_session)
        assert any(p.person == "سامر" for p in due)

    def test_plan_add_one_off_is_due_immediately(self, db_session):
        self._run_cmd_plan(db_session, ["add", "خالد", "300", "مرة واحدة"])
        due = due_employee_bonus_plans(db_session)
        assert any(p.person == "خالد" for p in due)

    def test_plan_add_quarterly_is_due_immediately(self, db_session):
        self._run_cmd_plan(db_session, ["add", "أحمد", "900", "ربع سنوي"])
        due = due_employee_bonus_plans(db_session)
        assert any(p.person == "أحمد" for p in due)

    def test_plan_add_multi_word_person(self, db_session):
        """الشخص المكوّن من كلمتين (أبو محمد) يُلتقط كاملاً لا الكلمة الأولى فقط."""
        self._run_cmd_plan(db_session, ["add", "أبو", "محمد", "1000", "monthly"])
        plans = list_employee_bonus_plans(db_session, USER_A)
        assert len(plans) == 1
        assert plans[0].person == "أبو محمد"
        assert plans[0].amount == Decimal("1000")

    def test_plan_add_multi_word_with_cap(self, db_session):
        self._run_cmd_plan(db_session, ["add", "أم", "كلثوم", "2000", "monthly", "500"])
        plans = list_employee_bonus_plans(db_session, USER_A)
        assert len(plans) == 1
        assert plans[0].person == "أم كلثوم"
        assert plans[0].monthly_cap == Decimal("500")


# ---------- مسار الأمر الفعلي (_cmd_event) ----------


class TestBonusEventCommandPath:
    def _run_cmd_event(self, db_session, args):
        import asyncio
        from unittest.mock import patch

        from sqlalchemy.orm import sessionmaker

        sent: list[str] = []

        class _Msg:
            async def reply_text(self, text, *a, **k):
                sent.append(text)

        class _Upd:
            def __init__(self):
                self.message = _Msg()

        from bot.bonus import _cmd_event

        cmd = _cmd_event
        fac = sessionmaker(bind=db_session.get_bind())
        with patch("bot.bonus.SessionLocal", fac):
            asyncio.run(cmd(_Upd(), USER_A, args))
        return sent

    def test_event_add_multi_word_name(self, db_session):
        from app.database.crud import list_bonus_events

        self._run_cmd_event(
            db_session, ["add", "عرض", "رمضان", "5000", "ILS"]
        )
        events = list_bonus_events(db_session, USER_A)
        assert len(events) == 1
        assert events[0].name == "عرض رمضان"
        assert events[0].budget == Decimal("5000")
        assert events[0].currency == "ILS"

    def test_event_add_multi_word_name_without_budget(self, db_session):
        from app.database.crud import list_bonus_events

        self._run_cmd_event(db_session, ["add", "عرض", "رمضان", "ILS"])
        events = list_bonus_events(db_session, USER_A)
        assert len(events) == 1
        assert events[0].name == "عرض رمضان"
        assert events[0].currency == "ILS"

    def test_event_add_multi_word_name_no_currency(self, db_session):
        from app.database.crud import list_bonus_events

        self._run_cmd_event(db_session, ["add", "فعاليـة", "الصيف"])
        events = list_bonus_events(db_session, USER_A)
        assert len(events) == 1
        assert events[0].name == "فعاليـة الصيف"


# ---------- نظرة شاملة ----------


class TestBonusOverview:
    def test_overview_shape_empty(self, db_session):
        data = bonus_overview(db_session, USER_A)
        assert data["events_count"] == 0
        assert data["plans_count"] == 0
        assert data["due_plans_count"] == 0
        assert data["loyalty_enabled"] is False
        assert data["total_points"] == 0

    def test_overview_pulls_everything(self, db_session):
        _grant(db_session, amount="100")
        create_bonus_event(db_session, USER_A, "عروض")
        create_employee_bonus_plan(db_session, USER_A, "محمد", "100")
        loyalty_config_enable(db_session, USER_A)
        loyalty_add_points(db_session, USER_A, "أحمد", 10)
        data = bonus_overview(db_session, USER_A)
        assert data["events_count"] == 1
        assert data["plans_count"] == 1
        assert data["loyalty_enabled"] is True
        assert data["total_points"] == 10
        assert data["report"]["count_expense"] == 1


# ---------- أدوات bot.bonus ----------


class TestBotBonusHelpers:
    def test_fmt_amount(self):
        assert _fmt_amount(Decimal("12.345")) == "12.35"
        assert _fmt_amount(Decimal("12"), "ILS") == "12.00 ILS"
        assert _fmt_amount(None) == "—"

    def test_parse_optional_args(self):
        parsed = _parse_optional_args(["ILS", "محمد", "منحة شهرية"])
        assert parsed == {"currency": "ILS", "person": "محمد", "desc": "منحة شهرية"}
        parsed = _parse_optional_args(["100"])
        assert parsed["currency"] is None and parsed["person"] == "100"

    def test_parse_optional_args_arabic_person_not_currency(self):
        # يصل فقط ما بعد المبلغ (args[2:]) — كما في _cmd_add
        for name in ("علي", "خالد", "سامر", "أحمد", "هدى"):
            parsed = _parse_optional_args([name])
            assert parsed["currency"] is None, name
            assert parsed["person"] == name, name
            assert parsed["desc"] is None
        parsed = _parse_optional_args(["علي", "منحة"])
        assert parsed["currency"] is None and parsed["person"] == "علي"
        assert parsed["desc"] == "منحة"

    def test_parse_optional_args_known_currency_still_parsed(self):
        parsed = _parse_optional_args(["شيكل", "محمد"])
        assert parsed == {"currency": "ILS", "person": "محمد", "desc": None}
        parsed = _parse_optional_args(["USD", "أحمد", "بونس"])
        assert parsed == {"currency": "USD", "person": "أحمد", "desc": "بونس"}
        parsed = _parse_optional_args(["دولار"])
        assert parsed["currency"] == "USD" and parsed["person"] is None

    def test_format_bonus_report(self):
        report = {
            "period": "this_month",
            "expense": {"ILS": Decimal("100")},
            "income": {"USD": Decimal("50")},
            "count_expense": 1,
            "count_income": 1,
            "rows": [],
        }
        text = format_bonus_report(report, person="محمد")
        assert "تقرير البونس" in text
        assert "محمد" in text

    def test_format_events_list_empty(self):
        assert "لا توجد" in format_events_list([])

    def test_format_plans_list_empty(self):
        assert "لا توجد" in format_plans_list([])

    def test_format_loyalty_accounts(self, db_session):
        loyalty_config_enable(db_session, USER_A)
        loyalty_add_points(db_session, USER_A, "أحمد", 10)
        cfg = loyalty_config_get(db_session, USER_A)
        text = format_loyalty_accounts(list_loyalty_accounts(db_session, USER_A), cfg)
        assert "أحمد" in text
        assert "10" in text

    def test_format_bonus_overview(self, db_session):
        data = bonus_overview(db_session, USER_A)
        text = format_bonus_overview(data)
        assert "نقاط الولاء: غير مفعّلة" in text
