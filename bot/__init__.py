"""حزمة البوت: فصل منطق handlers / العرض / آلة الحالة من bot.py الأصلي.

- bot.formatters: تنسيق النتائج والرسائل (عرض فقط).
- bot.conversation: آلة الحالة (جمع النواقص → تأكيد → حفظ) عبر ConversationHandler.
- bot.handlers: أوامر بسيطة (start/done/undo) وتسجيل المعالجات.
- bot.ratelimit: حدّ معدل الرسائل مع تنظيف دوري للذاكرة.
"""

from bot.handlers import register_handlers

__all__ = ["register_handlers"]
