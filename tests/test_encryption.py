"""
اختبارات تشفير الحقول: EncryptedNumeric — ألا تُسقط القيم الصالحة، وألا تكتب
NULL بصمت لقيم غير صالحة (طبقة حماية للمسارات التي تكتب .amount مباشرة).
"""

from decimal import Decimal

import pytest

from app.security import EncryptedNumeric, EncryptedString


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
        assert t.process_result_value(out, None) == Decimal("55.68")

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
