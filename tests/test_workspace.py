"""
اختبارات مساحات العمل المشتركة (Shared account): الانضمام، المدى المرئي، الأمان.
"""

from app.database.crud import (
    accessible_user_ids,
    create_transaction,
    create_workspace,
    invite_to_workspace,
    is_workspace_owner,
    leave_workspace,
    list_pending_tasks,
    list_workspace,
    remove_from_workspace,
    run_query,
    workspace_for_user,
)


def _add_tx(db, uid, amount, currency="ILS", kind="expense"):
    create_transaction(
        db,
        uid,
        {"type": kind, "amount": amount, "currency": currency, "description": "اختبار"},
        "اختبار",
    )


class TestIndividualDefault:
    def test_user_without_workspace_sees_only_own_data(self, db_session):
        _add_tx(db_session, 1, 100)
        _add_tx(db_session, 2, 999)
        assert accessible_user_ids(db_session, 1) == {1}
        total = run_query(db_session, 1, {"metric": "total_expenses"})
        assert total["result"]["ILS"] == 100

    def test_create_workspace_is_idempotent(self, db_session):
        row1 = create_workspace(db_session, 7)
        row2 = create_workspace(db_session, 7)
        assert row1.telegram_user_id == row2.telegram_user_id
        assert workspace_for_user(db_session, 7) == 7
        assert is_workspace_owner(db_session, 7)


class TestSharedWorkspace:
    def test_invite_makes_data_visible(self, db_session):
        owner, partner = 10, 20
        create_workspace(db_session, owner)
        _add_tx(db_session, owner, 100)
        _add_tx(db_session, partner, 200)

        # قبل الانضمام: كل طرف يرى نفسه فقط
        assert accessible_user_ids(db_session, partner) == {partner}

        assert invite_to_workspace(db_session, owner, partner) is True
        assert accessible_user_ids(db_session, partner) == {10, 20}
        assert accessible_user_ids(db_session, owner) == {10, 20}

        # بعد المشاركة: الشريك يرى بيانات المالك وميزانياته أيضًا
        total = run_query(db_session, partner, {"metric": "total_expenses"})
        assert total["result"]["ILS"] == 300

    def test_only_owner_can_invite_and_remove(self, db_session):
        owner, member, outsider = 1, 2, 3
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, member)

        # العضو لا يستطيع دعوة أو إخراج أحد
        assert invite_to_workspace(db_session, member, outsider) is False
        assert remove_from_workspace(db_session, member, outsider) is False
        assert outsider not in accessible_user_ids(db_session, member)

        # المالك يستطيع الإخراج
        assert remove_from_workspace(db_session, owner, member) is True
        assert accessible_user_ids(db_session, member) == {member}

    def test_member_cannot_remove_owner_or_owner_cannot_remove_self(self, db_session):
        create_workspace(db_session, 5)
        invite_to_workspace(db_session, 5, 6)
        assert remove_from_workspace(db_session, 5, 5) is False
        assert remove_from_workspace(db_session, 6, 5) is False

    def test_leave_returns_to_individual(self, db_session):
        create_workspace(db_session, 5)
        invite_to_workspace(db_session, 5, 6)
        assert leave_workspace(db_session, 6) is True
        assert workspace_for_user(db_session, 6) is None
        # المالك لا يغادر (لا يوجد مدير بعده)
        assert leave_workspace(db_session, 5) is False

    def test_tasks_scoped_to_workspace(self, db_session):
        owner, partner = 30, 40
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, partner)

        # مهام الطرفين تظهر لكل عضو (شفافية المساحة)
        assert len(list_pending_tasks(db_session, partner)) == 0
        # بيانات فردية للسياق: نبسط — أضف مهمة يدويًا
        from app.database.models import Task

        db_session.add(Task(telegram_user_id=owner, description="مهمة مالك", status="pending"))
        db_session.add(Task(telegram_user_id=partner, description="مهمة شريك", status="pending"))
        db_session.commit()

        found = list_pending_tasks(db_session, partner)
        assert {t.description for t in found} == {"مهمة مالك", "مهمة شريك"}

    def test_list_workspace_report(self, db_session):
        create_workspace(db_session, 50)
        info = list_workspace(db_session, 50)
        assert info["owner"] is True
        assert info["workspace_id"] == 50

        # عضو بلا مساحة → None دون كسر
        assert list_workspace(db_session, 999) is None
