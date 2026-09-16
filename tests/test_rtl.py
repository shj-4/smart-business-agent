"""
Unit tests لـ app.rtl — تطويق المقاطع اللاتينية بعلامات اتجاه (Bidi) كي لا
ينقلب ترتيب العملات/التواريخ/الأرقام داخل الجمل العربية.
"""

from app.rtl import RLM, _apply_fix, fix_bidi, install_bidi_patches


class TestFixBidi:
    def test_wraps_each_latin_run_in_rlm(self):
        out = fix_bidi("المبلغ: 100 USD")
        assert out == f"المبلغ: {RLM}100{RLM} {RLM}USD{RLM}"

    def test_money_line_quoted_in_report(self):
        out = fix_bidi("• 1500.50 USD")
        assert out == f"• {RLM}1500.50{RLM} {RLM}USD{RLM}"

    def test_date_and_time_are_wrapped(self):
        out = fix_bidi("موعد: 2026-09-10 15:30")
        assert out == f"موعد: {RLM}2026-09-10{RLM} {RLM}15:30{RLM}"

    def test_adjacent_latin_stays_single_run(self):
        out = fix_bidi("قيمة 100USD")
        assert out == f"قيمة {RLM}100USD{RLM}"

    def test_callbacks_and_symbols_wrapped(self):
        out = fix_bidi("زر: edit:1/x")
        assert out == f"زر: {RLM}edit:1/x{RLM}"

    def test_pure_arabic_untouched(self):
        text = "مرحبًا بك، كيف حالك؟"
        assert fix_bidi(text) == text

    def test_mixed_arabic_digits_untouched(self):
        # الأرقام العربية-الهندية ليست لاتينية فلا تُطوَّق
        out = fix_bidi("المصروف ٣٥٠")
        assert out == "المصروف ٣٥٠"

    def test_empty_and_none_safe(self):
        assert fix_bidi("") == ""
        assert fix_bidi(None) is None

    def test_multiline_table(self):
        out = fix_bidi("العملة    المبلغ\nUSD        100\nILS        50")
        assert out.count(RLM) == 8  # 4 كلمات لاتينية × علامتين
        assert out.startswith("العملة")


class TestApplyFix:
    def test_positional_text(self):
        args, kwargs = _apply_fix(0, ("مبلغ 100 USD", "html"), {})
        assert args[0] == f"مبلغ {RLM}100{RLM} {RLM}USD{RLM}"
        assert args[1] == "html"

    def test_keyword_text(self):
        args, kwargs = _apply_fix(1, (555,), {"text": "إجمالي 50 USD", "parse_mode": "Markdown"})
        assert args == (555,)
        assert kwargs["text"] == f"إجمالي {RLM}50{RLM} {RLM}USD{RLM}"
        assert kwargs["parse_mode"] == "Markdown"

    def test_no_text_leaves_untouched(self):
        args, kwargs = _apply_fix(1, (), {})
        assert args == ()
        assert kwargs == {}

    def test_none_text_untouched(self):
        args, kwargs = _apply_fix(1, ("123",), {"text": None})
        assert args == ("123",)
        assert kwargs["text"] is None


class TestInstallBidiPatches:
    def test_install_is_idempotent(self):
        install_bidi_patches()
        install_bidi_patches()
        from telegram import Message

        assert "__wrapped__" in dir(Message.reply_text)
