"""
مُرافق ضبط اتجاه النص (Bidi) للرسائل ثنائية الاتجاه.

النصوص العربية داخل البوت تحقن مقاطع لاتينية (ILS, USD, تواريخ 2026-09-10,
رموز callback...) داخل جملة RTL بلا علامات اتجاه، فيظهر الترتيب مقلوبًا أو
متداخلًا عند المستخدم (خصوصًا عند تتابع أكثر من مقطع لاتيني). الحل: تطويق كل
مقطع لاتيني بعلامتي RLM (Right-to-Left Mark) حوله ليظل ترتيبه ثابتًا، بدل
الاعتماد على خوارزمية Bidi التلقائية في عميل تيليجرام.

النقطة الموحَّدة للتطبيق: `install_bidi_patches()` تُثبّت غلافًا واحدًا على
طرق الإرسال في python-telegram-bot (Message.reply_text,
CallbackQuery.edit_message_text, Bot.send_message) فيمرر جميع النصوص النهائية
الصادرة من البوت عبر `fix_bidi` — بلا تصحيح يدوي لكل سطر.
"""

import logging
import re
from functools import wraps

logger = logging.getLogger(__name__)

_LATIN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,:/_-]*")
LRM = "\u200e"  # Left-to-Right Mark
RLM = "\u200f"  # Right-to-Left Mark
# جميع الرموز الاتجاهية التي قد تُحقن عبر fix_bidi أو النسخ
_BIDI_CHARS = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_BIDI_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")

# نمط الأكواد داخل backticks (مفردة أو ثلاثية) — يجب استثناؤها من التطويق
_BACKTICK_RE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)


def _strip_bidi(text: str) -> str:
    """يزيل كل الرموز الاتجاهية من النص لجعل fix_bidi idempotent."""
    return _BIDI_RE.sub("", text)


def fix_bidi(text: str) -> str:
    """يطوّق كل مقطع لاتيني (عملة/تاريخ/رقم إنجليزي) بعلامتي RLM
    كي يبقى ترتيبه صحيحًا داخل جملة عربية RTL، بدل الاعتماد على
    خوارزمية Bidi التلقائية التي قد تقلب الترتيب عند وجود أكثر من مقطع
    لاتيني متتالٍ.

    المقاطع اللاتينية تشمل الأرقام الغربية والتواريخ مثل 2026-09-10 ورموز
    العملات والكلمات الإنجليزية؛ المقاطع العربية (بما فيها الأرقام
    العربية-الهندية ٠-٩) لا تتأثر.

    يُستثنى الأوامر (/command) والروابط (http/https/t.me) والأكواد
    داخل backticks من التطويق كي لا تُكسر (نسخ الكود مع RLM يفشل /join).
    الغلاف idempotent: إزالة الرموز الموجودة أولاً يمنع تضاعفها عند
    تطبيق الطبقتين Message.reply_text → Bot.send_message.
    """
    if not text:
        return text

    # idempotent: إزالة أي RLM/LRM سابقة قبل إعادة التطويق
    # هذا يمنع تضاعف العلامات عند تغليف reply_text و send_message معًا
    if any(c in text for c in (RLM, LRM)) or _BIDI_RE.search(text):
        # لا نُزيل كل شيء إذا كان النص يحتوي RLM بالفعل من تغليف سابق،
        # بل نُزيلها ثم نُعيد التطبيق لضمان نتيجة واحدة
        text = _strip_bidi(text)

    # فكّك النص إلى مقاطع: أكواد backticks مقابل نص عادي
    # الأكواد داخل `...` أو ```...``` لا تُطوّق أبدًا
    bidi_parts = []
    last_b = 0
    for m in _BACKTICK_RE.finditer(text):
        # نص قبل الكود — طبّق عليه Bidi مع حماية الروابط/الأوامر
        if m.start() > last_b:
            bidi_parts.append(_fix_bidi_outside_backticks(text[last_b:m.start()]))
        # الكود نفسه — لا تغيّر
        bidi_parts.append(m.group(0))
        last_b = m.end()
    if last_b < len(text):
        bidi_parts.append(_fix_bidi_outside_backticks(text[last_b:]))
    return "".join(bidi_parts)


