"""
تشفير تطبيقي للحقول الحساسة (AES-GCM) عبر SQLAlchemy TypeDecorator.

الآلية:
  - المفتاح من ENCRYPTION_KEY في .env (base64-encoded 32 bytes).
  - عند غياب المفتاح يعمل النظام في "وضع واضح" (plaintext) مع تسجيل تحذير —
    حتى لا يفقد البيانات الحالية، ويستمر عمل الاختبارات/التطوير.
  - عند تفعيله: تُشفَّر القيم على الكتابة وتُفكَّر على القراءة بشفافية.
    القيم القديمة المخزنة كنص واضح تُفكَّر فشلًا → نعود للقيمة الخام (هجرة تدريجية).
  - قيمة تبدأ بـ v1$ ولا يمكن فكّها بالمفتاح الحالي (تدوير مفتاح أو خطأ ضبط):
    يُعرض بديل واضح بدل تسريب النص المشفَّر الخام للمستخدم النهائي.

التنسيق المخزن: "v1$<nonce b64>.<ciphertext b64>" (AES-256-GCM، nonce عشوائي).
"""

import base64
import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import settings

logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "v1$"
UNREADABLE_VALUE = "غير قابلة للقراءة"
_VALID_AES_KEY_LENGTHS = (16, 24, 32)
_warned_unset = False
_warned_invalid = False


def _decode_key_bytes(raw: str) -> bytes | None:
    """يفك مفتاحًا إلى بايتات AES-GCM صالحة (16/24/32) أو None.

    متسامح في صيغة الإدخال (Standard/URL-safe base64 أو نص مباشر) لكن صارم
    في الطول: `base64.b64decode` وحده متساهل ويَفُك أي نص "شبه base64" بطول
    تعسّفي، وتمرير طول غير صالح لـ AESGCM يرفع ValueError — لذا نتحقق هنا قبل
    الاستخدام بدل انهيار أول كتابة (create_transaction/create_note/create_task).

    القواعد:
    - إن تأوّل المفتاح كـ base64 صالح: يُقبل فقط بطول 16/24/32 بايت وإلا رُفض
      كاملًا (لا إعادة تأويل نصية — مفتاح base64 مضبوط خاطئًا يُكشف ولا يُخطئ).
    - إن لم يكن base64 صالحًا إطلاقًا: يُقبل كنص مباشر بطول 16/24/32 بايت
      (توافق المفاتيح النصية القديمة خارج الأبجدية، مثل العربية).
    """
    decoded: bytes | None = None
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        try:
            decoded = base64.urlsafe_b64decode(raw, validate=True)
        except Exception:
            decoded = None
    if decoded is not None:
        if len(decoded) in _VALID_AES_KEY_LENGTHS:
            return decoded
        return None
    key = raw.encode("utf-8")
    if len(key) in _VALID_AES_KEY_LENGTHS:
        return key
    return None


def _key_bytes() -> bytes | None:
    """يعيد مفتاح التشفير كـ bytes صالحة أو None (غير مضبوط / غير صالح)."""
    raw = (settings.encryption_key or "").strip()
    if not raw:
        return None
    key = _decode_key_bytes(raw)
    if key is not None:
        return key
    _warn_invalid_key()
    return None


