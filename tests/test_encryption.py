"""
اختبارات تشفير الحقول: EncryptedNumeric — ألا تُسقط القيم الصالحة، وألا تكتب
NULL بصمت لقيم غير صالحة (طبقة حماية للمسارات التي تكتب .amount مباشرة).
وكذلك تحقق صحة مفتاح ENCRYPTION_KEY (الطول 16/24/32) والسقوط الآمن عند
مفتاح غير صالح بدل انهيار أول كتابة.
"""

import base64
import logging
from decimal import Decimal

import pytest

from app.security import (
    EncryptedNumeric,
    EncryptedString,
    _decode_key_bytes,
    encrypt_text,
    encryption_key_problem,
    settings,
)

VALID_B64_KEY = base64.b64encode(b"0" * 32).decode("ascii")


class TestKeyValidation:
    def test_accepts_base64_32_byte_key(self):
        assert _decode_key_bytes(VALID_B64_KEY) == b"0" * 32
        assert encryption_key_problem(VALID_B64_KEY) is None

    def test_accepts_urlsafe_base64_key(self):
        raw = base64.urlsafe_b64encode(b"1" * 32).decode("ascii")
        assert _decode_key_bytes(raw) == b"1" * 32
        assert encryption_key_problem(raw) is None

    def test_accepts_urlsafe_with_dash_underscore(self):
        # مفتاح حقيقي يحتوي -_ في ترميز urlsafe يجب أن يُفك بعد التحويل إلى +/
        # نبحث عن مفتاح عشوائي يُنتج -_ في urlsafe
        found = False
        for seed in [b"\xfb\xef" * 16, b"\xff\x00" * 16, b"\x00\xff\x01\x02" * 8]:
            raw = base64.urlsafe_b64encode(seed).decode("ascii")
            if "-" in raw or "_" in raw:
                assert _decode_key_bytes(raw) == seed
                assert encryption_key_problem(raw) is None
                found = True
                break
        # fallback: brute force
        if not found:
            import os
            for _ in range(100):
                seed = os.urandom(32)
                raw = base64.urlsafe_b64encode(seed).decode("ascii")
                if "-" in raw or "_" in raw:
                    assert _decode_key_bytes(raw) == seed
                    break
        # تأكد أن التحويل اليدوي يعمل
        std = base64.b64encode(b"0" * 32).decode()
        url = std.replace("+", "-").replace("/", "_")
        # إذا كان std لا يحوي +/، نُجبر مثالًا
        if "-" not in url and "_" not in url:
            # استخدم مفتاحًا يُنتج -_
            url = base64.urlsafe_b64encode(b"\xfb\xef" * 16).decode()
            std2 = url.replace("-", "+").replace("_", "/")
            assert _decode_key_bytes(url) == base64.b64decode(std2, validate=True)
        else:
            assert _decode_key_bytes(url) == b"0" * 32

    def test_accepts_raw_text_key(self):
        # نص خارج أبجدية base64 (عربية، 32 بايت) — يسقط للتفسير النصي المباشر
        raw = "ك" * 16
        assert _decode_key_bytes(raw) == raw.encode("utf-8")
        assert encryption_key_problem(raw) is None

    def test_pure_alphabet_text_follows_base64_interpretation(self):
        # "x"*32 أبجدي رقمي خالص بطول 32: كان يُفك قديمًا إلى 24 بايت AES-192 بصمت
        # بعد الإصلاح يُفضَّل النص المباشر (32 بايت) لتجنب التحويل الصامت،
        # ويُفرض base64 صريح عبر حروف +/=_-
        assert _decode_key_bytes("x" * 32) == b"x" * 32
        # base64 صريح (44 حرفًا مع =) ما زال يُفك
        assert _decode_key_bytes(base64.b64encode(b"0" * 32).decode()) == b"0" * 32

    def test_accepts_16_and_24_byte_keys(self):
        assert _decode_key_bytes(base64.b64encode(b"k" * 16).decode()) == b"k" * 16
        # 24 بايت كنص مباشر
        assert _decode_key_bytes(b"k" * 24) == b"k" * 24
        # 24 بايت كـ base64 صريح يضم +/ (ليُفرّق عن النص المباشر)
        raw24 = b"\xff\x00\xff\x01" * 6  # 24 بايت تُنتج +/ في base64
        b64_24 = base64.b64encode(raw24).decode()
        assert any(c in b64_24 for c in "+/"), "b64_24 should contain +/ to be explicit"
        assert _decode_key_bytes(b64_24) == raw24
        # URL-safe أيضًا
        assert _decode_key_bytes(base64.urlsafe_b64encode(b"k" * 32).decode()) == b"k" * 32

    def test_rejects_wrong_decoded_length(self):
        # base64 صالح يَفُك لطول غير 16/24/32 → رفض كامل (لا إعادة تأويل نصية)
        assert _decode_key_bytes("abcd") is None  # → 3 بايت
        assert _decode_key_bytes(base64.b64encode(b"z" * 17).decode()) is None  # → 17 بايت
        assert encryption_key_problem("abcd") is not None

    def test_never_returns_wrong_length(self):
        for raw in ("abcd", "x" * 5, "x" * 20, base64.b64encode(b"z" * 10).decode()):
            key = _decode_key_bytes(raw)
            if key is not None:
                assert len(key) in (16, 24, 32)

    def test_empty_key_has_no_problem(self):
        assert _decode_key_bytes("") is None
        assert encryption_key_problem("") is None
        assert encryption_key_problem("   ") is None


