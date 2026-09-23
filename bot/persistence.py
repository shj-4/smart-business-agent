"""
Persistence مرشحة لتجنب كتابة الحقول الحساسة على القرص.

PicklePersistence الافتراضي يكتب كل user_data نصًا واضحًا في
data/conversation.pickle*، مما يلغي فائدة التشفير في قاعدة البيانات
(confirm_result/pending_raw/edit_records/pending_record_edit_obj كلها
تحوي مبالغ/أشخاص/أوصاف مفكوكة التشفير).

هذا الملف يطبّق FilteredPicklePersistence التي ترث PicklePersistence
وتسقط المفاتيح الحساسة عند الكتابة فقط (البيانات تبقى في الذاكرة
للجلسة الحالية، لكن لا تُحفظ على القرص). عند إعادة التشغيل يفقد
المستخدم شاشة التأكيد المعلّقة ويُطلب منه إعادة الإرسال — ثمن مقبول
مقابل عدم تسريب البيانات المشفّرة.

خيار بديل مذكور في التقرير هو تخزين معرّفات فقط أو تشفير الحالة؛
الإسقاط أبسط ويحقق الهدف الأمني دون تعقيد.
"""

from copy import deepcopy
from pathlib import Path
from typing import Any

from telegram.ext import PicklePersistence

# مفاتيح user_data التي تحوي بيانات حساسة (مبالغ/أشخاص/أوصاف/نص خام)
# يجب ألا تُكتب على القرص. الحذف هنا لا يمس الذاكرة الحالية — فقط
# النسخة التي تُحفظ عبر pickle.
SENSITIVE_USER_DATA_KEYS: set[str] = {
    # تأكيد وتجميع النواقص (قبل الحفظ)
    "confirm_result",
    "confirm_raw",
    "pending_raw",
    "pending_record",
    # تعديل السجلات
    "edit_records",
    "pending_record_edit_obj",
    # بحث وشركة (قد تحوي أسماء أشخاص/أوصاف شركة)
    "pending_search_term",
    "co_name",
    "co_desc",
}


def _filter_user_data(
    user_data: dict[int, dict[str, Any]] | None,
) -> dict[int, dict[str, Any]] | None:
    if not user_data:
        return user_data
    filtered: dict[int, dict[str, Any]] = {}
    for uid, data in user_data.items():
        if not isinstance(data, dict):
            filtered[uid] = deepcopy(data)
            continue
        # انسخ بدون المفاتيح الحساسة
        clean = {k: deepcopy(v) for k, v in data.items() if k not in SENSITIVE_USER_DATA_KEYS}
        filtered[uid] = clean
    return filtered


def _is_user_data_file(filepath: Path) -> bool:
    name = filepath.name
    return "user_data" in name


