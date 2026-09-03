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

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# المنطقة الزمنية المحلية (قابلة للتكوين عبر متغير البيئة TIMEZONE)
_LOCAL_TZ_NAME = os.getenv("TIMEZONE", "Asia/Gaza")
LOCAL_TZ = ZoneInfo(_LOCAL_TZ_NAME)


def now_utc() -> datetime:
    """الآن بصيغة UTC (naive) — نفس صيغة التخزين في قاعدة البيانات."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_local() -> datetime:
    """الآن بالتوقيت المحلي (aware)."""
    return datetime.now(LOCAL_TZ)


def to_local_naive(utc_naive: datetime) -> datetime:
    """يحوّل قيمة مخزنة (UTC naive) إلى التوقيت المحلي (naive)."""
    if utc_naive is None:
        return None
    if utc_naive.tzinfo is None:
        utc_naive = utc_naive.replace(tzinfo=timezone.utc)
    return utc_naive.astimezone(LOCAL_TZ).replace(tzinfo=None)


def to_utc_naive(local_dt: datetime) -> datetime:
    """يحوّل توقيتًا محليًا (naive أو aware) إلى UTC (naive) للمقارنة مع المخزن."""
    if local_dt is None:
        return None
    if local_dt.tzinfo is None:
        # تعامل مع القيمة الـ naive كمحلية
        local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)
