"""
اختبارات توحيد خريطة الأولوية/التكرار في app/normalize.py (بند المراجعة 7):

- سلوك موحّد للمرادفات التي كانت متفرّقة عبر 5 نسخ (crud/schemas/editing/conversation×2).
- حارس بنيوي يمنع الرجوع لنسخ لاحقة من المفردات الناقلة خارج الوحدة الموحّدة.
"""

from pathlib import Path

import pytest

from app.normalize import normalize_priority, normalize_recurrence

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("high", "high"),
        ("HIGH", "high"),
        ("عالية", "high"),
        ("عالي", "high"),
        ("عالى", "high"),
        ("مهم", "high"),
        ("مهمّ", "high"),
        ("عاجل", "high"),
        ("عاجلة", "high"),
        ("مستعجل", "high"),
        ("h", "high"),
        ("low", "low"),
        ("منخفضة", "low"),
        ("منخفض", "low"),
        ("ضعيفة", "low"),
        ("عادية جدًا", "low"),
        ("l", "low"),
        ("normal", "normal"),
        ("عادية", "normal"),
        ("عادي", "normal"),
        ("مجهول", "normal"),
        ("", "normal"),
        (None, "normal"),
    ],
)
def test_normalize_priority(raw, expected):
    assert normalize_priority(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("daily", "daily"),
        ("يومي", "daily"),
        ("كل يوم", "daily"),
        ("يوم", "daily"),
        ("weekly", "weekly"),
        ("أسبوعي", "weekly"),
        ("كل أسبوع", "weekly"),
        ("كل اسبوع", "weekly"),
        ("اسبوعي", "weekly"),
        ("اسبوع", "weekly"),
        ("monthly", "monthly"),
        ("شهري", "monthly"),
        ("كل شهر", "monthly"),
        ("شهر", "monthly"),
        ("غريب", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_recurrence(raw, expected):
    assert normalize_recurrence(raw) == expected


def test_same_behavior_across_crud_schemas_and_edit_paths():
    """الكلمة التي انقسمت عليها النسخ تتصرف الآن بشكل موحّد في كل المسارات."""
    from app.database.crud import create_task  # noqa: F401
    from app.schemas import normalize_analysis

    # مسار crud (create_task) — كان يقرأ "مهم" عاليًا
    assert normalize_priority("مهم") == "high"
    # مسار schemas — نفس القيمة
    assert normalize_analysis({"priority": "مهم"})["priority"] == "high"
    # مسار bot/editing كان يدعم "عاجلة" فقط — الآن "مهم" أيضًا
    # كل المستهلكين يشاركون نفس التنفيذ (هوية واحدة) — لا نسخ منفصلة
    import bot.conversation
    import bot.editing
    from app import schemas as app_schemas
    from app.schemas import normalize_priority as schemas_priority

    assert bot.editing.normalize_priority is normalize_priority
    assert bot.conversation.normalize_priority is normalize_priority
    assert app_schemas.normalize_priority is normalize_priority
    assert schemas_priority is normalize_priority


def test_priority_vocabulary_drift_guard():
    """المفردات الناقلة يجب ألا تعود إلى نسخ داخل bot/ أو app/ خارج الوحدة الموحّدة."""
    tokens = ("مستعجل", "عالى", "عادية جدًا")
    for f in [*ROOT.glob("app/**/*.py"), *ROOT.glob("bot/*.py")]:
        if f.name == "normalize.py":
            continue
        text = f.read_text(encoding="utf-8")
        for token in tokens:
            assert token not in text, f"{f}: مفردة أولوية خارج app/normalize.py ({token})"
