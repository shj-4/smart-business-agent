"""
اختبارات النسخ الاحتياطي (app.database.backup): نسخ سليم + تدوير الاحتفاظ.

تُنفَّذ على قاعدة SQLite مؤقتة داخل tmp_path — لا تلمس بيانات حقيقية أبدًا.
"""

import sqlite3

from app.database import backup


def _make_source_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO demo (note) VALUES ('بيانات تجريبية')")
    conn.commit()
    conn.close()


def test_run_backup_creates_snapshot_and_rotates(monkeypatch, tmp_path):
    src = tmp_path / "business.db"
    _make_source_db(str(src))
    bdir = tmp_path / "backups"

    monkeypatch.setattr(backup, "DB_PATH", str(src))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(bdir))
    monkeypatch.setattr(backup, "KEEP_COUNT", 3)
    monkeypatch.setattr(backup, "LOG_FILE", str(tmp_path / "backup.log"))

    assert backup.run_backup() is True
    files = backup._list_backups()
    assert len(files) == 1

    snapped = sqlite3.connect(bdir / files[0])
    row = snapped.execute("SELECT note FROM demo").fetchone()
    snapped.close()
    assert row[0] == "بيانات تجريبية"

    # ملء أكثر من KEEP_COUNT ثم تشغيل النسخ → يبقى فقط KEEP_COUNT
    for _ in range(5):
        backup.run_backup()
    assert len(backup._list_backups()) <= 3


def test_run_backup_missing_db_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "DB_PATH", str(tmp_path / "ghost.db"))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(backup, "LOG_FILE", str(tmp_path / "backup.log"))
    assert backup.run_backup() is False


def test_run_backup_skips_non_sqlite(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "DB_PATH", "")  # قاعدة ليست SQLite
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(backup, "LOG_FILE", str(tmp_path / "backup.log"))
    assert backup.run_backup() is False


def test_verify_backup_accepts_clean_copy(monkeypatch, tmp_path):
    """النسخة المنتجة عبر run_backup تمر فحص PRAGMA integrity_check."""
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO t (name) VALUES ('x')")
    conn.commit()
    conn.close()

    bdir = tmp_path / "backups"
    monkeypatch.setattr(backup, "DB_PATH", str(src))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(bdir))
    monkeypatch.setattr(backup, "KEEP_COUNT", 3)
    monkeypatch.setattr(backup, "LOG_FILE", str(tmp_path / "backup.log"))

    assert backup.run_backup() is True
    made = backup._list_backups()
    assert len(made) == 1
    assert backup._verify_backup(str(bdir / made[0])) is True


def test_verify_backup_rejects_corrupted_file(tmp_path):
    """ملف تالف (ليس قاعدة صالحة) يفشل الفحص ولا يعتمد كنسخة سليمة."""
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"\x00\x01\x02 not a sqlite file at all \xff\xfe")
    assert backup._verify_backup(str(bad)) is False


def test_run_backup_removes_failed_verification(monkeypatch, tmp_path):
    """إذا فشل فحص السلامة تُحذف النسخة ولا تُحسب ضمن المحتفظ بها."""
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    bdir = tmp_path / "backups"
    monkeypatch.setattr(backup, "DB_PATH", str(src))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(bdir))
    monkeypatch.setattr(backup, "LOG_FILE", str(tmp_path / "backup.log"))
    monkeypatch.setattr(backup, "_verify_backup", lambda path: False)

    assert backup.run_backup() is False
    assert backup._list_backups() == []
