"""
سجل التدقيق (Audit Log) لأوامر الحذف/التعديل الحساسة.

يُسجّل في logs/audit.log بشكل منفصل عن app.log العام — يحتوي فقط على
أفعال التأثير (حذف، تعديل، حذف ميزانية، تأكيد إنجاز) ولا يحتوي معلومات
شخصية敏感ة كاملة، فقط معرّف المستخدم والمعرّف والقيمة القديمة والجديدة.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from app.logging_config import LOGS_DIR

AUDIT_LOG = os.path.join(LOGS_DIR, "audit.log")

_MAX_BYTES = 2 * 1024 * 1024  # 2MB
_BACKUP_COUNT = 5

_configured = False
audit_logger = logging.getLogger("audit")


def setup_audit_log() -> None:
    """تهيئة سجل التدقيق مرة واحدة عند بدء التشغيل."""
    global _configured
    if _configured:
        return
    _configured = True

    audit_logger.propagate = False
    audit_logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s - AUDIT - %(message)s")
    handler = RotatingFileHandler(
        AUDIT_LOG, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    audit_logger.addHandler(handler)


def log_audit(
    user_id: int | None,
    action: str,
    target: str,
    detail: str = "",
) -> None:
    """تسجيل حدث تدقيق.

    action: "update" | "soft_delete" | "complete_task" | "delete_budget" | ...
    target: "transaction" | "task" | "note" | "budget" + id إن وُجد
    detail: معلومات مختصرة (مثل: fields changed, record description, etc.)
    """
    uid = user_id or "?"
    parts = [f"user={uid}", f"action={action}", f"target={target}"]
    if detail:
        parts.append(f"detail={detail}")
    audit_logger.info(" | ".join(parts))
