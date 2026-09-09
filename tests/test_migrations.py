"""
ضمان الجودة المكاني: كل ملف migration يحتوي downgrade() فعليًا، والنسخ المتسلسلة
مترابطة بشكل غير منقطع (كل revision يظهر مرة واحدة ك down_revision إلا الجذر).
"""

import os
import re

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations", "versions"
)


def _version_files() -> list[str]:
    return sorted(
        f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".py") and not f.startswith("__")
    )


def test_all_migrations_have_downgrade():
    missing = [
        f
        for f in _version_files()
        if "def downgrade" not in open(os.path.join(MIGRATIONS_DIR, f), encoding="utf-8").read()
    ]
    assert missing == []


def test_migration_chain_is_linked():
    files = _version_files()
    revisions = {}
    down_refs = []
    for f in files:
        src = open(os.path.join(MIGRATIONS_DIR, f), encoding="utf-8").read()
        rev = re.search(r"^revision[^\n]*=\s*['\"]([^'\"]+)['\"]", src, re.M)
        down = re.search(r"^down_revision[^\n]*=\s*['\"]([^'\"]+)['\"]", src, re.M)
        assert rev is not None, f"{f} بلا revision"
        revisions[rev.group(1)] = f
        if down is not None:
            down_refs.append(down.group(1))
    # كل down_revision يشير إلى نسخة موجودة
    assert set(down_refs) <= set(revisions), "سلسلة migrations مقطوعة"
    # down_revision فريدة (شجرة واحدة بلا تفرع/تعارض)
    assert len(down_refs) == len(set(down_refs)), "اندماج غير متوقّع في سلسلة migrations"
