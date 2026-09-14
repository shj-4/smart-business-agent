"""
اختبارات مساحات العمل المشتركة (Shared account): الانضمام بموافقة، المدى المرئي، الأمان.
"""

from app.database.crud import (
    accept_workspace_invite,
    accessible_user_ids,
    create_transaction,
    create_workspace,
    decline_workspace_invite,
    dissolve_workspace,
    invite_to_workspace,
    is_workspace_owner,
    leave_workspace,
    list_pending_tasks,
    list_workspace,
    pending_workspace_invite,
    remove_from_workspace,
    run_query,
    transfer_workspace_ownership,
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
    def test_invite_requires_explicit_acceptance(self, db_session):
        owner, partner = 10, 20
        create_workspace(db_session, owner)
        _add_tx(db_session, owner, 100)
        _add_tx(db_session, partner, 200)

        # قبل الدعوة: كل طرف يرى نفسه فقط
        assert accessible_user_ids(db_session, partner) == {partner}
        assert accessible_user_ids(db_session, owner) == {owner}

        # الدعوة تنشئ صفًا معلّقًا ولا تمنح أي وصول — الثغرة مغلقة
        assert invite_to_workspace(db_session, owner, partner) is True
        assert workspace_for_user(db_session, partner) is None
        assert pending_workspace_invite(db_session, partner) == 10
        assert accessible_user_ids(db_session, partner) == {partner}
        assert accessible_user_ids(db_session, owner) == {owner}

        # بدون قبول لا تختلط البيانات إطلاقًا
        total_pre = run_query(db_session, partner, {"metric": "total_expenses"})
        assert total_pre["result"]["ILS"] == 200

        # قبول صريح من الطرف المدعو → يندمج الاثنان
        assert accept_workspace_invite(db_session, partner, 10) is True
        assert workspace_for_user(db_session, partner) == 10
        assert pending_workspace_invite(db_session, partner) is None
        assert accessible_user_ids(db_session, partner) == {10, 20}
        assert accessible_user_ids(db_session, owner) == {10, 20}

        total = run_query(db_session, partner, {"metric": "total_expenses"})
        assert total["result"]["ILS"] == 300

        # لا قبولُ ثانٍ لدعوة منتهية
        assert accept_workspace_invite(db_session, partner, 10) is False

    def test_pending_invitee_remains_individual_and_can_decline(self, db_session):
        owner, stranger = 11, 22
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, stranger)

        assert workspace_for_user(db_session, stranger) is None
        assert list_workspace(db_session, stranger) is None
        assert accessible_user_ids(db_session, stranger) == {stranger}

        # رفض/إلغاء يزيل كل أثر
        assert decline_workspace_invite(db_session, stranger) is True
        assert pending_workspace_invite(db_session, stranger) is None
        assert decline_workspace_invite(db_session, stranger) is False

        # يمكن إعادة الدعوة بعد الرفض والقبول لاحقًا
        assert invite_to_workspace(db_session, owner, stranger) is True
        assert accept_workspace_invite(db_session, stranger, owner) is True
        assert workspace_for_user(db_session, stranger) == owner

    def test_accept_wrong_workspace_id_is_false(self, db_session):
        owner = 100
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, 101)
        assert accept_workspace_invite(db_session, 101, owner + 999) is False
        assert workspace_for_user(db_session, 101) is None

    def test_only_owner_can_invite_and_remove(self, db_session):
        owner, member, outsider = 1, 2, 3
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, member)
        accept_workspace_invite(db_session, member, owner)

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
        accept_workspace_invite(db_session, 6, 5)
        assert remove_from_workspace(db_session, 5, 5) is False
        assert remove_from_workspace(db_session, 6, 5) is False

    def test_leave_returns_to_individual(self, db_session):
        create_workspace(db_session, 5)
        invite_to_workspace(db_session, 5, 6)
        accept_workspace_invite(db_session, 6, 5)
        assert leave_workspace(db_session, 6) is True
        assert workspace_for_user(db_session, 6) is None
        # المالك لا يغادر (لا يوجد مدير بعده)
        assert leave_workspace(db_session, 5) is False

    def test_tasks_scoped_to_workspace(self, db_session):
        owner, partner = 30, 40
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, partner)
        accept_workspace_invite(db_session, partner, owner)

        # مهام الطرفين تظهر لكل عضو (شفافية المساحة)
        assert len(list_pending_tasks(db_session, partner)) == 0
        # بيانات فردية للسياق: نبسط — أضف مهمة يدويًا
        from app.database.models import Task

        db_session.add(Task(telegram_user_id=owner, description="مهمة مالك", status="pending"))
        db_session.add(Task(telegram_user_id=partner, description="مهمة شريك", status="pending"))
        db_session.commit()

        found = list_pending_tasks(db_session, partner)
        assert {t.description for t in found} == {"مهمة مالك", "مهمة شريك"}

    def test_pending_invitee_tasks_not_visible_to_owner(self, db_session):
        """الدعوة المعلّقة لا تكشف مهام الطرف المدعو للمالك قبل القبول."""
        owner, partner = 30, 41
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, partner)

        from app.database.models import Task

        db_session.add(Task(telegram_user_id=owner, description="مهمة مالك", status="pending"))
        db_session.add(Task(telegram_user_id=partner, description="مهمة خاصة", status="pending"))
        db_session.commit()

        found = list_pending_tasks(db_session, owner)
        assert all(t.description == "مهمة مالك" for t in found)

    def test_list_workspace_report(self, db_session):
        create_workspace(db_session, 50)
        info = list_workspace(db_session, 50)
        assert info["owner"] is True
        assert info["workspace_id"] == 50

        # عضو بلا مساحة → None دون كسر
        assert list_workspace(db_session, 999) is None

    def test_owner_sees_pending_invites_in_list(self, db_session):
        owner = 60
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, 61)
        invite_to_workspace(db_session, owner, 62)

        info = list_workspace(db_session, owner)
        assert info["members"] == [owner]
        assert info["pending"] == [61, 62]

        # قبول أحدهما ينقله من المعلّق إلى الأعضاء
        accept_workspace_invite(db_session, 61, owner)
        info = list_workspace(db_session, owner)
        assert info["members"] == [owner, 61]
        assert info["pending"] == [62]

    def test_owner_can_cancel_pending_invite(self, db_session):
        owner, invitee = 63, 64
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, invitee)
        assert pending_workspace_invite(db_session, invitee) == owner

        # إزالة من المالك تلغي الدعوة قبل قبولها
        assert remove_from_workspace(db_session, owner, invitee) is True
        assert pending_workspace_invite(db_session, invitee) is None
        assert workspace_for_user(db_session, invitee) is None
        assert accessible_user_ids(db_session, invitee) == {invitee}

    def test_invite_refuses_active_member_of_another_workspace(self, db_session):
        """لا يُسحب عضو نشط من مساحة أخرى بلا موافقته — حتى لو أرسل مالك آخر دعوة."""
        owner1, owner2, user = 70, 71, 72
        create_workspace(db_session, owner1)
        create_workspace(db_session, owner2)
        invite_to_workspace(db_session, owner1, user)
        invite_to_workspace(db_session, owner2, user)  # دعوة ثانية فوق المعلّقة جائزة
        accept_workspace_invite(db_session, user, owner2)

        # الآن user عضو نشط في مساحة owner2 — دعوة owner1 أخرى تُرفض
        assert invite_to_workspace(db_session, owner1, user) is False
        assert workspace_for_user(db_session, user) == owner2

    def test_create_workspace_clears_own_pending_invite(self, db_session):
        """إنشاء مساحتك الخاصة يلغي أي دعوة معلّقة سابقة (لا تعارض مفتاح)."""
        stranger_owner = 80
        create_workspace(db_session, stranger_owner)
        invite_to_workspace(db_session, stranger_owner, 81)
        assert pending_workspace_invite(db_session, 81) == stranger_owner

        create_workspace(db_session, 81)
        assert workspace_for_user(db_session, 81) == 81
        assert pending_workspace_invite(db_session, 81) is None
        assert list_workspace(db_session, stranger_owner)["pending"] == []

    def test_write_invalidates_cache_for_all_members(self, db_session):
        """كتابة أي عضو تمسح كاش run_query لكل الأعضاء في المساحة، لا الكاتب فقط."""
        from app.cache import _UNSET, get
        from app.cache import set as cache_set

        owner, partner = 70, 80
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, partner)
        accept_workspace_invite(db_session, partner, owner)

        for uid in (owner, partner):
            cache_set(f"run_query:{uid}:total_expenses:this_month:None", {"ILS": 1.0})

        # العضو يضيف معاملة → يجب أن يُبطل كاش الطرفين
        _add_tx(db_session, partner, 150)

        assert get(f"run_query:{owner}:total_expenses:this_month:None") is _UNSET
        assert get(f"run_query:{partner}:total_expenses:this_month:None") is _UNSET

    def test_write_invalidates_only_affected_cache_namespaces_individually(self, db_session):
        """كدقة جانبية: فرد بلا مساحة يُبطل كاش نفسه فقط ولا يمس غيره."""
        from app.cache import get
        from app.cache import set as cache_set

        cache_set("run_query:90:total_expenses:this_month:None", {"ILS": 5.0})
        cache_set("run_query:91:total_expenses:this_month:None", {"ILS": 6.0})
        _add_tx(db_session, 90, 50)
        assert get("run_query:91:total_expenses:this_month:None") == {"ILS": 6.0}

    def test_remove_invalidates_cache_of_removed_member(self, db_session):
        """عند إخراج عضو: يُبطل كاشه الفردي (حيث بدّلته الإجماليات المشتركة)."""
        from app.cache import _UNSET, get
        from app.cache import set as cache_set

        owner, member = 710, 720
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, member)
        accept_workspace_invite(db_session, member, owner)

        # الكاش المخزن يُمثّل إجماليات المساحة (مبنية على accessible_user_ids)
        for uid in (owner, member):
            cache_set(f"run_query:{uid}:total_expenses:this_month:None", {"ILS": 300.0})

        assert remove_from_workspace(db_session, owner, member) is True
        # العضو المُخرَج لا يبقى يرى إجمالي المساحة حتى انتهاء TTL
        assert get(f"run_query:{member}:total_expenses:this_month:None") is _UNSET
        # المالك أيضًا (تغيّرت مجموعة المساحة)
        assert get(f"run_query:{owner}:total_expenses:this_month:None") is _UNSET

    def test_leave_invalidates_cache_of_leaver(self, db_session):
        """عند مغادرة العضو: يُبطل كاشه الفردي وكاش الباقين في المساحة."""
        from app.cache import _UNSET, get
        from app.cache import set as cache_set

        owner, member = 730, 740
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, member)
        accept_workspace_invite(db_session, member, owner)

        for uid in (owner, member):
            cache_set(f"run_query:{uid}:total_expenses:this_month:None", {"ILS": 300.0})

        assert leave_workspace(db_session, member) is True
        assert get(f"run_query:{member}:total_expenses:this_month:None") is _UNSET
        assert get(f"run_query:{owner}:total_expenses:this_month:None") is _UNSET