def _fix_bidi_outside_backticks(segment: str) -> str:
    """يطبّق Bidi على مقطع خارج backticks مع حماية الروابط/الأوامر."""
    # فكّك النص إلى مقاطع: روابط/أوامر مقابل نص عادي
    # الروابط: http://, https://, t.me/...
    # الأوامر: /xxx في بداية السطر أو بعد مسافة (لا وسط كلمة مثل edit:1/x)
    # نُوسّع حماية الأمر لتشمل التوكن بعده مثل "/join 12345678" كاملًا
    url_pattern = re.compile(r"https?://\S+|t\.me/\S+|(?<!\S)/\w[\w/]*(?:\s+\S+)?")
    parts = []
    last = 0
    for m in url_pattern.finditer(segment):
        # نص قبل الرابط/الأمر — طبّق عليه Bidi
        if m.start() > last:
            parts.append(_wrap_latin(segment[last:m.start()]))
        # الرابط/الأمر نفسه (مع التوكن إن وجد) — لا تغيّر
        parts.append(m.group(0))
        last = m.end()
    if last < len(segment):
        parts.append(_wrap_latin(segment[last:]))
    return "".join(parts)


def _wrap_latin(segment: str) -> str:
    def _wrap(m: re.Match) -> str:
        return f"{RLM}{m.group(0)}{RLM}"

    return _LATIN_RUN.sub(_wrap, segment)


def strip_bidi_marks(text: str) -> str:
    """يزيل الرموز الاتجاهية (RLM/LRM/Isolate...) من نص — للاستخدام في redeem_invite."""
    if not text:
        return text
    return _BIDI_RE.sub("", text)


def _apply_fix(text_arg_index: int, args: tuple, kwargs: dict):
    """يعيد (args, kwargs) بعد تطبيق fix_bidi على معامل النص (موقعيًا أو كلمة
    مفتاحية) — قابل للاختبار دون الاعتماد على كائنات telegram."""
    text = None
    new_args = list(args)
    if len(new_args) > text_arg_index:
        text = new_args[text_arg_index]
        new_args[text_arg_index] = fix_bidi(text)
        return tuple(new_args), kwargs
    if "text" in kwargs and kwargs["text"] is not None:
        new_kwargs = dict(kwargs)
        new_kwargs["text"] = fix_bidi(new_kwargs["text"])
        return args, new_kwargs
    return args, kwargs


_PATCHED = False


def install_bidi_patches() -> None:
    """يُثبّت غلافًا واحدًا على طرق الإرسال في python-telegram-bot لتمرير كل
    نص صادر عبر fix_bidi — نقطة موحّدة بدل تصحيح كل reply_text/edit يدويًا.

    آمن عند التكرار (يُطبَّق مرة واحدة فقط)، واختبارات الوحدة لا تتأثر لأنها
    تستبدل الطريقة على مستوى الـ instance بـ AsyncMock فتظل تحصل على النص الخام.
    """
    global _PATCHED  # noqa: PLW0603
    if _PATCHED:
        return
    _PATCHED = True

    from telegram import Bot, CallbackQuery, Message

    _patch_send_method(Message, "reply_text", text_arg_index=0)
    _patch_send_method(CallbackQuery, "edit_message_text", text_arg_index=0)
    _patch_send_method(Bot, "send_message", text_arg_index=1)
    logger.debug("ثُبّتت غلافات Bidi على طرق الإرسال")


def _patch_send_method(cls, method_name: str, text_arg_index: int) -> None:
    """يلفّ أسلوب الإرسال method_name داخل cls ليمرر نصّه عبر fix_bidi."""
    original = getattr(cls, method_name)

    @wraps(original)
    async def _bidi_wrapper(self, *args, **kwargs):
        args, kwargs = _apply_fix(text_arg_index, args, kwargs)
        return await original(self, *args, **kwargs)

    setattr(cls, method_name, _bidi_wrapper)
