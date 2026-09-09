"""
أداة موحّدة للتعامل مع المناطق الزمنية (Timezone).

القاعدة المعتمدة:
- التخزين في قاعدة البيانات (created_at, due_date) يكون بصيغة UTC (naive) — توقيت موحّد.
- الحدود الزمنية للمستخدم (اليوم/الأسبوع/الشهر) تُحسب بالتوقيت المحلي (فلسطين: Asia/Gaza)
  ثم تُحوَّل إلى UTC للمقارنة مع القيم المخزنة.
- "التاريخ المرجعي" المرسل لـ Gemini وكل ما يظهر للمستخدم يُعرض بالتوقيت المحلي.

المنطقة الزمنية الافتراضية: Asia/Gaza (فلسطين، UTC+2 في الشتاء و UTC+3 في الصيف).
يمكن تغييرها عبر متغير البيئة TIMEZONE في ملف .env (مثل "Asia/Gaza" أو "Asia/Jerusalem").
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import settings

# المنطقة الزمنية المحلية (من Pydantic Settings / .env)
LOCAL_TZ = ZoneInfo(settings.timezone)

# أوّل يوم في الأسبوع (من Pydantic Settings / .env)
# القيمة بصيغة weekday() الخاصة بـ Python حيث الاثنين=0 ... الأحد=6.
# الافتراضي 6 (الأحد) لأن الأسبوع في فلسطين/السياق العربي يبدأ غالبًا من الأحد.
DEFAULT_FIRST_DAY = int(settings.first_day_of_week)


def first_day_of_week() -> int:
    """يعيد أوّل يوم في الأسبوع (0=الاثنين, 6=الأحد) محصورًا في نطاق صالح."""
    day = DEFAULT_FIRST_DAY % 7
    return day


def now_utc() -> datetime:
    """الآن بصيغة UTC (naive) — نفس صيغة التخزين في قاعدة البيانات."""
    return datetime.now(UTC).replace(tzinfo=None)


def now_local() -> datetime:
    """الآن بالتوقيت المحلي (aware)."""
    return datetime.now(LOCAL_TZ)


def to_local_naive(utc_naive: datetime) -> datetime:
    """يحوّل قيمة مخزنة (UTC naive) إلى التوقيت المحلي (naive)."""
    if utc_naive is None:
        return None
    if utc_naive.tzinfo is None:
        utc_naive = utc_naive.replace(tzinfo=UTC)
    return utc_naive.astimezone(LOCAL_TZ).replace(tzinfo=None)


def to_utc_naive(local_dt: datetime) -> datetime:
    """يحوّل توقيتًا محليًا (naive أو aware) إلى UTC (naive) للمقارنة مع المخزن."""
    if local_dt is None:
        return None
    if local_dt.tzinfo is None:
        # تعامل مع القيمة الـ naive كمحلية
        local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
    return local_dt.astimezone(UTC).replace(tzinfo=None)
