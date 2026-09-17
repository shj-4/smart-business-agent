"""
اختبارات تعقيم الأسرار في السجلات (app.logging_config._scrub + المُنسّقات).
"""

import io
import logging

from app.logging_config import JsonFormatter, ScrubbingFormatter, _scrub


def test_scrub_redacts_env_secret_values():
    text = "configured TELEGRAM_BOT_TOKEN=123456:ABC gEMINI_API_KEY=zzz"
    out = _scrub(text)
    assert "123456:ABC" not in out
    assert "zzz" not in out
    assert "***REDACTED***" in out


def test_scrub_redacts_password_and_authorization():
    text = "password=S3cret! Authorization: Bearer xyzabc"
    out = _scrub(text)
    assert "S3cret!" not in out
    assert "xyzabc" not in out


def test_scrub_keeps_plain_arabic_message():
    text = "تم إيداع 50 شيكل بنجاح لمحل البقالة"
    assert _scrub(text) == text


def test_scrubbing_formatter_redacts_interpolated_args():
    buffer = io.StringIO()
    logger = logging.getLogger("test.scrubber.text")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(ScrubbingFormatter("%(message)s"))
    logger.addHandler(handler)

    logger.info("TELEGRAM_BOT_TOKEN=%s", "leak-123")
    logger.removeHandler(handler)

    output = buffer.getvalue()
    assert "leak-123" not in output
    assert "***REDACTED***" in output


def test_scrubbing_formatter_handles_placeholder_cleanly():
    """رسالة بها %s تُنسَّق بسلاسة إما بالحجب أو بالإبقاء — لا كسر ولا تسريب قيمة."""
    buffer = io.StringIO()
    logger = logging.getLogger("test.scrubber.placeholder")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(ScrubbingFormatter("%(message)s"))
    logger.addHandler(handler)

    logger.info("فشل الاتصال: token=%s", "abc123")
    logger.removeHandler(handler)

    output = buffer.getvalue()
    assert "abc123" not in output


def test_json_formatter_redacts_args():
    buffer = io.StringIO()
    logger = logging.getLogger("test.scrubber.json")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter(service="test"))
    logger.addHandler(handler)

    logger.info("ENCRYPTION_KEY=%s", "super-secret-enc")
    logger.removeHandler(handler)

    output = buffer.getvalue()
    assert "super-secret-enc" not in output
    assert "***REDACTED***" in output


def test_scrub_does_not_double_separators():
    """الحجب يحافظ على الفاصل الواحد — لا يتحول `token=` إلى `token==`."""
    assert _scrub("token=abc123") == "token=***REDACTED***"
    assert _scrub("BOT_TOKEN: secret") == "BOT_TOKEN: ***REDACTED***"
    assert "token==***" not in _scrub("token=abc123")