def encryption_key_problem(raw: str) -> str | None:
    """يعيد وصف مشكلة ENCRYPTION_KEY أو None إن كان غائبًا/صالحًا.

    مفيد للتحقق عند الإقلاع (validate_env) وفي فحوصات /diag — مفتاح مضبوط
    لكن غير صالح لن يُكتشف بدونه إلا عند أول عملية كتابة (سابقًا بانهيار).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if _decode_key_bytes(raw) is not None:
        return None
    return (
        "مضبوط لكن غير صالح — يجب أن يُفك إلى 16/24/32 بايت (AES-GCM)؛ "
        "الحقول الحساسة ستُخزَّن كنص واضح ما لم يُصحَّح."
    )


def _warn_if_unset() -> None:
    global _warned_unset
    if not (settings.encryption_key or "").strip() and not _warned_unset:
        _warned_unset = True
        logger.warning("ENCRYPTION_KEY غير مضبوط في .env — الحقول الحساسة تُخزَّن كنص واضح!")


def _warn_invalid_key() -> None:
    global _warned_invalid
    if not _warned_invalid:
        _warned_invalid = True
        logger.warning(
            "ENCRYPTION_KEY مضبوط لكن غير صالح (يجب أن يُفك إلى 16/24/32 بايت). "
            "الحقول الحساسة تُخزَّن كنص واضح — راجع فحص التشفير في /diag."
        )


def encrypt_text(plain: str) -> str | None:
    """يشفّر نصًا ويعيد التوكن المخزن، أو None في الوضع الواضح.

    لا يرمي أي استثناء نحو الطبقة العليا: مفتاح غير صالح أو خطأ داخلي يسقط
    آمنًا للوضع الواضح مع تسجيل خطأ — بدل انهيار أول كتابة (create_transaction/
    create_note/create_task) كما كان يحصل مع مفتاح يُفك لطول غير صالح.
    """
    if plain is None:
        return None
    key = _key_bytes()
    if key is None:
        _warn_if_unset()
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = __import__("os").urandom(12)
        token = AESGCM(key).encrypt(nonce, str(plain).encode("utf-8"), None)
    except Exception:
        logger.exception("فشل تشفير قيمة حساسة — سقطت للوضع الواضح")
        return None
    return "{}{}.{}".format(
        _ENCRYPTED_PREFIX,
        base64.b64encode(nonce).decode("ascii"),
        base64.b64encode(token).decode("ascii"),
    )


def decrypt_text(token: str) -> str | None:
    """يفك تشفير توكن مخزّن ويعيد النص، أو None (كلمة غير مشفرة/فشل)."""
    if not token or not str(token).startswith(_ENCRYPTED_PREFIX):
        return None
    key = _key_bytes()
    if key is None:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        _, payload = str(token).split("$", 1)
        nonce_b64, ct_b64 = payload.split(".", 1)
        plain = AESGCM(key).decrypt(base64.b64decode(nonce_b64), base64.b64decode(ct_b64), None)
        return plain.decode("utf-8")
    except Exception:
        logger.exception("فشل فك تشفير قيمة مخزنة (قد تكون legacy plaintext)")
        return None


def is_encrypted(value) -> bool:
    return bool(value) and str(value).startswith(_ENCRYPTED_PREFIX)


class EncryptedString(TypeDecorator):
    """يخزّن نصًا مشفّرًا — القيمة القديمة (واضحة) تُقرأ كما هي حتى تُعاد كتابتها.

    impl=Text: يعمل على MySQL (TEXT يتسع للنص المشفر الطويل) وعلى SQLite (TEXT).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        enc = encrypt_text(value)
        return enc if enc is not None else str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        dec = decrypt_text(value)
        if dec is not None:
            return dec
        if is_encrypted(value):
            # قيمة مشفّرة لا تُقرأ بالمفتاح الحالي (تدوير مفتاح / ضبط خاطئ) —
            # لا نُسرّب النص المشفَّر الخام للمستخدم.
            return UNREADABLE_VALUE
        return str(value)


class EncryptedNumeric(TypeDecorator):
    """يخزّن مبلغًا (Decimal) كمكوّن نص مشفّر مع فك تلقائي على القراءة.

    ملاحظة: لا يمكن الجمع داخل SQL (SUM/AVG) على عمود مشفَّر — كل التجميعات
    المالية تجري في Python بعد فك التشفير (انظر crud.run_query وغيرها).
    """

    impl = (
        Text  # SQLite لا يفرض نوع العمود — النص المشفر يعمل على أي عمود؛ على MySQL TEXT يتسع دائمًا
    )
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        try:
            dec = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            # بدل كتابة NULL بصمت (فقدان بيانات بلا أثر في السجلات): نفشل الطلب
            # كي يُلتقط الخطأ في سجل الدعوة/المستدعي بدل إخفائه.
            raise ValueError(f"مبلغ غير صالح للعمود المشفَّر: {value!r}") from exc
        if not dec.is_finite():
            raise ValueError(f"مبلغ غير صالح (NaN/Infinity) للعمود المشفَّر: {value!r}")
        plain = str(dec)
        enc = encrypt_text(plain)
        return enc if enc is not None else plain

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        raw = str(value)
        dec = decrypt_text(raw)
        if dec is None and is_encrypted(raw):
            # قيمة مشفّرة بلا مفتاح صالح — لا نُسرّب النص الخام (المبالغ رقمية)
            return None
        if dec is None:
            dec = raw  # قيمة قديمة مخزنة كنص واضح
        try:
            return Decimal(dec).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None
