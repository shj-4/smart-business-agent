"""
حدّ معدل الرسائل (rate limiting) مع تنظيف دوري للذاكرة.

- حد أقصى لعدد الرسائل ضمن نافذة زمنية لكل مستخدم (بالذاكرة).
- تتبّع محاولات الوصول الفاشلة لأوامر الأدمن (استكشاف صلاحيات) مع عتبة تُفعّل إنذارًا.
- تنظيف دوري عبر threading.Timer (daemon) لحذف إدخالات المستخدمين غير
  النشطين ومنع تسريب الذاكرة في عملية تعمل بشكل مستمر.
- كل هذا معزول في قفل (lock) لأن threading.Timer وأحداث البوت تعمل معًا.
"""

import json
import threading
import time
from pathlib import Path as _Path

RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60.0
# حد كلي لكل العملية سابقًا كان 100 فقط لكل المستخدمين مجتمعين — يخنق البوت
# مع 20 مستخدمًا فعالًا. الآن حد أعلى بكثير (1000) ويُعتبر لكل العملية
# كحماية من إغراق شامل فقط، لا حدًا ضيقًا لكل مستخدم (الحد الضيق هو RATE_LIMIT_MAX)
GLOBAL_RATE_LIMIT_MAX = 1000  # حد كلي لكل العملية (حماية من إغراق عام)
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

# --- قائمة السماح والحصة اليومية (البند 6) ---

# مسار تخزين المستخدمين المسجلين عبر /register
_REGISTERED_FILE = _Path(__file__).resolve().parent.parent / "data" / "registered_users.json"
_registered_users: set[int] = set()
_daily_counts: dict[tuple[int, str], int] = {}  # (user_id, date_str) -> count
_global_daily_counts: dict[str, int] = {}  # date_str -> count
_daily_date: str = ""  # آخر تاريخ تم تنظيفه


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


# ---------- قائمة السماح (allowlist) ----------

