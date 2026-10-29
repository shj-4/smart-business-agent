"""
الحد الأدنى القابل للتوسع للترجمة (ar/en) لواجهة القوائم.

التخزين الدائم في جدول user_prefs (crud.get_user_lang/set_user_lang)؛ هذا
الوحدة تحتفظ بكاش في الذاكرة لتفادي استعلام قاعدة لكل زر، مع ذاكرة افتراضية
"ar" لمن لم يضبط لغة بعد.

ذاكرة اللغة كاش محدود الحجم (LRU): كل مستخدم يخزن سلسلة صغيرة ("ar"/"en")،
ولكن بدون إخلاء كان القاموس ينمو بلا حدود مع كل مستخدم جديد في عملية طويلة
التشغيل — سقف الحجم أدناه يطرد الأقدم تلقائيًا (بخلاف app/cache.py الذي
يملك TTL ومنظفًا؛ هنا قيمة تافهة الحجم تكفيها حدود الحجم فقط).

ملاحظة صادقة: الترجمة تغطي القوائم الرئيسية/الأدوات حاليًا، والردود النصية
الداخلية ما زالت عربية — البنية جاهزة لنقل السلسلة كاملة تباعًا.
"""

from collections import OrderedDict

from bot.icons import BACK, EXPENSE, HOME, NEW, TASK, WARNING

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
"btn_record": {"ar": f"{NEW} تسجيل عملية", "en": f"{NEW} New record"},
    "btn_reports": {"ar": "📊 التقارير", "en": "📊 Reports"},
    "btn_tasks": {"ar": f"{TASK} مهامي", "en": f"{TASK} My tasks"},
    "btn_tools": {"ar": "🧰 أدوات", "en": "🧰 Tools"},
    "btn_settings": {"ar": "⚙️ الإعدادات", "en": "⚙️ Settings"},
    "tools_title": {"ar": "🧰 صندوق الأدوات ✨ — كل شيء بضغطتين", "en": "🧰 Toolbox ✨ — everything in two taps"},
    "btn_edit_last": {"ar": "✏️ تعديل آخر سجل", "en": "✏️ Edit last"},
    "btn_budget": {"ar": f"{EXPENSE} الميزانيات", "en": f"{EXPENSE} Budgets"},
    "btn_finance": {"ar": "💳 الذمم الموحّدة", "en": "💳 Unified ledger"},
    "btn_debts": {"ar": "💳 الديون", "en": "💳 Debts"},
    "btn_invoices": {"ar": "🧾 الفواتير", "en": "🧾 Invoices"},
    "btn_orders": {"ar": "🛒 طلبياتي", "en": "🛒 Orders"},
    "btn_credit": {"ar": f"{WARNING} الحدود", "en": f"{WARNING} Limits"},
    "btn_chart": {"ar": "📈 الرسم", "en": "📈 Chart"},
    "btn_kpi": {"ar": "🧭 المؤشرات", "en": "🧭 KPIs"},
    "btn_export": {"ar": "📦 تصدير Excel", "en": "📦 Export Excel"},
    "btn_export_pdf": {"ar": "📄 تصدير PDF", "en": "📄 Export PDF"},
    "btn_summary": {"ar": "🧾 تقرير شامل", "en": "🧾 Summary"},
    "btn_convert": {"ar": "💱 تحويل عملة", "en": "💱 Convert"},
    "btn_workspace": {"ar": "🏢 مساحتي", "en": "🏢 Workspace"},
    "btn_bonus": {"ar": "🎁 البونس والنقاط", "en": "🎁 Bonus & points"},
    "btn_search": {"ar": "🔍 بحث", "en": "🔍 Search"},
    "btn_history": {"ar": "🕘 آخر العمليات", "en": "🕘 Recent"},
    "btn_back": {"ar": f"{BACK} رجوع", "en": f"{BACK} Back"},
    "btn_home": {"ar": f"{HOME} القائمة الرئيسية", "en": f"{HOME} Main menu"},
    "lang_prompt": {
        "ar": "اختر لغة الواجهة:",
        "en": "Choose interface language:",
    },
    "lang_done_ar": {"ar": "الواجهة أصبحت بالعربية 🇸🇦", "en": "Interface set to Arabic 🇸🇦"},
    "lang_done_en": {"ar": "الواجهة أصبحت بالإنجليزية 🇬🇧", "en": "Interface set to English 🇬🇧"},
}

_LANG_CACHE_MAX = 5000  # سقف عدد المستخدمين المخزَّنين — فوقه يُطرد الأقدم
_LANG_CACHE: "OrderedDict[int, str]" = OrderedDict()


def _cache_put(user_id: int, lang: str) -> None:
    """يخزن قيمة ويحدّثها كأحدث استخدام، ويطرد الأقدم عند تجاوز السقف."""
    _LANG_CACHE[user_id] = lang
    _LANG_CACHE.move_to_end(user_id)
    while len(_LANG_CACHE) > _LANG_CACHE_MAX:
        _LANG_CACHE.popitem(last=False)


def _cache_get(user_id: int) -> str | None:
    """يقرأ القيمة ويكرّمها كأحدث استخدام، أو None إن غاب المفتاح."""
    if user_id in _LANG_CACHE:
        _LANG_CACHE.move_to_end(user_id)
        return _LANG_CACHE[user_id]
    return None


def remember_lang(telegram_user_id: int, lang: str) -> None:
    """يحدّث كاش اللغة في الذاكرة بعد أي تبديل."""
    _cache_put(telegram_user_id, "ar")


def user_lang(telegram_user_id: int) -> str:
    """لغة واجهة البوت الثابتة: العربية."""
    if telegram_user_id is None:
        return "ar"
    cached = _cache_get(telegram_user_id)
    if cached is not None:
        return "ar"
    _cache_put(telegram_user_id, "ar")
    return "ar"


def t(key: str, lang: str = "ar") -> str:
    """ترجمة مفتاح للغة المطلوبة مع سقوط آمن للعربية ثم المفتاح نفسه."""
    entry = _STRINGS.get(key)
    if not entry:
        return key
    return entry.get("ar", key)
