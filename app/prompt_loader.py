"""
محمّل برومبتات من مجلد prompts/ (ملفات مستقلة قابلة للتحرير دون كود).

الغرض: فصل نصوص التوجيه (prompts) عن الكود لتسهيل الضبط التجريبي دون لمس
الوحدات — بمجرد وضع ملف .md باسم معروف في مجلد prompts/. عند غياب الملف أو
فشل قراءته لأي سبب يعود loader إلى النص الافتراضي المضمّن في الكود (سقوط
آمن يمنع كسر النظام بملف تالف).
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str, default: str) -> str:
    """يقرأ برومبت من ملف utf-8 — أو يرجّع default عند الغياب/فشل القراءة."""
    try:
        path = PROMPTS_DIR / name
        if not path.exists():
            return default
        content = path.read_text(encoding="utf-8").strip()
        return content or default
    except Exception as exc:  # noqa: BLE001
        logger.warning("تعذّر تحميل البرومبت %s — سقوط آمن إلى الافتراضي: %s", name, exc)
        return default
