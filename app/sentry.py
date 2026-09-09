"""
تكامل Sentry الاختياري — صفر تكلفة عند عدم وجود DSN.

الغرض: عند تعبئة SENTRY_DSN في .env تُفعَّل مراقبة الأخطاء (الحوادث، الأخطاء
غير المتوقعة في البوت والداشبورد). عند تركه فارغًا (الوضع الافتراضي للتطوير)
لا يُستورد sentry_sdk إطلاقًا — لا يضيف حمولة أو طلبات شبكة.
"""

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def install_sentry() -> bool:
    """تهيئة Sentry إن وُجد DSN؛ تعيد True إذا فُعِّلت، وإلا False.

    تُستدعى مرة واحدة لكل عملية (lru_cache). استدعاء متكرر لا يضاعف البايندينغ.
    """
    from app.config import settings

    dsn = (settings.sentry_dsn or "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            environment="production",
            # لا نرسل بيانات مالية؛ Sentry تصفّي المتغيرات الحساسة مبدئيًا
            send_default_pii=False,
        )
        logger.info("Sentry مفعّل (DSN موجود).")
        return True
    except Exception as exc:  # noqa: BLE001
        # أي خلل في الإعداد يجب ألا يعطّل البوت — نتجاهله ونكمل
        logger.warning("فشل تهيئة Sentry (يتجاهل): %s", exc)
        return False