class FilteredPicklePersistence(PicklePersistence):
    """PicklePersistence تُسقط الحقول الحساسة عند الكتابة للقرص.

    الذاكرة الحالية (self.user_data) تبقى كاملة للجلسة، لكن أي كتابة
    للقرص (update_user_data / _dump_singlefile / _dump_file / flush)
    تستخدم نسخة مرشحة بدون المفاتيح الحساسة.
    """

    def _dump_singlefile(self) -> None:
        # استبدال user_data بمرشح عند الكتابة فقط
        original = self.user_data
        try:
            self.user_data = _filter_user_data(original)
            super()._dump_singlefile()
        finally:
            self.user_data = original

    def _dump_file(self, filepath: Path, data: object) -> None:
        if _is_user_data_file(filepath) and isinstance(data, dict):
            # data هنا هو self.user_data (dict[int, dict])
            filtered = _filter_user_data(data)  # type: ignore[arg-type]
            super()._dump_file(filepath, filtered)
        else:
            super()._dump_file(filepath, data)

    async def update_user_data(self, user_id: int, data: dict) -> None:  # type: ignore[override]
        # احتفظ بالنسخة الكاملة في الذاكرة
        if self.user_data is None:
            self.user_data = {}
        # تحقق تغيير مرشح؟ نقارن المرشح لتجنب كتابة زائدة
        # لكن نخزن الكامل في self.user_data
        if self.user_data.get(user_id) == data:
            return
        self.user_data[user_id] = data
        if not self.on_flush:
            if not self.single_file:
                # اكتب مرشح
                filtered = _filter_user_data(self.user_data)
                self._dump_file(Path(f"{self.filepath}_user_data"), filtered)
            else:
                self._dump_singlefile()

    async def flush(self) -> None:
        # flush يستخدم _dump_singlefile/_dump_file لذا نفس الترشيح يطبّق
        # نتجاوز السلوك الافتراضي لضمان الترشيح
        if self.single_file:
            if (
                self.user_data
                or self.chat_data
                or self.bot_data
                or self.callback_data
                or self.conversations
            ):
                self._dump_singlefile()
        else:
            if self.user_data:
                filtered = _filter_user_data(self.user_data)
                self._dump_file(Path(f"{self.filepath}_user_data"), filtered)
            if self.chat_data:
                self._dump_file(Path(f"{self.filepath}_chat_data"), self.chat_data)
            if self.bot_data:
                self._dump_file(Path(f"{self.filepath}_bot_data"), self.bot_data)
            if self.callback_data:
                self._dump_file(Path(f"{self.filepath}_callback_data"), self.callback_data)
            if self.conversations:
                self._dump_file(Path(f"{self.filepath}_conversations"), self.conversations)


def scrub_existing_pickle(filepath: str | Path) -> bool:
    """ينظّف ملفات pickle القديمة التي قد تحوي مفاتيح حساسة.

    يعيد True إذا تم التنظيف (أُعيدت الكتابة)، False إذا لم يوجد ملف
    أو لم يحتوِ حساسًا. آمن للاستدعاء عند كل إقلاع.
    """
    import pickle

    path = Path(filepath)
    cleaned = False
    # single_file
    if path.exists() and path.is_file():
        try:
            with path.open("rb") as f:
                data = pickle.load(f)
            if isinstance(data, dict) and "user_data" in data:
                orig = data["user_data"]
                filtered = _filter_user_data(orig)
                if filtered != orig:
                    data["user_data"] = filtered
                    # استخدم نفس بروتوكول الكتابة مع _BotPickler إن أمكن، وإلا pickle عادي
                    try:
                        from unittest.mock import MagicMock

                        from telegram.ext._picklepersistence import _BotPickler

                        bot_mock = MagicMock()
                        with path.open("wb") as out:
                            _BotPickler(bot_mock, out, protocol=pickle.HIGHEST_PROTOCOL).dump(data)
                    except Exception:
                        with path.open("wb") as out:
                            pickle.dump(data, out, protocol=pickle.HIGHEST_PROTOCOL)
                    cleaned = True
        except Exception:
            pass
    # multi-file user_data
    multi = Path(f"{path}_user_data")
    if multi.exists() and multi.is_file():
        try:
            with multi.open("rb") as f:
                # حاول مع _BotUnpickler أولًا
                try:
                    from unittest.mock import MagicMock

                    from telegram.ext._picklepersistence import _BotUnpickler

                    bot_mock = MagicMock()
                    data = _BotUnpickler(bot_mock, f).load()
                except Exception:
                    f.seek(0)
                    data = pickle.load(f)
            if isinstance(data, dict):
                filtered = _filter_user_data(data)
                if filtered != data:
                    try:
                        from unittest.mock import MagicMock

                        from telegram.ext._picklepersistence import _BotPickler

                        bot_mock = MagicMock()
                        with multi.open("wb") as out:
                            _BotPickler(bot_mock, out, protocol=pickle.HIGHEST_PROTOCOL).dump(
                                filtered
                            )
                    except Exception:
                        with multi.open("wb") as out:
                            pickle.dump(filtered, out, protocol=pickle.HIGHEST_PROTOCOL)
                    cleaned = True
        except Exception:
            pass
    return cleaned
