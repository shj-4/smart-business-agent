"""
اختبارات عزل crud عن الإدارة والتدقيق عبر app.events/app.money (بند المراجعة 6):

- لا تُستورَد admin/audit داخل أجساد دوال حزمة crud (الاستيراد المؤجَّل المبعثر).
- app.admin لا يستورد من app.database.crud إطلاقًا.
- كتابة من crud تنشر "data_written" فتمسح كاش إحصائيات الأدمن عبر الحدث.
"""

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "app" / "database" / "crud"
FORBIDDEN = ("app.admin", "app.audit")


@pytest.mark.parametrize("module", ["common", "workspace", "tasks", "records", "budgets", "invoices", "credit", "reports", "prefs", "feedback"])
def test_crud_functions_do_not_lazy_import_admin_or_audit(module):
    tree = ast.parse((PKG / f"{module}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.col_offset > 0:
            assert node.module not in FORBIDDEN, f"{module}.py: استيراد مؤجَّل {node.module} داخل دالة"


def test_admin_module_has_no_crud_dependency():
    tree = ast.parse(Path(__file__).resolve().parent.parent.joinpath("app/admin.py").read_text(encoding="utf-8"))
    mods = {
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not any(m == "app.database.crud" or m.startswith("app.database.crud.") for m in mods)


def test_admin_clear_cache_subscribed_to_data_written():
    import app.admin  # noqa: F401  (التسجيل يتم عند الاستيراد)
    from app.admin import clear_admin_cache
    from app.events import _HANDLERS, DATA_WRITTEN

    assert clear_admin_cache in _HANDLERS[DATA_WRITTEN]


def test_write_path_emits_data_written(db_session):
    from app.database.crud import create_transaction
    from app.events import DATA_WRITTEN, emit, on

    spy = []

    def handler(**kwargs):
        spy.append(kwargs)

    on(DATA_WRITTEN, handler)
    emit(DATA_WRITTEN)  # إثبات مسار النشر يعمل قبل الاعتماد على الكتابة
    create_transaction(
        db_session,
        111,
        {"type": "expense", "amount": 10, "currency": "ILS", "description": "حدث"},
        raw_message="حدث",
        telegram_message_id=1111,
    )
    assert spy


def test_data_written_clears_admin_stats_cache(db_session):
    from app.admin import build_admin_stats
    from app.cache import _UNSET, get
    from app.database.crud import create_transaction

    build_admin_stats(db_session)
    assert get("admin_stats") is not _UNSET

    create_transaction(
        db_session,
        222,
        {"type": "expense", "amount": 20, "currency": "ILS", "description": "يمسح الكاش"},
        raw_message="يمسح الكاش",
        telegram_message_id=2222,
    )
    assert get("admin_stats") is _UNSET
