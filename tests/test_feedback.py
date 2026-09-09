"""
اختبارات سجل التصحيحات (CorrectionFeedback) — تسجيل، قائمة، ومراجعة.
"""

from app.database.crud import (
    list_correction_feedback,
    mark_correction_reviewed,
    record_correction_feedback,
)
from app.database.models import CorrectionFeedback


class TestCorrectionFeedback:
    def test_record_ignores_empty_message(self, db_session):
        assert record_correction_feedback(db_session, 42, "   ", "cancel") is None
        assert db_session.query(CorrectionFeedback).count() == 0

    def test_record_and_list_unreviewed(self, db_session):
        record_correction_feedback(
            db_session, 42, "دفعت 500 شيكل لمحمد", "cancel", data_type="expense"
        )
        record_correction_feedback(db_session, 42, "نوع آخر", "reject_confirm", data_type="task")

        rows = list_correction_feedback(db_session, only_unreviewed=True)
        assert len(rows) == 2
        assert all(not r.reviewed for r in rows)
        # كلاهما بانتظار المراجعة
        assert {r.source for r in rows} == {"cancel", "reject_confirm"}

        # تعليم الأول كمراجَع → يختفي من قائمة بانتظار المراجعة
        first_id = rows[0].id
        assert mark_correction_reviewed(db_session, first_id) is True
        remaining = list_correction_feedback(db_session, only_unreviewed=True)
        assert len(remaining) == 1
        assert remaining[0].id != first_id

    def test_mark_reviewed(self, db_session):
        record_correction_feedback(db_session, 7, "رسالة سيئة الفهم", "cancel")
        row = db_session.query(CorrectionFeedback).first()
        fb_id = row.id

        assert mark_correction_reviewed(db_session, fb_id) is True
        assert list_correction_feedback(db_session, only_unreviewed=True) == []
        # التحديث الثاني لا يُوجد؟
        assert mark_correction_reviewed(db_session, fb_id) is True  # idempotent
        assert mark_correction_reviewed(db_session, 999999) is False

    def test_duplicate_message_recorded_without_crash(self, db_session):
        """تسجيل متكرر لنفس النص لا يكسر — الإلغاء المستخدم كثيرًا يمرّ آمنًا."""
        record_correction_feedback(db_session, 1, "نص متكرر", "cancel")
        record_correction_feedback(db_session, 1, "نص متكرر", "cancel")
        assert db_session.query(CorrectionFeedback).count() == 2
