"""
حدّ معدل الرسائل (rate limiting) مع تنظيف دوري للذاكرة.

- حد أقصى لعدد الرسائل ضمن نافذة زمنية لكل مستخدم (بالذاكرة).
- تنظيف دوري عبر threading.Timer (daemon) لحذف إدخالات المستخدمين غير
  النشطين ومنع تسريب الذاكرة في عملية تعمل بشكل مستمر.
- كل هذا معزول في قفل (lock) لأن threading.Timer وأحداث البوت تعمل معًا.
"""

import threading
import time

RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60.0
_RATE_BUCKET_MAX_ENTRIES = 1024  # حد أقصى للإدخالات قبل التنظيف الفوري
_CLEANUP_INTERVAL = RATE_LIMIT_WINDOW  # دورة التنظيف الدوري

_RATE_LOCK = threading.Lock()
_rate_buckets: dict = {}
_last_prune: float = 0.0
_cleanup_timer = None


def _prune_rate_buckets(now: float) -> None:
    """يحذف إدخالات المستخدمين غير النشطين (خارج نافذة الحدّ)."""
    window_start = now - RATE_LIMIT_WINDOW
    expired = [
        uid for uid, stamps in _rate_buckets.items() if not stamps or stamps[-1] <= window_start
    ]
    for uid in expired:
        _rate_buckets.pop(uid, None)


def is_rate_limited(user_id: int) -> bool:
    global _last_prune
    now = time.monotonic()
    with _RATE_LOCK:
        # تنظيف عند كل رسالة إن بلغنا السقف، أو دوريًا كل نافذة
        if _rate_buckets and (
            len(_rate_buckets) >= _RATE_BUCKET_MAX_ENTRIES or now - _last_prune >= RATE_LIMIT_WINDOW
        ):
            _prune_rate_buckets(now)
            _last_prune = now
        window_start = now - RATE_LIMIT_WINDOW
        stamps = [t for t in _rate_buckets.get(user_id, []) if t > window_start]
        if len(stamps) >= RATE_LIMIT_MAX:
            _rate_buckets[user_id] = stamps
            return True
        stamps.append(now)
        _rate_buckets[user_id] = stamps
        return False


def _cleanup_loop() -> None:
    """دورة واحدة من التنظيف، ثم تُجدول نفسها مجددًا عبر Timer (daemon)."""
    global _cleanup_timer, _last_prune
    with _RATE_LOCK:
        _prune_rate_buckets(time.monotonic())
        _last_prune = time.monotonic()
    _cleanup_timer = threading.Timer(_CLEANUP_INTERVAL, _cleanup_loop, daemon=True)
    _cleanup_timer.start()


def start_cleanup() -> None:
    """يبدأ مؤقت التنظيف الدوري (خيط daemon — لا يمنع إيقاف العملية)."""
    global _cleanup_timer
    with _RATE_LOCK:
        if _cleanup_timer is None:
            _cleanup_timer = threading.Timer(_CLEANUP_INTERVAL, _cleanup_loop, daemon=True)
            _cleanup_timer.start()


def stop_cleanup() -> None:
    """يوقف مؤقت التنظيف الدوري (يُستدعى عند إيقاف البوت)."""
    global _cleanup_timer
    with _RATE_LOCK:
        if _cleanup_timer is not None:
            _cleanup_timer.cancel()
            _cleanup_timer = None
