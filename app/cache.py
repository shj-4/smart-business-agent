"""
ذاكرة تخزين مؤقت بسيطة داخل العملية (in-memory TTL cache) للاستعلامات المتكررة.

الفكرة: إذا سأل المستخدم نفسه "كم صرفت هذا الشهر" أكثر من مرة خلال نفس الدقيقة،
نتجنّب إعادة الجمع المستقل (وهو مكلف هنا لأن المبالغ مشفّرة وتُجمع في Python).

الآليات:
  - TTL يقبل الضبط (افتراضيًا settings.cache_ttl_seconds ثانية).
  - خيط تنظيف دوري لإزالة الدخول المنتهية وعدم ترك الذاكرة تنمو إلى الأبد.
  - clear() تُستدعى عند أي كتابة (إضافة/تعديل/حذف) حتى لا نُقدّم بيانات عتيقة.
  - clear_all() تُستخدم في الاختبارات وأي سياق يريد ذاكرة مؤقتة نظيفة.

لا يعتمد على Redis — يعمل داخل العملية نفسه، وهو مناسب للعدد الصغير من المستخدمين.
"""

import atexit
import copy
import logging
import threading
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_UNSET = object()

_lock = threading.RLock()
_store: dict[str, tuple[float, Any]] = {}

_sweeper_stop = threading.Event()
_sweeper_thread: threading.Thread | None = None
_started = False


def _now() -> float:
    return time.monotonic()


def _sweep_loop() -> None:
    while not _sweeper_stop.wait(10):
        try:
            now = _now()
            with _lock:
                expired = [k for k, (t, _) in _store.items() if now > t]
                for k in expired:
                    _store.pop(k, None)
        except Exception:
            logger.exception("خطأ في تنظيف ذاكرة التخزين المؤقت")


def start_sweeper() -> None:
    """يبدأ خيط التنظيف الدوري (يُستدعى مرة واحدة عند تشغيل البوت/الخادم)."""
    global _sweeper_thread, _started
    with _lock:
        if _started:
            return
        _started = True
        _sweeper_stop.clear()
        _sweeper_thread = threading.Thread(target=_sweep_loop, name="cache-sweeper", daemon=True)
        _sweeper_thread.start()


def stop_sweeper() -> None:
    """يوقف خيط التنظيف (لإغلاق نظيف / اختبارات)."""
    global _started
    with _lock:
        _sweeper_stop.set()
        _started = False


def get(key: str) -> Any:
    with _lock:
        item = _store.get(key)
        if item is None:
            return _UNSET
        exp, value = item
        if _now() > exp:
            _store.pop(key, None)
            return _UNSET
        return value


def set(key: str, value: Any, ttl_seconds: float | None = None) -> None:
    if value is None or value is _UNSET:
        return
    if ttl_seconds is None:
        ttl_seconds = max(1, settings.cache_ttl_seconds)
    with _lock:
        _store[key] = (_now() + ttl_seconds, value)


def get_or_set(key: str, factory, ttl_seconds: float | None = None) -> Any:
    """يعيد القيمة المخزنة أو يحسبها عبر factory ويخزّنها.

    يُعاد نسخة مستقلة (deepcopy) كي لا يغيّر المتصل البيانات المخزنة عن قصد
    أو بغير قصد.
    """
    cached = get(key)
    if cached is not _UNSET:
        return copy.deepcopy(cached)
    value = factory()
    set(key, value, ttl_seconds=ttl_seconds)
    return copy.deepcopy(value)


def clear(namespace: str | None = None, exact: bool = False) -> None:
    """يمسح كل الدخول أو فقط نطاق (prefix) معيّن — يُستدعى عند أي كتابة.

    exact=True يمسح مفتاحًا دقيقًا بذاته (بلا نقطتين لاحقة) أضافه المتصل مباشرة
    (مثل "admin_stats") دون بادئة namespace؛ الافتراضي يبقي سلوك البادئة كما هو.
    """
    with _lock:
        if namespace is None:
            _store.clear()
            return
        if exact:
            _store.pop(namespace, None)
            return
        prefix = namespace.rstrip(":") + ":"
        keys = [k for k in _store if k.startswith(prefix)]
        for k in keys:
            _store.pop(k, None)


def clear_all() -> None:
    clear()


atexit.register(stop_sweeper)