class TestWorkspaceOwnershipTransfer:
    def test_transfer_ownership(self, db_session):
        owner, partner = 5, 6
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, partner)
        accept_workspace_invite(db_session, partner, owner)

        assert transfer_workspace_ownership(db_session, owner, partner) is True
        assert is_workspace_owner(db_session, partner) is True
        assert is_workspace_owner(db_session, owner) is False
        assert workspace_for_user(db_session, owner) == partner
        assert workspace_for_user(db_session, partner) == partner

        # المالك الجديد يملك صلاحيات الإدارة — لكن الدعوة لـ 7 معلّقة حتى يقبل
        invite_to_workspace(db_session, partner, 7)
        assert accessible_user_ids(db_session, owner) == {5, 6}
        accept_workspace_invite(db_session, 7, partner)
        assert accessible_user_ids(db_session, owner) == {5, 6, 7}

    def test_transfer_refuses_non_member_or_non_owner(self, db_session):
        create_workspace(db_session, 5)
        invite_to_workspace(db_session, 5, 6)
        accept_workspace_invite(db_session, 6, 5)
        assert transfer_workspace_ownership(db_session, 6, 5) is False  # عضو ليس مالكًا
        assert transfer_workspace_ownership(db_session, 5, 99) is False  # خارج المساحة
        assert transfer_workspace_ownership(db_session, 5, 5) is False  # مالك لنفسه

    def test_transfer_refuses_pending_invitee(self, db_session):
        owner = 501
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, 502)
        assert transfer_workspace_ownership(db_session, owner, 502) is False
        assert is_workspace_owner(db_session, owner)

    def test_dissolve_workspace_returns_all_to_individual(self, db_session):
        owner, partner = 5, 6
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, partner)
        accept_workspace_invite(db_session, partner, owner)

        assert dissolve_workspace(db_session, partner) is False  # ليس مالكًا
        assert dissolve_workspace(db_session, owner) is True
        assert workspace_for_user(db_session, owner) is None
        assert workspace_for_user(db_session, partner) is None
        assert accessible_user_ids(db_session, owner) == {owner}

    def test_dissolve_clears_pending_invites(self, db_session):
        owner = 504
        create_workspace(db_session, owner)
        invite_to_workspace(db_session, owner, 505)
        invite_to_workspace(db_session, owner, 506)
        assert dissolve_workspace(db_session, owner) is True
        assert pending_workspace_invite(db_session, 505) is None
        assert pending_workspace_invite(db_session, 506) is None

    def test_dissolve_refuses_non_owner(self, db_session):
        create_workspace(db_session, 5)
        assert dissolve_workspace(db_session, 6) is False
