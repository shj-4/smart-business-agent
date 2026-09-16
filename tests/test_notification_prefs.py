"""
اختبارات تفضيلات الإشعارات القابلة للتخصيص (UserPref.notif_*).

- الحقول السبعة موجودة بقيم افتراضية True.
- get_or_create_user_pref يُنشئ سجلًا عند غيابه.
- toggle_notification_pref يُحدّث كل حقل ويشغّل/يعطّل كل نوع.
- _user_pref_flag يعيد True للمستخدمين بلا سجل (سلوك متوافق مع السابق).
"""

import pytest

from app.database.crud.common import get_or_create_user_pref, toggle_notification_pref

NOTIF_FIELDS = (
    "notif_task_reminder",
    "notif_budget_alert",
    "notif_credit_alert",
    "notif_invoice_alert",
    "notif_morning_summary",
    "notif_deviation",
    "notif_periodic_report",
)


class TestUserPrefModel:
    def test_notif_fields_exist_with_defaults(self, db_session):
        from app.database.models import UserPref

        pref = UserPref(telegram_user_id=111)
        db_session.add(pref)
        db_session.commit()
        for field in NOTIF_FIELDS:
            assert getattr(pref, field) is True

    def test_unique_per_user(self, db_session):
        get_or_create_user_pref(db_session, 222)
        get_or_create_user_pref(db_session, 222)
        from app.database.models import UserPref

        count = (
            db_session.query(UserPref).filter(UserPref.telegram_user_id == 222).count()
        )
        assert count == 1


class TestGetOrCreate:
    def test_creates_when_missing(self, db_session):
        pref = get_or_create_user_pref(db_session, 333)
        assert pref is not None
        assert pref.telegram_user_id == 333

    def test_returns_same_row(self, db_session):
        first = get_or_create_user_pref(db_session, 444)
        second = get_or_create_user_pref(db_session, 444)
        assert first.id == second.id


class TestToggleNotificationPref:
    @pytest.mark.parametrize("field", NOTIF_FIELDS)
    def test_toggle_off_then_on_roundtrip(self, db_session, field):
        toggle_notification_pref(db_session, 555, field, False)
        pref = get_or_create_user_pref(db_session, 555)
        assert getattr(pref, field) is False
        toggle_notification_pref(db_session, 555, field, True)
        pref = get_or_create_user_pref(db_session, 555)
        assert getattr(pref, field) is True

    def test_rejects_unknown_field(self, db_session):
        result = toggle_notification_pref(db_session, 666, "not_a_real_field", True)
        assert "غير معروف" in result

    def test_toggle_affects_only_target_field(self, db_session):
        toggle_notification_pref(db_session, 777, "notif_budget_alert", False)
        pref = get_or_create_user_pref(db_session, 777)
        assert pref.notif_budget_alert is False
        assert pref.notif_task_reminder is True
        assert pref.notif_morning_summary is True


class TestUserPrefFlag:
    def _flag(self, db_session, uid, field):
        from bot.reminders import _user_pref_flag

        return _user_pref_flag(db_session, uid, field)

    @pytest.mark.parametrize("field", NOTIF_FIELDS)
    def test_default_true_when_no_pref(self, db_session, field):
        assert self._flag(db_session, 888, field) is True

    def test_honors_disabled_field(self, db_session):
        toggle_notification_pref(db_session, 999, "notif_task_reminder", False)
        assert self._flag(db_session, 999, "notif_task_reminder") is False
        assert self._flag(db_session, 999, "notif_budget_alert") is True