"""
اختبارات بنية تقسيم app.database.crud إلى حزمة وحدات فرعية:

- الواجهة `app.database.crud` تعيد تصدير كل اسم من وحِدته الصحيحة (نفس الكائن).
- لا وجود لملف crud.py وحيد ضخم (مصدر موزع فعليًا).
- كل وحدة فرعية قابلة للاستيراد وملفها سليم الصياغة.
"""

import ast
import importlib
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "app" / "database" / "crud"


def _module_names(py_file: Path) -> tuple[str, list[str]]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
    return py_file.stem, names


def test_crud_module_is_package_not_monolithic_file():
    import app.database.crud as crud

    assert crud.__file__.endswith(
        ("crud" + "\\__init__.py", "crud" + "/__init__.py")
    )
    assert (PKG.parent / "crud.py").exists() is False


@pytest.mark.parametrize("module", ["common", "workspace", "tasks", "records", "budgets", "invoices", "credit", "reports", "prefs", "feedback", "bonus"])
def test_submodule_importable(module):
    importlib.import_module(f"app.database.crud.{module}")


def test_facade_exposes_all_names_from_their_module():
    from app.database import crud as fac

    seen = {}
    for py_file in sorted(PKG.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        mod_name, names = _module_names(py_file)
        mod_obj = importlib.import_module(f"app.database.crud.{mod_name}")
        for name in names:
            assert hasattr(fac, name), f"الواجهة تفتقد {name} من {mod_name}"
            assert getattr(fac, name) is getattr(mod_obj, name)
            assert name not in seen, f"اسم مكرر بين الوحدات: {name}"
            seen[name] = mod_name


def test_still_has_all_expected_public_apis():
    from app.database import crud as fac

    for name in (
        "search_records",
        "list_pending_tasks",
        "list_overdue_tasks",
        "monthly_totals",
        "_sum_amounts_by_currency",
        "accessible_user_ids",
        "CURRENCY_ALIASES",
        "run_query",
    ):
        assert callable(getattr(fac, name, None)) or getattr(fac, name, None) is not None
