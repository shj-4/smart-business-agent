"""
الحد الأدنى القابل للتوسع للترجمة (ar/en) لواجهة القوائم.

التخزين الدائم في جدول user_prefs (crud.get_user_lang/set_user_lang)؛ هذا
الوحدة تحتفظ بكاش في الذاكرة لتفادي استعلام قاعدة لكل زر، مع ذاكرة افتراضية
"ar" لمن لم يضبط لغة بعد.

ملاحظة صادقة: الترجمة تغطي القوائم الرئيسية/الأدوات حاليًا، والردود النصية
الداخلية ما زالت عربية — البنية جاهزة لنقل السلسلة كاملة تباعًا.
"""

_STRINGS: dict[str, dict[str, str]] = {
    "main_title": {
        "ar": (
            "القائمة الرئيسية — اختر ما تريد:\n"
            "أو اكتب مباشرة أي عملية أو سؤال (مثل: 300 شيكل لمحمد مقابل مواد)."
        ),
        "en": (
            "Main menu — pick an option:\n"
            "Or just type a transaction or question (e.g. 300 ILS to Mohammed for materials)."
        ),
    },
    "btn_record": {"ar": "💰 تسجيل عملية", "en": "💰 New record"},
    "btn_reports": {"ar": "📊 التقارير", "en": "📊 Reports"},
    "btn_tasks": {"ar": "📋 مهامي", "en": "📋 My tasks"},
    "btn_tools": {"ar": "🧰 أدوات", "en": "🧰 Tools"},
    "btn_settings": {"ar": "⚙️ الإعدادات", "en": "⚙️ Settings"},
    "tools_title": {"ar": "🧰 أدوات — اختر:", "en": "🧰 Tools — choose:"},
    "btn_edit_last": {"ar": "✏️ تعديل آخر سجل", "en": "✏️ Edit last record"},
    "btn_budget": {"ar": "💰 الميزانيات", "en": "💰 Budgets"},
    "btn_chart": {"ar": "📈 الرسم البياني", "en": "📈 Chart"},
    "btn_export": {"ar": "📦 تصدير Excel", "en": "📦 Export Excel"},
    "btn_convert": {"ar": "💱 تحويل عملة", "en": "💱 Convert currency"},
    "btn_workspace": {"ar": "🏢 المساحة المشتركة", "en": "🏢 Shared workspace"},
    "btn_search": {"ar": "🔍 بحث", "en": "🔍 Search"},
    "btn_history": {"ar": "🕘 آخر العمليات", "en": "🕘 Recent records"},
    "btn_back": {"ar": "⬅️ رجوع", "en": "⬅️ Back"},
    "btn_home": {"ar": "🏠 القائمة الرئيسية", "en": "🏠 Main menu"},
    "lang_prompt": {
        "ar": "اختر لغة الواجهة:",
        "en": "Choose interface language:",
    },
    "lang_done_ar": {"ar": "الواجهة أصبحت بالعربية 🇸🇦", "en": "Interface set to Arabic 🇸🇦"},
    "lang_done_en": {"ar": "الواجهة أصبحت بالإنجليزية 🇬🇧", "en": "Interface set to English 🇬🇧"},
}

_LANG_CACHE: dict[int, str] = {}


def remember_lang(telegram_user_id: int, lang: str) -> None:
    """يحدّث كاش اللغة في الذاكرة بعد أي تبديل."""
    _LANG_CACHE[telegram_user_id] = "en" if lang == "en" else "ar"


def user_lang(telegram_user_id: int) -> str:
    """لغة الواجهة من الكاش (ar افتراضي). يُحمَّل من قاعدة البيانات عند أول طلب."""
    if telegram_user_id is None:
        return "ar"
    if telegram_user_id not in _LANG_CACHE:
        try:
            from app.database.crud import get_user_lang
            from app.database.db import SessionLocal

            db = SessionLocal()
            try:
                _LANG_CACHE[telegram_user_id] = get_user_lang(db, telegram_user_id)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — غياب الجدول/أخطاء مؤقتة لا تُسقط القوائم
            _LANG_CACHE[telegram_user_id] = "ar"
    return _LANG_CACHE.get(telegram_user_id, "ar")


def t(key: str, lang: str = "ar") -> str:
    """ترجمة مفتاح للغة المطلوبة مع سقوط آمن للعربية ثم المفتاح نفسه."""
    entry = _STRINGS.get(key)
    if not entry:
        return key
    return entry.get(lang, entry.get("ar", key))