class TestEncryptSafeFallback:
    def test_encrypt_with_invalid_key_returns_none_not_raise(self, monkeypatch):
        monkeypatch.setattr(settings, "encryption_key", "abcd")
        assert encrypt_text("سر") is None

    def test_write_with_invalid_key_stores_plaintext(self, monkeypatch):
        monkeypatch.setattr(settings, "encryption_key", "abcd")
        t = EncryptedNumeric()
        assert t.process_bind_param(Decimal("77.50"), None) == "77.50"
        t2 = EncryptedString()
        assert t2.process_bind_param("ملاحظة", None) == "ملاحظة"

    def test_encrypt_with_valid_key_round_trips(self, monkeypatch):
        monkeypatch.setattr(settings, "encryption_key", VALID_B64_KEY)
        t = EncryptedString()
        stored = t.process_bind_param("بيان سري", None)
        assert str(stored).startswith(("v1$", "v2$"))
        assert t.process_result_value(stored, None) == "بيان سري"

    def test_warns_once_on_invalid_key(self, monkeypatch, caplog):
        from app import security

        monkeypatch.setattr(security, "_warned_invalid", False)
        monkeypatch.setattr(settings, "encryption_key", "abcd")
        with caplog.at_level(logging.WARNING):
            encrypt_text("a")
            encrypt_text("b")
        assert sum("غير صالح" in record.message for record in caplog.records) == 1


class TestEncryptedNumeric:
    def test_accepts_valid_amount(self):
        t = EncryptedNumeric()
        out = t.process_bind_param(Decimal("12.30"), None)
        assert out is not None
        assert t.process_result_value(out, None) == Decimal("12.30")

    def test_accepts_numeric_string(self):
        t = EncryptedNumeric()
        out = t.process_bind_param("55.678", None)
        assert out is not None
        # بعد إصلاح EncryptedNumeric: يحافظ على 3 منازل (JOD/KWD/BHD/OMR) بدل تقريب قسري إلى 0.01
        assert t.process_result_value(out, None) == Decimal("55.678")
        # تأكد أن المنزلتين ما زالتا محفوظتين
        out2 = t.process_bind_param("55.67", None)
        assert t.process_result_value(out2, None) == Decimal("55.67")
        # حالة JOD الحاسمة: 12.345 دينار يجب أن تبقى 12.345 لا 12.35
        out3 = t.process_bind_param("12.345", None)
        assert t.process_result_value(out3, None) == Decimal("12.345")

    def test_rejects_unconvertible_values(self):
        t = EncryptedNumeric()
        for bad in ("abc", "ثلاثة", "12abc", "", "1,5"):
            with pytest.raises(ValueError):
                t.process_bind_param(bad, None)

    def test_rejects_non_finite_values(self):
        t = EncryptedNumeric()
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                t.process_bind_param(bad, None)

    def test_none_passes_through(self):
        assert EncryptedNumeric().process_bind_param(None, None) is None

    def test_round_trip(self):
        t = EncryptedNumeric()
        stored = t.process_bind_param(Decimal("99.99"), None)
        assert t.process_result_value(stored, None) == Decimal("99.99")


class TestEncryptedString:
    def test_round_trip(self):
        t = EncryptedString()
        stored = t.process_bind_param("بيان سري", None)
        assert t.process_result_value(stored, None) == "بيان سري"

    def test_none_passes_through(self):
        assert EncryptedString().process_bind_param(None, None) is None
