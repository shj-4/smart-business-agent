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

        # EventScrubber لتصفية الحقول الحساسة تلقائيًا
        try:
            from sentry_sdk.scrubber import EventScrubber

            scrubber = EventScrubber(
                denylist=[
                    "password",
                    "token",
                    "secret",
                    "api_key",
                    "apikey",
                    "authorization",
                    "cookie",
                    "amount",
                    "person",
                    "description",
                    "category",
                    "currency",
                    "raw_message",
                    "telegram_user_id",
                    "amount_in_base_currency",
                ],
                recursive=True,
            )
        except Exception:
            scrubber = None

        _SENSITIVE_RE = None
        try:
            import re

            _SENSITIVE_RE = re.compile(
                r"(amount|person|description|category|currency|raw_message|telegram_user_id|parameters\s*:|bot\d+:[A-Za-z0-9_-]{20,})",
                re.IGNORECASE,
            )
        except Exception:
            pass

        def _scrub_text(text: str | None) -> str:
            if not text:
                return text  # type: ignore
            try:
                if _SENSITIVE_RE and _SENSITIVE_RE.search(str(text)):
                    return "[REDACTED sensitive data]"
                # IntegrityError parameters تحتوي القيم الحساسة
                if "[parameters:" in str(text) or "parameters" in str(text).lower():
                    return "[REDACTED IntegrityError parameters]"
            except Exception:
                pass
            return text

        def _scrub_vars(vars_dict: dict | None) -> None:
            if not isinstance(vars_dict, dict):
                return
            for k in list(vars_dict.keys()):
                if any(s in k.lower() for s in ["amount", "person", "description", "category", "currency", "raw_message", "result", "telegram_user_id"]):
                    try:
                        vars_dict[k] = "[REDACTED]"
                    except Exception:
                        pass

        def _before_send(event, hint):
            # صفّ أي PII قد تسربت إلى الرسالة أو breadcrumbs أو المتغيرات المحلية
            try:
                # رسالة الحدث نفسه
                if event.get("message"):
                    event["message"] = _scrub_text(str(event.get("message")))
                # استخدم EventScrubber إن توفر
                if scrubber is not None:
                    try:
                        # scrubber.scrub_event يعدل الحدث في المكان
                        scrubber.scrub_event(event)
                    except Exception:
                        pass
                # exception.values[].value  (مثل IntegrityError مع [parameters: ...])
                for exc in event.get("exception", {}).get("values", []) or []:
                    if exc.get("value"):
                        exc["value"] = _scrub_text(str(exc.get("value")))
                    # frames → vars (include_local_variables لو بقيت مفعّلة)
                    for frame in exc.get("stacktrace", {}).get("frames", []) or []:
                        _scrub_vars(frame.get("vars"))
                # threads.values[].stacktrace.frames[].vars
                for thr in event.get("threads", {}).get("values", []) or []:
                    for frame in thr.get("stacktrace", {}).get("frames", []) or []:
                        _scrub_vars(frame.get("vars"))
                # breadcrumbs
                for bc in event.get("breadcrumbs", {}).get("values", []) or []:
                    if "message" in bc and bc["message"]:
                        bc["message"] = _scrub_text(str(bc["message"]))
                        if any(k in str(bc["message"]).lower() for k in ["person", "description"]):
                            bc["message"] = "[REDACTED]"
                    if "data" in bc and isinstance(bc["data"], dict):
                        for dk in list(bc["data"].keys()):
                            if any(s in dk.lower() for s in ["amount", "person", "description"]):
                                bc["data"][dk] = "[REDACTED]"
                    # لو كان هناك vars في breadcrumb (نادر)
                    if "vars" in bc:
                        _scrub_vars(bc.get("vars"))
                # extra
                if "extra" in event and isinstance(event["extra"], dict):
                    for ek in list(event["extra"].keys()):
                        if any(s in ek.lower() for s in ["amount", "person", "description"]):
                            event["extra"][ek] = "[REDACTED]"
            except Exception:
                # لا نكسر الإرسال بسبب خطأ في التصفية
                pass
            return event

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            environment=(getattr(settings, "app_env", None) or "development"),
            # لا نرسل بيانات مالية؛ Sentry تصفّي المتغيرات الحساسة مبدئيًا
            send_default_pii=False,
            include_local_variables=False,
            event_scrubber=scrubber,
            before_breadcrumb=_before_breadcrumb,
            before_send=_before_send,
        )
        logger.info("Sentry مفعّل (DSN موجود).")
        return True
    except Exception as exc:  # noqa: BLE001
        # أي خلل في الإعداد يجب ألا يعطّل البوت — نتجاهله ونكمل
        logger.warning("فشل تهيئة Sentry (يتجاهل): %s", exc)
        return False
