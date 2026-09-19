"""
اختبارات حارس الإنتاج: رفض بدء البوت في البيئة production دون ENCRYPTION_KEY،
مع بقاء التطوير متساهلًا بغياب المفتاح (وضع التوافق/النص الواضح).
"""

import base64

import pytest
from pydantic import ValidationError

from app.config import Settings, ensure_env_or_exit, validate_env

_TEST_KEY = base64.b64encode(b"0" * 32).decode("ascii")


def _isolate_env(monkeypatch, app_env="development", encryption_key="", dashboard_password="test-dashboard-pass"):
    monkeypatch.setattr("app.config.settings.app_env", app_env)
    monkeypatch.setattr("app.config.settings.encryption_key", encryption_key)
    monkeypatch.setattr("app.config.settings.dashboard_password", dashboard_password)
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

    def test_invalid_key_flagged_in_production(self, monkeypatch):
        _isolate_env(monkeypatch, app_env="production", encryption_key="abcd")
        missing = validate_env()
        assert any("ENCRYPTION_KEY" in m for m in missing)

    def test_wrong_length_decoded_key_flagged_in_production(self, monkeypatch):
        # يُفك بنجاح بطول غير 16/24/32 بايت (متساهل) ثم يُرفض
        _isolate_env(monkeypatch, app_env="production", encryption_key="YWJjZGVmZ2hpamtsbW5vcHFyc3R1")
        missing = validate_env()
        assert any("ENCRYPTION_KEY" in m for m in missing)

    def test_ensure_env_or_exit_refuses_invalid_key_in_production(self, monkeypatch):
        _isolate_env(monkeypatch, app_env="production", encryption_key="abcd")
        with pytest.raises(SystemExit):
            ensure_env_or_exit()


class TestAdminIdsParsing:
    def test_parses_comma_separated(self):
        s = Settings(admin_user_ids="111, 222, abc")
        assert s.admin_user_ids == [111, 222]


class TestSettingsBoundsValidation:
    """باگ 17 — حقول الأداء الحساسة يجب أن ترفض القيم غير الموجبة (0/سالبة):
    كانت cache_ttl_seconds=0، ai_queue_maxsize=0، ai_queue_concurrency=0،
    max_voice_file_mb=0 تمرّ في التحقق ثم تُسقط ضوابط التشغيل/الحجم عند التشغيل."""

    @pytest.mark.parametrize(
        "field, value",
        [
            ("cache_ttl_seconds", 0),
            ("cache_ttl_seconds", -1),
            ("ai_queue_maxsize", 0),
            ("ai_queue_maxsize", -10),
            ("ai_queue_concurrency", 0),
            ("ai_queue_concurrency", -2),
            ("max_voice_file_mb", 0),
            ("max_voice_file_mb", -5),
        ],
    )
    def test_rejects_non_positive(self, field, value):
        with pytest.raises(ValidationError):
            Settings(**{field: value})

    @pytest.mark.parametrize(
        "field, value",
        [
            ("cache_ttl_seconds", 1),
            ("ai_queue_maxsize", 1),
            ("ai_queue_concurrency", 1),
            ("max_voice_file_mb", 1),
        ],
    )
    def test_accepts_positive_minimum(self, field, value):
        s = Settings(**{field: value})
        assert getattr(s, field) == value
