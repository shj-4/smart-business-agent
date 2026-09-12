"""
اختبارات حارس استكشاف صلاحيات الأدمن (البند 11):

- تسجيل محاولات الوصول الفاشلة وعدّها ضمن نافذة العتبة.
- انتهاء صلاحية العدّ بعد انقضاء النافذة.
- سلوك بوابة _require_admin: رفض غير الأدمن، قبول الأدمن، وإنذار واحد عند تجاوز العتبة.
"""

import asyncio
from types import SimpleNamespace

import pytest

from bot import ratelimit
from bot.handlers import _require_admin


@pytest.fixture(autouse=True)
def _reset_admin_state():
    ratelimit._admin_denied.clear()
    yield
    ratelimit._admin_denied.clear()


class _Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


class TestAdminDenyTracker:
    def test_count_increments_within_window(self, monkeypatch):
        monkeypatch.setattr(ratelimit, "ADMIN_ATTEMPTS_MAX", 3)
        counts = [ratelimit.register_admin_denied(555) for _ in range(4)]
        assert counts == [1, 2, 3, 4]

    def test_expires_after_window(self, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr(ratelimit.time, "monotonic", clock.monotonic)
        ratelimit.register_admin_denied(666)
        assert ratelimit.admin_denied_count(666) == 1
        clock.now += ratelimit.ADMIN_ATTEMPTS_WINDOW + 1
        assert ratelimit.admin_denied_count(666) == 0

    def test_probing_detected_at_threshold(self, monkeypatch):
        monkeypatch.setattr(ratelimit, "ADMIN_ATTEMPTS_MAX", 2)
        ratelimit.register_admin_denied(777)
        assert ratelimit.is_admin_probing(777) is False
        ratelimit.register_admin_denied(777)
        assert ratelimit.is_admin_probing(777) is True

    def test_per_user_isolation(self):
        ratelimit.register_admin_denied(1111)
        assert ratelimit.admin_denied_count(2222) == 0


def _fake_update(user_id, replied=None, sent=None):
    async def _reply(text, **kwargs):
        if replied is not None:
            replied.append(text)

    async def _send(chat_id, text):
        if sent is not None:
            sent.append((chat_id, text))

    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(reply_text=_reply),
        bot=SimpleNamespace(send_message=_send),
    )


class TestRequireAdmin:
    def test_rejects_non_admin(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.admin_user_ids", [])
        replied = []
        ok = asyncio.run(_require_admin(_fake_update(111, replied=replied), "admin_stats"))
        assert ok is False
        assert len(replied) == 1
        assert "للمسؤول فقط" in replied[0]

    def test_allows_admin(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.admin_user_ids", [222])
        ok = asyncio.run(_require_admin(_fake_update(222), "admin_stats"))
        assert ok is True

    def test_notifies_once_when_threshold_crossed(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.admin_user_ids", [999])
        monkeypatch.setattr(ratelimit, "ADMIN_ATTEMPTS_MAX", 2)
        sent = []
        for _ in range(3):
            update = _fake_update(333, sent=sent)
            asyncio.run(_require_admin(update, "admin_stats"))
        assert len(sent) == 1
        assert sent[0][0] == 999
        assert "333" in sent[0][1]
