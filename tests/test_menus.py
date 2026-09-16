"""
اختبارات تجربة الأزرار (menus): قوائم InlineKeyboard + Router + تعديل حقول
التأكيد + إدارة المهام عبر الأزرار (إنجاز/حذف) — unit tests بلا شبكة.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from telegram import InlineKeyboardMarkup

from app.database.crud import (
    complete_task,
    create_task,
    delete_task_by_id,
    list_done_tasks,
)
from app.timeutil import now_utc
from bot import menus
from bot.conversation import (
    _confirm_editable_fields,
    apply_confirm_edit,
    is_greeting,
    parse_budget_text,
    parse_convert_text,
    parse_rate_question,
    workspace_invite_value,
)

USER_A = 111
USER_B = 222


def _seed_task(db, desc="الاتصال بسامر", user=USER_A):
    return create_task(
        db,
        user,
        {"description": desc, "person": "سامر", "date": (now_utc() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")},
        raw_message=f"مهمة {desc}",
    )


def _find_button(kb: InlineKeyboardMarkup, callback_data: str):
    for row in kb.inline_keyboard:
        for btn in row:
            if btn.callback_data == callback_data:
                return btn
    return None


def _build_query(data: str, user_id: int = USER_A):
    edited = []

    class _Query:
        def __init__(self):
            self.data = data
            self.from_user = SimpleNamespace(id=user_id)
            self.answered = False

        async def answer(self, *args, **kwargs):
            self.answered = True

        async def edit_message_text(self, text, **kwargs):
            edited.append((text, kwargs.get("reply_markup")))

    return _Query(), edited


def _context(user_data=None):
    return SimpleNamespace(user_data=user_data or {})


def _run(coro):
    return asyncio.run(coro)


class TestBuildMenu:
    def test_build_menu_rows_and_buttons(self):
        kb = menus.build_menu(
            [
                [("أ", "x:a"), ("ب", "x:b")],
                [("ج", "x:c")],
            ]
        )
        rows = kb.inline_keyboard
        assert len(rows) == 2
        assert [b.text for b in rows[0]] == ["أ", "ب"]
        assert [b.callback_data for b in rows[0]] == ["x:a", "x:b"]
        assert rows[1][0].callback_data == "x:c"

    def test_main_menu_has_five_entries(self):
        kb = menus.build_menu([list(r) for r in menus.MAIN_MENU])
        rows = kb.inline_keyboard
        assert len(rows) == 5
        assert _find_button(kb, "menu:record")
        assert _find_button(kb, "menu:reports")
        assert _find_button(kb, "menu:tasks")
        assert _find_button(kb, "menu:tools")
        assert _find_button(kb, "menu:settings")

    def test_main_menu_dynamic_task_badge(self, db_session, monkeypatch):
        """زر المهام يظهر badge بعدد المتأخرة عند وجود مهام متأخرة."""
        from datetime import datetime

        from app.timeutil import to_utc_naive

        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        past = to_utc_naive(datetime(2020, 1, 1, 8, 0))
        task = create_task(
            db_session,
            USER_A,
            {"description": "متأخرة قديمة", "date": past.strftime("%Y-%m-%d %H:%M")},
            raw_message="متأخرة",
        )
        task.status = "overdue"
        db_session.commit()

        kb = menus._main_menu_keyboard(uid=USER_A)
        btn = _find_button(kb, "menu:tasks")
        assert "متأخرة" in btn.text

    def test_main_menu_task_badge_hides_without_overdue(self, db_session, monkeypatch):
        """لا تظهر أي شارة عند عدم وجود مهام للمستخدم."""
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        kb = menus._main_menu_keyboard(uid=USER_A)
        btn = _find_button(kb, "menu:tasks")
        assert "مهام" in btn.text
        assert "متأخرة" not in btn.text


class TestCallbackRouter:
    def test_unknown_action_passes_through(self):
        q, _ = _build_query("confirm:yes")
        out = _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert out is None
        assert not q.answered  # router لا يعترض callbacks المحادثة

    def test_menu_main_navigation_clears_pending(self):
        q, edited = _build_query("menu:main")
        ctx = _context({"confirm_result": {"type": "expense"}})
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert edited and "القائمة الرئيسية" in edited[0][0]
        assert edited[0][1] is not None
        assert ctx.user_data.get("confirm_result") is None

    def test_record_sets_seed_and_hints(self):
        q, edited = _build_query("rec:expense")
        ctx = _context()
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert ctx.user_data["record_seed_type"] == "expense"
        assert "مصروف" in edited[0][0]

    def test_report_metrics_buttons_carry_period(self):
        q, edited = _build_query("rpt:p:this_month")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        kb = edited[0][1]
        assert _find_button(kb, "rpt:m:total_expenses:this_month")
        assert _find_button(kb, "rpt:m:total_income:this_month")
        assert _find_button(kb, "rpt:m:count_transactions:this_month")

    def test_report_result_shows_query_output(self, monkeypatch, db_session):
        import asyncio

        q, edited = _build_query("rpt:m:total_expenses:this_month")

        class _Result:
            pass

        def fake_run_query(db, uid, details):
            return {"metric": "total_expenses", "period": "this_month", "result": {}}

        monkeypatch.setattr(menus, "run_query", fake_run_query)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))

        async def go():
            await menus.menu_callback_router(SimpleNamespace(callback_query=q), _context())

        asyncio.run(go())
        assert edited and "📊" in edited[0][0]


def _session_only(db):
    return db


class TestConfirmEditHelpers:
    def test_editable_fields_by_type(self):
        # نفس قائمة حقول /edit (editing.EDITABLE_FIELDS) حسب النوع
        assert _confirm_editable_fields({"type": "expense"}) == [
            "amount",
            "currency",
            "person",
            "category",
            "description",
        ]
        assert _confirm_editable_fields({"type": "task"}) == [
            "description",
            "person",
            "date",
            "priority",
        ]
        assert _confirm_editable_fields({"type": "note"}) == ["description", "person", "category"]

    def test_amount_with_currency(self):
        result, err = apply_confirm_edit({"type": "expense"}, "amount", "300 شيكل")
        assert err is None
        assert float(result["amount"]) == 300.0
        assert result["currency"] == "ILS"

    def test_amount_decimal_comma(self):
        result, err = apply_confirm_edit({"type": "expense"}, "amount", "12,5")
        assert err is None
        assert float(result["amount"]) == 12.5

    def test_amount_invalid(self):
        result, err = apply_confirm_edit({"type": "expense"}, "amount", "الف")
        assert err is not None
        assert result.get("amount") is None

    def test_person_free_text(self):
        result, err = apply_confirm_edit({"type": "expense"}, "person", "محمود")
        assert err is None and result["person"] == "محمود"

    def test_empty_description_rejected(self):
        result, err = apply_confirm_edit({"type": "note"}, "description", "  ")
        assert err is not None

    def test_date_structured_iso_kept_raw(self, monkeypatch):
        import app.ai_service as ai_service

        calls = []
        monkeypatch.setattr(ai_service, "interpret_arabic_date", lambda v: calls.append(v) or "x")
        result, err = apply_confirm_edit({"type": "task"}, "date", "2026-09-10 10:00")
        assert err is None
        assert result["date"] == "2026-09-10 10:00"
        assert calls == []  # الصيغة الصريحة لا تستدعي الذكاء الاصطناعي

    def test_date_natural_language_falls_back_to_ai(self, monkeypatch):
        import app.ai_service as ai_service

        monkeypatch.setattr(ai_service, "interpret_arabic_date", lambda v: "2026-09-11 09:00")
        result, err = apply_confirm_edit({"type": "task"}, "date", "بكرة الساعة 10")
        assert err is None
        assert result["date"] == "2026-09-11 09:00"

    def test_date_ai_failure_returns_error_not_silent_loss(self, monkeypatch):
        import app.ai_service as ai_service

        monkeypatch.setattr(ai_service, "interpret_arabic_date", lambda v: None)
        result, err = apply_confirm_edit({"type": "task"}, "date", "ما فهمت هالموعد")
        assert err is not None
        assert "date" not in result

    def test_empty_date_rejected(self):
        result, err = apply_confirm_edit({"type": "task"}, "date", "  ")
        assert err is not None


class TestMenuTaskActions:
    def test_delete_task_by_id(self, db_session):
        task = _seed_task(db_session)
        deleted = delete_task_by_id(db_session, USER_A, task.id)
        assert deleted is not None and deleted.id == task.id
        # الوظيفة مخصصة للمهام غير المنجزة فقط
        assert delete_task_by_id(db_session, USER_A, task.id) is None

    def test_delete_task_scoped_to_user(self, db_session):
        task = _seed_task(db_session, user=USER_A)
        assert delete_task_by_id(db_session, USER_B, task.id) is None

    def test_list_done_excludes_pending(self, db_session):
        task = _seed_task(db_session)
        assert list_done_tasks(db_session, USER_A) == []
        complete_task(db_session, USER_A, task.id)
        assert [t.id for t in list_done_tasks(db_session, USER_A)] == [task.id]

    def test_build_task_list_buttons(self, db_session):
        task = _seed_task(db_session)
        text, kb = menus.build_task_list([task], "pending")
        assert "الاتصال بسامر" in text
        assert _find_button(kb, f"tsk:done:{task.id}")
        assert _find_button(kb, f"tsk:edit:{task.id}")
        assert _find_button(kb, f"tsk:del:{task.id}")

    def test_build_task_list_empty(self):
        text, kb = menus.build_task_list([], "pending")
        assert "لا توجد" in text
        assert _find_button(kb, "menu:main")

    def test_build_task_list_done_shows_only_delete(self, db_session):
        task = _seed_task(db_session)
        complete_task(db_session, USER_A, task.id)
        _, kb = menus.build_task_list([task], "done")
        assert _find_button(kb, f"tsk:del:{task.id}")
        assert not _find_button(kb, f"tsk:done:{task.id}")


class TestTaskEditAndDoneHandlers:
    def test_task_done_via_button(self, db_session, monkeypatch):
        q, edited = _build_query(f"tsk:done:{_seed_task(db_session).id}")
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "تم إنجاز" in edited[0][0]

    def test_task_delete_via_button(self, db_session, monkeypatch):
        q, edited = _build_query(f"tsk:del:{_seed_task(db_session).id}")
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "حُذفت" in edited[0][0]

    def test_task_list_via_button(self, db_session, monkeypatch):
        _seed_task(db_session)
        q, edited = _build_query("tsk:list:pending")
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "الاتصال بسامر" in edited[0][0]
        assert _find_button(edited[0][1], "menu:tasks")


class TestBudgetConvertParsing:
    def test_parse_budget_person(self):
        parsed = parse_budget_text("person", "محمد 1500")
        assert parsed == {
            "scope": "person",
            "target": "محمد",
            "monthly_limit": Decimal("1500"),
        }

    def test_parse_budget_currency(self):
        parsed = parse_budget_text("currency", "ILS 2000")
        assert parsed["scope"] == "currency"
        assert parsed["target"] == "ILS"
        assert float(parsed["monthly_limit"]) == 2000.0

    def test_parse_budget_currency_by_arabic_alias(self):
        parsed = parse_budget_text("currency", "شيكل 2000")
        assert parsed["target"] == "ILS"

    def test_parse_budget_invalid(self):
        assert parse_budget_text("currency", "2000") is None
        assert parse_budget_text("person", "بدون رقم") is None

    def test_parse_budget_tolerates_prefix(self):
        parsed = parse_budget_text("person", "شخص: محمد 1500")
        assert parsed["target"] == "محمد"
        parsed = parse_budget_text("currency", "عملة: شيكل 2000")
        assert parsed["target"] == "ILS"

    def test_parse_convert_codes(self):
        parsed = parse_convert_text("300 ILS إلى USD")
        assert float(parsed["amount"]) == 300.0
        assert parsed["from"] == "ILS"
        assert parsed["to"] == "USD"

    def test_parse_convert_arabic_names(self):
        parsed = parse_convert_text("500 دولار إلى شيكل")
        assert float(parsed["amount"]) == 500.0
        assert parsed["from"] == "USD"
        assert parsed["to"] == "ILS"

    def test_parse_convert_invalid(self):
        assert parse_convert_text("كم سعر الصرف؟") is None
        assert parse_convert_text("") is None

    def test_parse_rate_question(self):
        parsed = parse_rate_question("كم صرف شيكل على دينار الأردني")
        assert parsed is not None
        assert parsed["from"] == "ILS"
        assert parsed["to"] == "JOD"

    def test_parse_rate_question_usd(self):
        parsed = parse_rate_question("كم سعر الدولار مقابل الشيكل؟")
        assert parsed is not None
        assert parsed["from"] == "USD"
        assert parsed["to"] == "ILS"

    def test_parse_rate_question_no_amount_needed(self):
        assert parse_rate_question("كم صرف يورو إلى دينار") is not None

    def test_parse_rate_question_normal_queries_excluded(self):
        assert parse_rate_question("كم صرفت هذا الشهر") is None
        assert parse_rate_question("دفعت 300 شيكل لمحمد") is None
        assert parse_rate_question("كم لي عند محمد؟") is None
        assert parse_rate_question("") is None

    def test_is_greeting(self):
        assert is_greeting("مرحبا")
        assert is_greeting("أهلا بك")
        assert is_greeting("hi")
        assert is_greeting("السلام عليكم")
        assert not is_greeting("كم صرف شيكل على دينار")
        assert not is_greeting("دفعت 300 شيكل لمحمد")


class TestExportAndTools:
    def test_export_period_label_and_start(self):
        start, label = menus.export_period_start("today")
        assert label == "today"
        assert start is not None
        start, label = menus.export_period_start("all")
        assert label == "all"
        assert start is None

    def test_export_menu_period_buttons(self):
        q, edited = _build_query("ex:menu")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert _find_button(edited[0][1], "ex:p:all")
        assert _find_button(edited[0][1], "ex:p:month")

    def test_tools_convert_sets_pending(self):
        q, edited = _build_query("tool:convert")
        ctx = _context()
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert ctx.user_data.get("pending_convert") is True
        assert "تحويل" in edited[0][0]

    def test_tools_page_via_menu(self):
        q, edited = _build_query("menu:tools")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "أدوات" in edited[0][0]
        assert _find_button(edited[0][1], "bg:list")
        assert _find_button(edited[0][1], "tool:chart")
        assert _find_button(edited[0][1], "tool:convert")
        assert _find_button(edited[0][1], "ws:status")
        assert _find_button(edited[0][1], "el:last")


class TestBudgetButtons:
    def test_budget_list_shows_item_and_add_buttons(self, db_session, monkeypatch):
        from app.database.crud import create_budget

        budget = create_budget(db_session, USER_A, "currency", "ILS", "2000")
        q, edited = _build_query("bg:list")
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "ميزانياتك" in edited[0][0]
        assert _find_button(edited[0][1], "bg:add:currency")
        assert _find_button(edited[0][1], "bg:add:person")
        assert _find_button(edited[0][1], f"bg:del:{budget.id}")

    def test_budget_add_sets_pending(self):
        q, edited = _build_query("bg:add:currency")
        ctx = _context()
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert ctx.user_data.get("pending_budget") == {"scope": "currency"}
        assert "ILS 2000" in edited[0][0]

    def test_budget_delete_via_button(self, db_session, monkeypatch):
        from app.database.crud import create_budget

        budget = create_budget(db_session, USER_A, "currency", "ILS", "2000")
        q, edited = _build_query(f"bg:del:{budget.id}")
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "حُذفت" in edited[0][0]

    def test_budget_category_button_add_and_list(self, db_session, monkeypatch):
        from app.database.crud import create_budget

        create_budget(db_session, USER_A, "category", "مشتريات", "2000")
        q, edited = _build_query("bg:list")
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert _find_button(edited[0][1], "bg:add:category")
        assert "التصنيف مشتريات" in edited[0][0]

    def test_budget_category_add_sets_pending(self):
        q, edited = _build_query("bg:add:category")
        ctx = _context()
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert ctx.user_data.get("pending_budget") == {"scope": "category"}
        assert "مشتريات 2000" in edited[0][0]


class TestDebtsInvoicesOrdersButtons:
    def test_tool_debts_lists_balances(self, db_session, monkeypatch):
        from app.database.crud import create_transaction

        create_transaction(
            db_session, USER_A, {"type": "expense", "amount": 300, "currency": "ILS", "person": "سامر"}, "دفعة"
        )
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("tool:debts")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "سامر" in edited[0][0]

    def test_tool_invoices_shows_and_pay_button_works(self, db_session, monkeypatch):
        from app.database.crud import create_invoice

        inv = create_invoice(
            db_session, USER_A, {"person": "مورّد", "amount": 500, "currency": "ILS"}, "فاتورة"
        )
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("tool:invoices")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "مورّد" in edited[0][0]
        assert _find_button(edited[0][1], f"inv:pay:{inv.id}")

        q2, edited2 = _build_query(f"inv:pay:{inv.id}")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q2), _context()))
        assert "سُدّدت" in edited2[0][0]

    def test_tool_orders_done_button(self, db_session, monkeypatch):
        from app.database.crud import create_note

        note = create_note(
            db_session, USER_A, {"type": "order", "description": "مواد من المورّد"}, "طلبية مواد"
        )
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("tool:orders")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "مواد من المورّد" in edited[0][0]
        assert _find_button(edited[0][1], f"ord:done:{note.id}")

        q2, edited2 = _build_query(f"ord:done:{note.id}")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q2), _context()))
        assert "أُنجز" in edited2[0][0]

    def test_tool_credit_shows_limits(self, db_session, monkeypatch):
        from app.database.crud import set_credit_limit

        set_credit_limit(db_session, USER_A, "خالد", "5000")
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("tool:credit")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "خالد" in edited[0][0]


class TestWorkspaceButtons:
    def test_workspace_status_empty_offers_create(self):
        q, edited = _build_query("ws:status")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "لا تملك مساحة مشتركة" in edited[0][0]
        assert _find_button(edited[0][1], "ws:new")

    def test_workspace_create_then_status(self, db_session, monkeypatch):
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("ws:new")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "مساحة العمل الحالية" in edited[-1][0]
        assert _find_button(edited[-1][1], "ws:members")
        assert _find_button(edited[-1][1], "ws:add")

    def test_workspace_members_owner_remove_buttons(self, db_session, monkeypatch):
        from app.database.crud import accept_workspace_invite, create_workspace, invite_to_workspace

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        accept_workspace_invite(db_session, USER_B, USER_A)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("ws:members")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "الأعضاء (2)" in edited[0][0]
        assert _find_button(edited[0][1], f"ws:rm:{USER_B}")
        assert _find_button(edited[0][1], f"ws:rm:{USER_A}") is None

    def test_workspace_member_see_leave_not_remove(self, db_session, monkeypatch):
        from app.database.crud import accept_workspace_invite, create_workspace, invite_to_workspace

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        accept_workspace_invite(db_session, USER_B, USER_A)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("ws:status", user_id=USER_B)
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "عضو" in edited[0][0]
        assert _find_button(edited[0][1], "ws:leave")
        assert _find_button(edited[0][1], "ws:rm:") is None

    def test_workspace_leave_via_button(self, db_session, monkeypatch):
        from app.database.crud import accept_workspace_invite, create_workspace, invite_to_workspace

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        accept_workspace_invite(db_session, USER_B, USER_A)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("ws:leave", user_id=USER_B)
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "لا تملك مساحة مشتركة" in edited[-1][0]

    def test_workspace_add_sets_pending(self):
        q, edited = _build_query("ws:add")
        ctx = _context()
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert ctx.user_data.get("pending_ws_invite") is True
        assert "Telegram ID" in edited[0][0]

    def test_workspace_invite_value(self, db_session, monkeypatch):
        from app.database.crud import create_workspace, pending_workspace_invite, workspace_for_user

        create_workspace(db_session, USER_A)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))

        replies = []

        class _Message:
            text = str(USER_B)

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        import bot.conversation as conversation

        monkeypatch.setattr(conversation, "SessionLocal", lambda: _session_only(db_session))
        _run(
            workspace_invite_value(
                SimpleNamespace(message=_Message(), effective_user=SimpleNamespace(id=USER_A)),
                _context(),
            )
        )
        # الدعوة تبقى معلّقة — لا تصبح عضوًا ولا يُدمج شيء قبل القبول
        assert workspace_for_user(db_session, USER_B) is None
        assert pending_workspace_invite(db_session, USER_B) == USER_A
        assert "دعوة" in replies[0]

    def test_workspace_pending_invite_card(self, db_session, monkeypatch):
        from app.database.crud import create_workspace, invite_to_workspace

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))

        q, edited = _build_query("ws:status", user_id=USER_B)
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "دعوة انضمام" in edited[0][0]
        assert _find_button(edited[0][1], f"ws:accept:{USER_A}")
        assert _find_button(edited[0][1], f"ws:decline:{USER_A}")

    def test_workspace_accept_via_button(self, db_session, monkeypatch):
        from app.database.crud import (
            create_workspace,
            invite_to_workspace,
            workspace_for_user,
        )

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))

        q, edited = _build_query(f"ws:accept:{USER_A}", user_id=USER_B)
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert q.answered
        assert workspace_for_user(db_session, USER_B) == USER_A
        assert "عضو" in edited[0][0]
        assert _find_button(edited[0][1], "ws:leave")

    def test_workspace_decline_via_button(self, db_session, monkeypatch):
        from app.database.crud import (
            create_workspace,
            invite_to_workspace,
            pending_workspace_invite,
            workspace_for_user,
        )

        create_workspace(db_session, USER_A)
        invite_to_workspace(db_session, USER_A, USER_B)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))

        q, edited = _build_query(f"ws:decline:{USER_A}", user_id=USER_B)
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert q.answered
        assert pending_workspace_invite(db_session, USER_B) is None
        assert workspace_for_user(db_session, USER_B) is None
        assert "لا تملك مساحة مشتركة" in edited[0][0]

    def test_workspace_accept_is_noop_without_invite(self, db_session, monkeypatch):
        from app.database.crud import workspace_for_user

        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query(f"ws:accept:{USER_A}", user_id=USER_B)
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert workspace_for_user(db_session, USER_B) is None


class TestEditLastFlow:
    def test_edit_last_shows_fields(self, db_session, monkeypatch):
        from app.database.crud import create_transaction

        create_transaction(
            db_session,
            USER_A,
            {
                "amount": 300,
                "type": "expense",
                "currency": "ILS",
                "person": "محمد",
                "description": "مواد",
            },
            raw_message="300 شيكل لمحمد مواد",
        )
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("el:last")
        ctx = _context()
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert "تعديل السجل" in edited[0][0]
        assert _find_button(edited[0][1], "rf:Transaction:amount")
        assert _find_button(edited[0][1], "rf:Transaction:description")
        assert _find_button(edited[0][1], "rf:end")
        assert ctx.user_data.get("pending_record_edit_model") == "Transaction"

    def test_record_field_sets_pending(self, db_session, monkeypatch):
        from app.database.crud import create_transaction

        tx = create_transaction(
            db_session,
            USER_A,
            {"amount": 300, "type": "expense", "currency": "ILS", "description": "مواد"},
            raw_message="300 شيكل",
        )
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        ctx = _context()
        _run(
            menus.menu_callback_router(
                SimpleNamespace(callback_query=_build_query("el:last")[0]), ctx
            )
        )
        q2, edited = _build_query("rf:Transaction:amount")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q2), ctx))
        assert ctx.user_data.get("pending_record_edit_field") == "amount"
        assert "300" in edited[0][0]
        assert ctx.user_data.get("pending_record_edit_id") == tx.id

    def test_record_edit_value_updates_db(self, db_session, monkeypatch):
        import bot.conversation as conversation
        from app.database.crud import create_transaction
        from app.database.models import Transaction
        from bot.conversation import record_edit_value

        tx = create_transaction(
            db_session,
            USER_A,
            {"amount": 300, "type": "expense", "currency": "ILS", "description": "مواد"},
            raw_message="300 شيكل",
        )
        monkeypatch.setattr(conversation, "SessionLocal", lambda: _session_only(db_session))

        replies = []

        class _Msg:
            text = "450"

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        _run(
            record_edit_value(
                SimpleNamespace(message=_Msg(), effective_user=SimpleNamespace(id=USER_A)),
                _context(
                    {
                        "pending_record_edit_id": tx.id,
                        "pending_record_edit_model": "Transaction",
                        "pending_record_edit_field": "amount",
                    }
                ),
            )
        )
        updated = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
        assert updated.amount == Decimal("450")
        assert "تم تحديث" in replies[0]

    def test_edit_last_no_records(self, db_session, monkeypatch):
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("el:last")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "لا توجد سجلات" in edited[0][0]

    def test_task_line_priority_and_recurrence_badges(self, db_session):
        from app.database.crud import create_task

        task = create_task(
            db_session,
            USER_A,
            {"description": "عاجلة", "priority": "high", "recurrence": "weekly"},
            raw_message="كل أسبوع عاجلة",
        )
        line = menus._task_line(task)
        assert "⚡" in line
        assert "🔁" in line


class TestHistorySearchAndLang:
    def _seed_tasks(self, db, n=6, person="سامر"):
        from app.database.crud import create_task

        created = []
        for i in range(n):
            created.append(
                create_task(
                    db,
                    USER_A,
                    {"description": f"مهمة {i}", "person": person, "date": "2026-09-10 10:00"},
                    raw_message=f"task {i}",
                )
            )
        return created

    def test_history_page_shows_pagination(self, db_session, monkeypatch):
        self._seed_tasks(db_session)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("his:p:1")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "صفحة 1/2" in edited[0][0]
        assert _find_button(edited[0][1], "his:p:2")
        assert _find_button(edited[0][1], "rb:e:Task:6")

        q2, edited2 = _build_query("his:p:2")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q2), _context()))
        assert "صفحة 2/2" in edited2[0][0]
        assert _find_button(edited2[0][1], "his:p:1")
        assert _find_button(edited2[0][1], "rb:e:Task:1")

    def test_record_action_edit_and_delete(self, db_session, monkeypatch):
        from app.database.crud import list_recent_records

        task = self._seed_tasks(db_session, n=1)[0]
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))

        q, edited = _build_query(f"rb:e:Task:{task.id}")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "تعديل السجل" in edited[0][0]
        assert _find_button(edited[0][1], "rf:Task:description")

        qd, edited_d = _build_query(f"rb:d:Task:{task.id}")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=qd), _context()))
        db = db_session
        remaining = db.query(type(task)).filter(type(task).id == task.id).first()
        assert remaining is not None and remaining.deleted_at is not None
        recs = list_recent_records(db, USER_A, limit=50)
        assert recs == []

    def test_search_start_asks_term(self, db_session, monkeypatch):
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("sb:start")
        ctx = _context()
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert ctx.user_data.get("pending_search") is True
        assert "كلمة البحث" in edited[0][0]

    def test_search_page_uses_term(self, db_session, monkeypatch):
        self._seed_tasks(db_session, n=3, person="محمد")
        self._seed_tasks(db_session, n=2, person="سامر")
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("sr:p:1")
        ctx = _context({"pending_search_term": "محمد"})
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert "نتائج البحث" in edited[0][0]
        text, _ = edited[0]
        assert text.count("👤 محمد") == 3
        assert _find_button(edited[0][1], "rb:d:Task:1")

    def test_lang_toggle_to_english(self, db_session, monkeypatch):
        from bot import i18n

        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query("ln:en")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), _context()))
        assert "English" in edited[0][0]
        assert i18n.user_lang(USER_A) == "en"

        q2, edited2 = _build_query("menu:main")
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q2), _context()))
        assert "Main menu" in edited2[0][0]
        assert _find_button(edited2[0][1], "ln:ar") or _find_button(edited2[0][1], "menu:settings")

    def test_tools_menu_has_history_and_search_buttons(self):
        kb = menus._tools_keyboard("ar")
        assert _find_button(kb, "his:p:1")
        assert _find_button(kb, "sb:start")


class TestPendingClearedOnNewFlow:
    """بدء أي تدفق معلّق جديد يمسح كل الأعلام المعلّقة القديمة أولًا.

    يمنع هذا تضارب أعلام pending_* المتزامنة: لو بقي علم تدفق قديم (مثل
    pending_convert) وضُبط رسم جديد (مثل pending_task_edit_id) لابتُلع نص
    المستخدم التالي في المسار الخاطئ — وقد يُستبدل وصف مهمة فعلية بنص غير
    مقصود (تلف بيانات). القاعدة: كل زر يضبط pending_* يستدعي
    _clear_all_pending(context) قبل ضبط علمه.
    """

    def assert_single_pending(self, ctx, key, value):
        pending = {k: v for k, v in ctx.user_data.items() if k.startswith("pending_")}
        pending.pop("pending_search_term", None)
        assert pending == {key: value}

    def test_task_edit_clears_stale_convert(self, db_session, monkeypatch):
        task = _seed_task(db_session)
        monkeypatch.setattr(menus, "SessionLocal", lambda: _session_only(db_session))
        q, edited = _build_query(f"tsk:edit:{task.id}")
        ctx = _context({"pending_convert": True})
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        self.assert_single_pending(ctx, "pending_task_edit_id", task.id)

    def test_convert_clears_stale_task_edit(self):
        q, edited = _build_query("tool:convert")
        ctx = _context({"pending_task_edit_id": 5})
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        self.assert_single_pending(ctx, "pending_convert", True)

    def test_budget_add_clears_stale_convert(self):
        q, edited = _build_query("bg:add:currency")
        ctx = _context({"pending_convert": True})
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        self.assert_single_pending(ctx, "pending_budget", {"scope": "currency"})

    def test_workspace_add_clears_stale_convert(self):
        q, edited = _build_query("ws:add")
        ctx = _context({"pending_convert": True})
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        self.assert_single_pending(ctx, "pending_ws_invite", True)

    def test_record_seed_clears_stale_convert(self):
        q, edited = _build_query("rec:expense")
        ctx = _context({"pending_convert": True})
        _run(menus.menu_callback_router(SimpleNamespace(callback_query=q), ctx))
        assert all(k.startswith("pending_") is False for k in ctx.user_data)
        assert ctx.user_data.get("record_seed_type") == "expense"