def _load_registered_users() -> None:
    """تحميل المستخدمين المسجلين من ملف JSON إن وجد."""
    global _registered_users
    try:
        if _REGISTERED_FILE.exists():
            data = json.loads(_REGISTERED_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _registered_users = {int(x) for x in data if str(x).isdigit() or isinstance(x, int)}
    except Exception:
        pass


def _save_registered_users() -> None:
    """حفظ المستخدمين المسجلين إلى ملف JSON."""
    try:
        _REGISTERED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _REGISTERED_FILE.write_text(json.dumps(sorted(_registered_users), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# حمّل عند الاستيراد
_load_registered_users()


def is_user_allowed(user_id: int) -> bool:
    """هل المستخدم مسموح له باستخدام البوت؟

    القواعد:
    - الأدمن دائمًا مسموح.
    - إن كانت ALLOWED_USER_IDS مضبوطة (غير فارغة) → يجب أن يكون ضمنها أو ضمن المسجلين.
    - إن كان REGISTRATION_TOKEN مضبوطًا → يجب أن يكون مسجلاً عبر /register أو ضمن ALLOWED_USER_IDS/أدمن.
    - إن لم تُضبط أي منهما → السماح للجميع (وضع تطوير/اختبار).
    """
    try:
        from app.config import settings
    except Exception:
        return True
    try:
        uid = int(user_id)
    except Exception:
        return False
    # أدمن دائمًا مسموح
    if uid in (getattr(settings, "admin_user_ids", []) or []):
        return True
    allowed = getattr(settings, "allowed_user_ids", []) or []
    token = (getattr(settings, "registration_token", "") or "").strip()
    # إن كانت قائمة السماح مضبوطة
    if allowed:
        if uid in allowed:
            return True
        if uid in _registered_users:
            return True
        return False
    # إن كان رمز التسجيل مضبوطًا ولم تكن قائمة السماح مضبوطة
    if token:
        if uid in _registered_users:
            return True
        return False
    # لا قيود
    return True


def register_via_token(user_id: int, token: str) -> bool:
    """يسجل المستخدم إذا كان الرمز صحيحًا. يعيد True عند النجاح."""
    try:
        from app.config import settings
    except Exception:
        return False
    expected = (getattr(settings, "registration_token", "") or "").strip()
    if not expected:
        return False
    if (token or "").strip() != expected:
        return False
    with _RATE_LOCK:
        _registered_users.add(int(user_id))
        _save_registered_users()
    return True


def is_user_registered(user_id: int) -> bool:
    with _RATE_LOCK:
        return int(user_id) in _registered_users


def get_allowed_users_count() -> int:
    with _RATE_LOCK:
        return len(_registered_users)


# ---------- الحصة اليومية ----------

def _today_str() -> str:
    """تاريخ اليوم بتوقيت Asia/Gaza كـ YYYY-MM-DD."""
    try:
        from app.timeutil import now_local
        return now_local().date().isoformat()
    except Exception:
        import datetime
        return datetime.date.today().isoformat()


def _prune_daily_counts(now_date: str) -> None:
    """يحذف إدخالات الأيام القديمة (غير اليوم)."""
    global _daily_counts, _global_daily_counts, _daily_date
    if _daily_date != now_date:
        # يوم جديد → امسح كل العدادات القديمة
        _daily_counts = {k: v for k, v in _daily_counts.items() if k[1] == now_date}
        _global_daily_counts = {k: v for k, v in _global_daily_counts.items() if k == now_date}
        _daily_date = now_date
    else:
        # حتى لو نفس اليوم، احذف أي تاريخ قديم متبقي
        _daily_counts = {k: v for k, v in _daily_counts.items() if k[1] == now_date}
        _global_daily_counts = {k: v for k, v in _global_daily_counts.items() if k == now_date}


def get_daily_count(user_id: int, date_str: str | None = None) -> int:
    if date_str is None:
        date_str = _today_str()
    with _RATE_LOCK:
        _prune_daily_counts(date_str)
        return _daily_counts.get((int(user_id), date_str), 0)


def get_global_daily_count(date_str: str | None = None) -> int:
    if date_str is None:
        date_str = _today_str()
    with _RATE_LOCK:
        _prune_daily_counts(date_str)
        return _global_daily_counts.get(date_str, 0)


def is_daily_limited(user_id: int) -> bool:
    """هل بلغ المستخدم أو الحد الكلي الحد اليومي؟"""
    try:
        from app.config import settings
        per_user = int(getattr(settings, "daily_ai_limit_per_user", 50) or 50)
        per_global = int(getattr(settings, "daily_ai_limit_global", 1000) or 1000)
    except Exception:
        per_user, per_global = 50, 1000
    date_str = _today_str()
    with _RATE_LOCK:
        _prune_daily_counts(date_str)
        if _daily_counts.get((int(user_id), date_str), 0) >= per_user:
            return True
        if _global_daily_counts.get(date_str, 0) >= per_global:
            return True
    return False


def check_and_increment_daily(user_id: int) -> bool:
    """يتحقق من الحصة اليومية ويزيد العداد إن كان مسموحًا.

    يعيد True إذا كان مسموحًا (وتم الزيادة)، False إذا بلغ الحد.
    يجب استدعاؤه قبل كل استدعاء Gemini (تحليل نص/صورة).
    """
    try:
        from app.config import settings
        per_user = int(getattr(settings, "daily_ai_limit_per_user", 50) or 50)
        per_global = int(getattr(settings, "daily_ai_limit_global", 1000) or 1000)
    except Exception:
        per_user, per_global = 50, 1000
    date_str = _today_str()
    with _RATE_LOCK:
        _prune_daily_counts(date_str)
        cur_user = _daily_counts.get((int(user_id), date_str), 0)
        cur_global = _global_daily_counts.get(date_str, 0)
        if cur_user >= per_user or cur_global >= per_global:
            return False
        _daily_counts[(int(user_id), date_str)] = cur_user + 1
        _global_daily_counts[date_str] = cur_global + 1
        return True


def reset_daily_counts() -> None:
    """لأغراض الاختبار: تصفير العدادات اليومية."""
    global _daily_counts, _global_daily_counts, _daily_date
    with _RATE_LOCK:
        _daily_counts.clear()
        _global_daily_counts.clear()
        _daily_date = _today_str()


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
