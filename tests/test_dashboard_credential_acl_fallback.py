import os
from pathlib import Path

import app.main as api


def test_locked_credentials_file_uses_private_fallback(tmp_path, monkeypatch):
    canonical = tmp_path / "dashboard_credentials.txt"
    canonical.write_text("preserve this file", encoding="utf-8")
    monkeypatch.setattr(api, "_DASHBOARD_CRED_FILE", str(canonical))
    monkeypatch.setattr(api, "_restrict_file_windows", lambda _path: None)
    original_open = os.open

    def deny_canonical(path, flags, mode=0o777):
        if os.fspath(path) == str(canonical):
            raise PermissionError("simulated restrictive ACL")
        return original_open(path, flags, mode)

    monkeypatch.setattr(api.os, "open", deny_canonical)
    fallback = api._write_dashboard_credentials("admin", "generated-password")

    assert fallback != str(canonical)
    assert canonical.read_text(encoding="utf-8") == "preserve this file"
    assert "DASHBOARD_PASSWORD=generated-password" in Path(fallback).read_text(encoding="utf-8")
