"""
اختبارات حارس الإنتاج: رفض بدء البوت في البيئة production دون ENCRYPTION_KEY،
مع بقاء التطوير متساهلًا بغياب المفتاح (وضع التوافق/النص الواضح).
"""

import base64

import pytest

from app.config import Settings, ensure_env_or_exit, validate_env

_TEST_KEY = base64.b64encode(b"0" * 32).decode("ascii")


def _isolate_env(monkeypatch, app_env="development", encryption_key=""):
    monkeypatch.setattr("app.config.settings.app_env", app_env)
    monkeypatch.setattr("app.config.settings.encryption_key", encryption_key)
    monkeypatch.setattr("app.config.settings.telegram_bot_token", "token")
    monkeypatch.setattr("app.config.settings.gemini_api_key", "key")


class TestProductionEncryptionGuard:
    def test_missing_key_flagged_in_production(self, monkeypatch):
        _isolate_env(monkeypatch, app_env="production")
        missing = validate_env()
        assert any("ENCRYPTION_KEY" in m for m in missing)

    def test_present_key_not_flagged_in_production(self, monkeypatch):
        _isolate_env(monkeypatch, app_env="production", encryption_key=_TEST_KEY)
        missing = validate_env()
        assert not any("ENCRYPTION_KEY" in m for m in missing)

    def test_missing_key_not_flagged_in_development(self, monkeypatch):
        _isolate_env(monkeypatch, app_env="development")
        missing = validate_env()
        assert not any("ENCRYPTION_KEY" in m for m in missing)

    def test_whitespace_key_treated_as_missing_in_production(self, monkeypatch):
        _isolate_env(monkeypatch, app_env="production", encryption_key="   ")
        missing = validate_env()
        assert any("ENCRYPTION_KEY" in m for m in missing)

    def test_ensure_env_or_exit_refuses_production_without_key(self, monkeypatch):
        _isolate_env(monkeypatch, app_env="production")
        with pytest.raises(SystemExit):
            ensure_env_or_exit()

    def test_ensure_env_or_exit_passes_production_with_key(self, monkeypatch):
        _isolate_env(monkeypatch, app_env="production", encryption_key=_TEST_KEY)
        ensure_env_or_exit()


class TestAdminIdsParsing:
    def test_parses_comma_separated(self):
        s = Settings(admin_user_ids="111, 222, abc")
        assert s.admin_user_ids == [111, 222]
