"""
ناقل أحداث داخلي بسيط (in-process events) — بديل عن الاستيرادات الدائرية المؤجَّلة.

مثال: crud ينشر "data_written" بعد أي كتابة لإبطال ذاكرات القراءة، و app.admin
يلتقطه لمسح كاش إحصائياته — دون أن يعتمد أيٌّ منهما على الآخر.

الأحداث متزامنة داخل العملية؛ تُستدعى المعالجات بالترتيب، وأي استثناء في
معالِج يُسجَّل في السجل ولا يُسقط تدفق العمل الصادر (الكتابة تمت بالفعل).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

_HANDLERS: dict[str, list[Callable[..., None]]] = {}
_LOCK = threading.Lock()

DATA_WRITTEN = "data_written"


def on(event: str, handler: Callable[..., None]) -> None:
    """يسجّل معالِجًا لحدث — يُستدعى حيًا عند كل emit."""
    with _LOCK:
        _HANDLERS.setdefault(event, []).append(handler)


def emit(event: str, **kwargs) -> None:
    """ينشر حدثًا لكل المعالجات مع نسخ القائمة (عدده صغير فالنسخ آمن وخفيف)."""
    handlers = _HANDLERS.get(event)
    if not handlers:
        return
    for handler in list(handlers):
        try:
            handler(**kwargs)
        except Exception:
            logger.exception("فشل معالِج الحدث %r", event)
