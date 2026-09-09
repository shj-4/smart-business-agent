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
