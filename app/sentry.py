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

        def _before_breadcrumb(crumb, hint):
            # لا تسجّل Breadcrumbs من مستوى INFO (تحوي نصوص المستخدم الحساسة مثل
            # fresh_entry "نتيجة التحليل" و update_*). ولا من logger audit.
            try:
                level = (crumb.get("level") or "").lower()
                category = (crumb.get("category") or "").lower()
                if level == "info":
                    return None
                if category == "audit":
                    return None
                # صفّ أي رسالة قد تحتوي حقولًا حساسة
                msg = str(crumb.get("message") or "")
                lower = msg.lower()
                sensitive_markers = ["amount", "person", "description", "نتيجة التحليل", "changes=", "fields_changed"]
                if any(m in lower for m in sensitive_markers):
                    crumb["message"] = "[REDACTED sensitive breadcrumb]"
                    if "data" in crumb:
                        crumb["data"] = {}
            except Exception:
                return None
            return crumb

        def _before_send(event, hint):
            # صفّ أي PII قد تسربت إلى الرسالة أو breadcrumbs
            try:
                # لا ترسل بيانات PII في الرسالة نفسها إن احتوت حقولًا حساسة
                msg = str(event.get("message") or "")
                if any(k in msg.lower() for k in ["person", "description"]):
                    event["message"] = "[REDACTED]"
                for bc in event.get("breadcrumbs", {}).get("values", []):
                    if "message" in bc and any(k in str(bc["message"]).lower() for k in ["person", "description"]):
                        bc["message"] = "[REDACTED]"
            except Exception:
                pass
            return event

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            environment=(getattr(settings, "app_env", None) or "development"),
            # لا نرسل بيانات مالية؛ Sentry تصفّي المتغيرات الحساسة مبدئيًا
            send_default_pii=False,
            before_breadcrumb=_before_breadcrumb,
            before_send=_before_send,
        )
        logger.info("Sentry مفعّل (DSN موجود).")
        return True
    except Exception as exc:  # noqa: BLE001
        # أي خلل في الإعداد يجب ألا يعطّل البوت — نتجاهله ونكمل
        logger.warning("فشل تهيئة Sentry (يتجاهل): %s", exc)
        return False
