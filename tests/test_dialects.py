"""
اختبارات كشف اللهجات وتوجيه الـ prompt حسبها.
"""

from app.dialects import (
    EGYPTIAN,
    GULF,
    LEVANTINE,
    MAGHREBI,
    MSA,
    detect_dialect,
    dialect_instruction,
    dialect_label,
)


class TestDetectDialect:
    def test_empty_returns_msa(self):
        assert detect_dialect("") == MSA
        assert detect_dialect(None) == MSA
        assert detect_dialect("   ") == MSA

    def test_egyptian(self):
        assert detect_dialect("انت إزاي؟ عايز أسجل عملية") == EGYPTIAN

    def test_levantine(self):
        assert detect_dialect("شو أخبارك؟ بدي سجل عملية اليوم") == LEVANTINE

    def test_gulf(self):
        assert detect_dialect("شلونك وينك؟ أبغى أسجل مصاريف") == GULF

    def test_maghrebi(self):
        assert detect_dialect("واش علاش، شحال راك؟ بزاف بزاف") == MAGHREBI

    def test_ultrae_mixed_defaults_msa(self):
        # لا نُعلن لهجة لكلمات منفردة مشتركة (الـ MSA خيار آمن)
        assert detect_dialect("سجل عملية شراء اليوم") == MSA

    def test_ambiguous_words_do_not_force_false_positive(self):
        assert detect_dialect("مش عارف، بس ممكن بعدين") == MSA

    def test_single_word_on_short_text_does_not_declare_dialect(self):
        # رسالة قصيرة جدًا (<3 كلمات): كلمة واحدة عامة/مميزة لا تكفي لإعلان
        # لهجة كاملة — النتيجة على نصوص عامة قصيرة غير موثوقة.
        assert detect_dialect("شو") == MSA
        assert detect_dialect("ابغى") == MSA
        assert detect_dialect("كيفك") == MSA
        assert detect_dialect("بزاف") == MSA

    def test_short_text_with_two_strong_matches_declares(self):
        # رسالة قصيرة لكنها لهجية بوضوح (أكثر من تطابق) تبقى تُصنَّف.
        assert detect_dialect("وين شلونك") == GULF
        assert detect_dialect("شو بدك") == MSA  # تطابق واحد فقط → لا تُعلن

    def test_egyptian_expanded_words(self):
        assert detect_dialect("يلا يلا تمام بجد") == EGYPTIAN

    def test_levantine_expanded_words(self):
        assert detect_dialect("كتير هلأ خليني أكيد") == LEVANTINE

    def test_gulf_expanded_words(self):
        assert detect_dialect("صراحة العافية بخصوص الشغل") == GULF

    def test_maghrebi_expanded_words(self):
        assert detect_dialect("هاك نعام غادي دوك") == MAGHREBI


class TestDialectHelpers:
    def test_instruction_not_empty_and_scoped(self):
        for d in (MSA, EGYPTIAN, LEVANTINE, GULF, MAGHREBI):
            ins = dialect_instruction(d)
            assert isinstance(ins, str) and ins
        assert dialect_instruction(EGYPTIAN) != dialect_instruction(MSA)

    def test_label_for_known_and_unknown(self):
        assert "مصر" in dialect_label(EGYPTIAN)
        assert dialect_label("unknown_dialect")  # يقع على MSA دون انفجار
