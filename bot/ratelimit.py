"""
حدّ معدل الرسائل (rate limiting) مع تنظيف دوري للذاكرة.

- حد أقصى لعدد الرسائل ضمن نافذة زمنية لكل مستخدم (بالذاكرة).
- تتبّع محاولات الوصول الفاشلة لأوامر الأدمن (استكشاف صلاحيات) مع عتبة تُفعّل إنذارًا.
- تنظيف دوري عبر threading.Timer (daemon) لحذف إدخالات المستخدمين غير
  النشطين ومنع تسريب الذاكرة في عملية تعمل بشكل مستمر.
- كل هذا معزول في قفل (lock) لأن threading.Timer وأحداث البوت تعمل معًا.
"""

import threading
import time

RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60.0
GLOBAL_RATE_LIMIT_MAX = 100  # حد كلي لكل العملية (حماية من إغراق عام)
GLOBAL_RATE_LIMIT_WINDOW = 60.0
ADMIN_ATTEMPTS_MAX = 5  # عتبة محاولات الوصول الفاشلة قبل اعتبارها استكشاف صلاحيات
ADMIN_ATTEMPTS_WINDOW = 300.0
JOIN_ATTEMPTS_MAX = 5  # محاولات /join الفاشلة قبل الحظر
JOIN_ATTEMPTS_WINDOW = 900.0  # 15 دقيقة
_RATE_BUCKET_MAX_ENTRIES = 1024  # حد أقصى للإدخالات قبل التنظيف الفوري
_CLEANUP_INTERVAL = RATE_LIMIT_WINDOW  # دورة التنظيف الدوري

_RATE_LOCK = threading.Lock()
_rate_buckets: dict = {}
_global_stamps: list = []
_admin_denied: dict = {}
_join_denied: dict = {}
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
    global_start = now - GLOBAL_RATE_LIMIT_WINDOW
    _global_stamps[:] = [t for t in _global_stamps if t > global_start]


def _global_limit_reached(now: float) -> bool:
    """صحيح إذا بلغنا الحد الكلي (لكل العملية) ضمن النافذة — حماية من إغراق شامل.

    لا يسجّل الطابع هنا؛ التسجيل يتم فقط عند قبول الرسالة (الفحص حتى لا يُحصي
    المحجوبون ضمن ميزانية الإغراق العالمية فيُعتبرون مقبولين).
    """
    window_start = now - GLOBAL_RATE_LIMIT_WINDOW
    _global_stamps[:] = [t for t in _global_stamps if t > window_start]
    if len(_global_stamps) >= GLOBAL_RATE_LIMIT_MAX:
        return True
    return False


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
        if _global_limit_reached(now):
            return True
        window_start = now - RATE_LIMIT_WINDOW
        stamps = [t for t in _rate_buckets.get(user_id, []) if t > window_start]
        if len(stamps) >= RATE_LIMIT_MAX:
            _rate_buckets[user_id] = stamps
            return True
        stamps.append(now)
        _rate_buckets[user_id] = stamps
        _global_stamps.append(now)
        return False


def _prune_admin_denied(now: float) -> None:
    """يحذف تتبّعات محاولات الأدمن الفاشلة الأقدم من نافذة العتبة."""
    window_start = now - ADMIN_ATTEMPTS_WINDOW
    expired = [
        uid
        for uid, stamps in _admin_denied.items()
        if not stamps or stamps[-1] <= window_start
    ]
    for uid in expired:
        _admin_denied.pop(uid, None)


def _prune_join_denied(now: float) -> None:
    window_start = now - JOIN_ATTEMPTS_WINDOW
    expired = [uid for uid, stamps in _join_denied.items() if not stamps or stamps[-1] <= window_start]
    for uid in expired:
        _join_denied.pop(uid, None)


def register_admin_denied(user_id: int) -> int:
    """يسجّل محاولة وصول فاشلة لأمر أدمن ويعيد عددها خلال النافذة بعد التسجيل."""
    with _RATE_LOCK:
        now = time.monotonic()
        _prune_admin_denied(now)
        window_start = now - ADMIN_ATTEMPTS_WINDOW
        stamps = [t for t in _admin_denied.get(user_id, []) if t > window_start]
        stamps.append(now)
        _admin_denied[user_id] = stamps
        return len(stamps)


def admin_denied_count(user_id: int) -> int:
    """عدد محاولات الوصول الفاشلة لأوامر الأدمن خلال النافذة (بدون تسجيل)."""
    with _RATE_LOCK:
        now = time.monotonic()
        _prune_admin_denied(now)
        window_start = now - ADMIN_ATTEMPTS_WINDOW
        return len([t for t in _admin_denied.get(user_id, []) if t > window_start])


def is_admin_probing(user_id: int) -> bool:
    """صحيح إذا تجاوزت المحاولات الفاشلة عتبة استكشاف الصلاحيات."""
    return admin_denied_count(user_id) >= ADMIN_ATTEMPTS_MAX


def register_join_denied(user_id: int) -> int:
    with _RATE_LOCK:
        now = time.monotonic()
        _prune_join_denied(now)
        window_start = now - JOIN_ATTEMPTS_WINDOW
        stamps = [t for t in _join_denied.get(user_id, []) if t > window_start]
        stamps.append(now)
        _join_denied[user_id] = stamps
        return len(stamps)


def join_denied_count(user_id: int) -> int:
    with _RATE_LOCK:
        now = time.monotonic()
        _prune_join_denied(now)
        window_start = now - JOIN_ATTEMPTS_WINDOW
        return len([t for t in _join_denied.get(user_id, []) if t > window_start])


def is_join_blocked(user_id: int) -> bool:
    return join_denied_count(user_id) >= JOIN_ATTEMPTS_MAX


def _new_cleanup_timer():
    """مؤقت تنظيف دوري (خيط daemon) — يتجنّب كلمة ``daemon`` في منشئ Timer
    لأن Python 3.14 رفضها في threading.Timer؛ الضبط بعد الإنشاء متوافق مع كل النسخ."""
    timer = threading.Timer(_CLEANUP_INTERVAL, _cleanup_loop)
    timer.daemon = True
    return timer


def _cleanup_loop() -> None:
    """دورة واحدة من التنظيف، ثم تُجدول نفسها مجددًا عبر Timer (daemon)."""
    global _cleanup_timer, _last_prune
    with _RATE_LOCK:
        now = time.monotonic()
        _prune_rate_buckets(now)
        _prune_admin_denied(now)
        _prune_join_denied(now)
        _last_prune = now
    _cleanup_timer = _new_cleanup_timer()
    _cleanup_timer.start()


def start_cleanup() -> None:
    """يبدأ مؤقت التنظيف الدوري (خيط daemon — لا يمنع إيقاف العملية)."""
    global _cleanup_timer
    with _RATE_LOCK:
        if _cleanup_timer is None:
            _cleanup_timer = _new_cleanup_timer()
            _cleanup_timer.start()


def stop_cleanup() -> None:
    """يوقف مؤقت التنظيف الدوري (يُستدعى عند إيقاف البوت)."""
    global _cleanup_timer
    with _RATE_LOCK:
        if _cleanup_timer is not None:
            _cleanup_timer.cancel()
            _cleanup_timer = None
