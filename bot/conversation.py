"""
آلة الحالة لالتقاط التسجيلات: تحليل → (جمع النواقص) → تأكيد → حفظ.

مبنية على ConversationHandler من python-telegram-bot بدل إدارة user_data يدويًا.
يحل هذا بشكل طبيعي مشكلة الحالة المعلّقة: أي رسالة نصية/وسائط جديدة أثناء
شاشة التأكيد تعيد بدء المحادثة (allow_reentry) مع مسح الحالة القديمة.
"""

import asyncio
import logging
import re
from decimal import Decimal
from decimal import InvalidOperation as DecimalInvalidOperation

from sqlalchemy.orm import Session
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.ai_queue import aiq
from app.ai_service import analyze_message, analyze_receipt_image, transcribe_audio
from app.database.crud import (
    complete_task,
    create_note,
    create_task,
    create_transaction,
    find_pending_task,
    get_record_by_id,
    normalize_currency,
    record_correction_feedback,
    run_query,
    update_note,
    update_task,
    update_transaction,
)
from app.database.db import SessionLocal
from app.normalize import normalize_priority
from bot.formatters import (
    FIELD_LABELS,
    TYPE_NAMES,
    _build_confirm_keyboard,
    _build_confirm_text,
    format_query_result,
    format_record_result,
    safe_reply,
)
from bot.icons import BACK, SUCCESS
from bot.ratelimit import is_rate_limited

logger = logging.getLogger(__name__)

# حالات المحادثة
COLLECT = 1  # ننتظر إكمال بيانات ناقصة
CONFIRM = 2  # ننتظر تأكيد/إلغاء (أزرار InlineKeyboard)

QUICK_ACTIONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("➕ تسجيل عملية", callback_data="menu:record")],
    [InlineKeyboardButton("📊 أسئلة مالية", callback_data="menu:reports"), InlineKeyboardButton("💱 تحويل عملة", callback_data="tool:convert")],
])

_AMOUNT_RE = re.compile(r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<cur>[\u0600-\u06FF\w]+)?")

# النوع (من نتيجة التحليل) → نموذج /edit (نفس حقول محادثة /edit)
_CONFIRM_TYPE_MODEL = {
    "expense": "Transaction",
    "income": "Transaction",
    "task": "Task",
    "order": "Note",
    "note": "Note",
}


def _confirm_editable_fields(result: dict) -> list[str]:
    from bot.editing import EDITABLE_FIELDS

    model = _CONFIRM_TYPE_MODEL.get(result.get("type"), "Note")
    fields = list(EDITABLE_FIELDS.get(model, []))
    # النتيجة المؤقتة للسجل الجديد تخزّن موعد المهمة تحت مفتاح "date"
    return ["date" if f == "due_date" else f for f in fields]


def _confirm_value_preview(result: dict, field: str) -> str:
    val = result.get(field)
    if field == "amount" and val is not None:
        return f"{val} {result.get('currency') or ''}".strip()
    return val or "(فارغ)"


def apply_confirm_edit(result: dict, field: str, raw: str) -> tuple[dict, str | None]:
    """يطبّق قيمة نصّية على حقل في نتائج التأكيد. يُعيد (result, خطأ|None)."""
    value = raw.strip()
    if field == "amount":
        m = _AMOUNT_RE.match(value)
        if not m:
            return result, "لم أتعرف على رقم صالح. مثال: 300 أو 300.5"
        num = m.group("num").replace(",", ".")
        try:
            amount = Decimal(num)
        except DecimalInvalidOperation:
            return result, "المبلغ غير صالح."
        result["amount"] = amount
        cur = m.group("cur")
        if cur:
            norm = normalize_currency(cur)
            if norm:
                result["currency"] = norm
        return result, None
    if field == "currency":
        norm = normalize_currency(value)
        if not norm:
            return result, "عملة غير معروفة. جرّب: شيكل / دولار / دينار / يورو."
        result["currency"] = norm
        return result, None
    if field == "priority":
        result["priority"] = normalize_priority(value)
        return result, None
    if field == "date":
        from app.database.crud import parse_date_local

        if not value:
            return result, FIELD_LABELS.get("date", "الموعد") + " لا يمكن أن يكون فارغًا."
        parsed = parse_date_local(value)
        if parsed is not None:
            result["date"] = value
            return result, None
        # نص حر (غدًا/بكرة/بعد يومين...) — يُفسَّر بالذكاء الاصطناعي مثل مسار
        # الإدخال الأصلي بدل إضاعته صامتًا (كان create_* يستدعي parse_date_local
        # مباشرة على النص الخام فتضيع قيمة الموعد بلا تنبيه).
        import app.ai_service as ai_service

        interpreted = ai_service.interpret_arabic_date(value)
        if interpreted:
            result["date"] = interpreted
            return result, None
        return result, "لم أتعرف على الموعد. جرّب مثلًا: 2026-09-10 10:00"
    if not value:
        return result, f"{FIELD_LABELS.get(field, field)} لا يمكن أن يكون فارغًا."
    result[field] = value
    return result, None


_BUDGET_TEXT_RE = re.compile(r"^(?P<target>.+?)\s+(?P<amount>\d+(?:[.,]\d+)?)$", re.UNICODE)

_CONVERT_CONNECTORS = ("إلى", "الي", "الى", "من", "بح", "to", "in")


def parse_budget_text(scope: str, raw: str) -> dict | None:
    """يقرأ "الهدف والمبلغ" بصيغة حرة: "ILS 2000" أو "محمد 1500". يعيد dict أو None."""
    text = (raw or "").strip()
    text = re.sub(r"^(شخص|عملة)\s*[:\-]", "", text, flags=re.UNICODE).strip()
    m = _BUDGET_TEXT_RE.match(text)
    if not m:
        return None
    amount = Decimal(m.group("amount").replace(",", "."))
    target = m.group("target").strip()
    if scope == "currency":
        code = normalize_currency(target)
        if not code:
            return {"error": "عملة غير معروفة. جرّب رمزًا مثل ILS/USD/JOD أو شيكل/دولار/دينار"}
        target = code
    elif scope == "category" and not target:
        return {"error": "أرسل اسم التصنيف والمبلغ (مثال: مشتريات 2000)"}
    return {"scope": scope, "target": target, "monthly_limit": amount}


def parse_convert_text(raw: str) -> dict | None:
    """يقرأ "المبلغ من إلى" بصيغة حرة. يعيد {amount, from, to} أو None."""
    from app.database.crud import CURRENCY_ALIASES
    from app.exchange import CURRENCY_NAMES

    text = (raw or "").strip().strip("?؟")
    nums = re.findall(r"\d+(?:[.,]\d+)?", text)
    if not nums:
        return None
    amount = Decimal(nums[0].replace(",", "."))
    stripped = re.sub(r"\d+(?:[.,]\d+)?", " ", text)
    for connector in _CONVERT_CONNECTORS:
        stripped = stripped.replace(connector, " ")
    tokens = [t for t in stripped.split() if t]

    codes: list[str] = []
    for token in tokens:
        low = token.lower()
        if low in CURRENCY_ALIASES:
            code = CURRENCY_ALIASES[low]
        elif token.upper() in CURRENCY_NAMES:
            code = token.upper()
        else:
            continue
        if code not in codes:
            codes.append(code)
        if len(codes) >= 2:
            break

    if len(codes) < 2:
        return None
    return {"amount": amount, "from": codes[0], "to": codes[1]}


_RATE_KEYWORDS = re.compile(
    r"(?:كم\s*صرف|كم\s*سعر|بكم|سعر\s*(?:ال)?صرف|كم\s*يساوي|كم\s*تساوي|كم\s*توازي|تحويل\s*(?:عملة)?\s*(?:من|الى|إلى))",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(
    r"^(?:مرحبا|اهلا|أهلا|هلا|هاي|صباح الخير|مساء الخير|السلام عليكم|سلام|hi|hello|hey|yo)",
    re.IGNORECASE,
)


def parse_rate_question(raw: str) -> dict | None:
    """يكتشف سؤال سعر صرف بلا مبلغ: "كم صرف شيكل على دينار" → {"from": "ILS", "to": "JOD"}."""
    text = (raw or "").strip().strip("?؟!.")
    if not text or len(text) < 4:
        return None
    if _RATE_KEYWORDS.search(text):
        from app.database.crud import CURRENCY_ALIASES
        from app.exchange import CURRENCY_NAMES

        tokens = re.split(r"[\s,،!?.]+", text)
        codes: list[str] = []
        for t in tokens:
            low = t.lower()
            code = CURRENCY_ALIASES.get(low)
            if code is None:
                code = t.upper() if t.upper() in CURRENCY_NAMES else None
            if code is None:
                for alias, acode in CURRENCY_ALIASES.items():
                    if alias in low and len(alias) >= 2:
                        code = acode
                        break
            if code and code not in codes:
                codes.append(code)
            if len(codes) >= 2:
                break
        if len(codes) >= 2:
            return {"from": codes[0], "to": codes[1]}
    return None


def is_greeting(text: str) -> bool:
    """يحدد إذا كان النص تحية (مرحبا/أهلا/هلا/hi/...)."""
    return bool(_GREETING_RE.match((text or "").strip().strip("?؟!.")))


async def budget_add_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """نص حر من زر "إضافة ميزانية" (عملة/شخص) → إنشاء الميزانية."""
    pending = context.user_data.pop("pending_budget", None)
    if not pending:
        return await text_router(update, context)

    scope = pending.get("scope", "currency")
    parsed = parse_budget_text(scope, update.message.text)
    from bot.menus import MAIN_HOME_KEYBOARD

    if parsed is None:
        example = {
            "currency": "ILS 2000",
            "person": "محمد 1500",
            "category": "مشتريات 2000",
        }.get(scope, "ILS 2000")
        await update.message.reply_text(f"لم أفهم الصيغة. أرسل مثل: {example}")
        return None
    if parsed.get("error"):
        await update.message.reply_text(parsed["error"])
        return None

    from app.database.crud import create_budget

    uid = update.effective_user.id
    db = SessionLocal()
    try:
        budget = create_budget(db, uid, scope, parsed["target"], str(parsed["monthly_limit"]))
    finally:
        db.close()

    if not budget:
        await update.message.reply_text(
            "لم تُنشأ الميزانية. ربما ميزانية بنفس النطاق والهدف موجودة مسبقًا أو المبلغ غير صالح.",
            reply_markup=MAIN_HOME_KEYBOARD,
        )
        return None

    scope_txt = {
        "currency": budget.currency,
        "person": f"الشخص {budget.person}",
        "category": f"التصنيف {budget.category}",
    }.get(budget.scope, budget.scope)
    await update.message.reply_text(
        f"{SUCCESS} أُنشئت ميزانية شهرية: {scope_txt} — {budget.monthly_limit}\n"
        "سأرسل تنبيهًا عند اقترابك من السقف وتجاوزه.",
        reply_markup=MAIN_HOME_KEYBOARD,
    )
    return None


async def convert_add_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نص حر من زر "تحويل عملة" → تحويل وعرض الناتج."""
    context.user_data.pop("pending_convert", None)
    parsed = parse_convert_text(update.message.text)
    from bot.menus import MAIN_HOME_KEYBOARD

    if parsed is None:
        await update.message.reply_text(
            "لم أفهم صيغة التحويل. أرسل مثل:\n• 300 ILS إلى USD\n• 500 دولار إلى شيكل"
        )
        return None

    from app.exchange import CURRENCY_NAMES, convert

    result = await asyncio.to_thread(
        convert, parsed["amount"], parsed["from"], parsed["to"]
    )
    if "error" in result:
        await update.message.reply_text(result["error"], reply_markup=MAIN_HOME_KEYBOARD)
        return None

    from_name = CURRENCY_NAMES.get(result["from"], result["from"])
    to_name = CURRENCY_NAMES.get(result["to"], result["to"])
    await update.message.reply_text(
        f"{result['amount']} {from_name} = {result['result']} {to_name}\n"
        f"(سعر الصرف: 1 {result['from']} = {result['rate']} {result['to']})",
        reply_markup=MAIN_HOME_KEYBOARD,
    )
    return None


async def workspace_invite_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نص حر من زر "إضافة عضو" → دعوة المعرّف الرقمي إلى المساحة المشتركة."""
    context.user_data.pop("pending_ws_invite", None)
    from bot.menus import MAIN_HOME_KEYBOARD

    digits = re.sub(r"[^\d]", "", update.message.text or "")
    if not digits:
        await update.message.reply_text(
            "أرسل المعرّف الرقمي فقط (أرقام مثل: 555666777).", reply_markup=MAIN_HOME_KEYBOARD
        )
        return None

    from app.database.crud import invite_to_workspace

    db = SessionLocal()
    try:
        ok = invite_to_workspace(db, update.effective_user.id, int(digits))
    finally:
        db.close()
    if ok:
        await update.message.reply_text(
            f"{SUCCESS} أُرسلت دعوة للعضو {digits} إلى مساحتك المشتركة.\n"
            "لن تُدمج بياناتكما حتى يقبل الطرف الدعوة بنفسه "
            "(سيراها في قائمة المساحة المشتركة أو عبر /work).",
            reply_markup=MAIN_HOME_KEYBOARD,
        )
    else:
        await update.message.reply_text(
            "تعذّرت الدعوة: لست المرتكز (المالك)، أو أرسلت معرّفك الخاص، "
            "أو الطرف عضو نشط في مساحة أخرى (لا سحب دون موافقته).",
            reply_markup=MAIN_HOME_KEYBOARD,
        )
    return None


# ---------- أدوات تنظيف الحالة المعلّقة ----------


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يمسح بيانات مرحلة جمع النواقص."""
    for key in (
        "pending_record",
        "pending_type",
        "pending_raw",
        "pending_missing",
        "pending_message_id",
    ):
        context.user_data.pop(key, None)


def _clear_confirm_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يمسح حالة شاشة التأكيد المعلّقة."""
    for key in ("confirm_result", "confirm_raw", "confirm_message_id", "confirm_editing_field"):
        context.user_data.pop(key, None)


def _clear_seeds(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يمسح "بذور" القوائم المعلّقة (تسجيل بنية، ميزانية، تحويل، تعديل مهمة، دعوة مساحة)."""
    for key in (
        "record_seed_type",
        "pending_budget",
        "pending_convert",
        "pending_task_edit_id",
        "pending_ws_invite",
        "pending_record_edit_id",
        "pending_record_edit_model",
        "pending_record_edit_field",
        "pending_search",
        "pending_search_term",
    ):
        context.user_data.pop(key, None)


def _clear_all_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_pending(context)
    _clear_confirm_state(context)
    _clear_seeds(context)


# ---------- منطق التسجيل (مشترك بين النص والوسائط) ----------


def can_save_record(result: dict) -> bool:
    data_type = result.get("type")
    if data_type in ("expense", "income"):
        return result.get("amount") is not None
    if data_type == "task":
        return bool(result.get("description") or result.get("person"))
    if data_type == "complete_task":
        return bool(result.get("description"))
    if data_type in ("order", "note"):
        return bool(result.get("description") or result.get("person"))
    return False


def _do_save(
    db: Session,
    telegram_user_id: int,
    result: dict,
    raw_message: str | None,
    telegram_message_id: int | None = None,
) -> str | None:
    data_type = result.get("type")

    if not can_save_record(result):
        return None

    if data_type in ("expense", "income"):
        obj = create_transaction(db, telegram_user_id, result, raw_message, telegram_message_id)
        if obj is None:
            return "تم تسجيل هذه العملية مسبقًا (تكرار)."
        return "تم حفظ العملية."

    if data_type == "task":
        obj = create_task(db, telegram_user_id, result, raw_message, telegram_message_id)
        if obj is None:
            return "تم تسجيل هذه المهمة مسبقًا (تكرار)."
        return "تم حفظ المهمة."

    if data_type == "complete_task":
        description_hint = result.get("description")
        if description_hint:
            task = find_pending_task(db, telegram_user_id, description_hint)
            if task:
                complete_task(db, telegram_user_id, task.id)
                return f"تم إنجاز المهمة: {task.description}"
            return "لم أجد مهمة مطابقة ضمن مهامك المعلّقة."
        return "حدد المهمة التي تريد إنجازها (مثلاً: أنجزت مهمة الاتصال بسامر)"

    if data_type in ("order", "note"):
        obj = create_note(db, telegram_user_id, result, raw_message, telegram_message_id)
        if obj is None:
            return "تم تسجيل هذا السجل مسبقًا (تكرار)."
        type_ar = "الطلبية" if data_type == "order" else "الملاحظة"
        return f"تم حفظ {type_ar}."

    return None


def save_record(
    db: Session,
    telegram_user_id: int,
    result: dict,
    raw_message: str | None,
    telegram_message_id: int | None = None,
) -> str | None:
    return _do_save(db, telegram_user_id, result, raw_message, telegram_message_id)


def missing_fields_for(result: dict) -> list[str]:
    data_type = result.get("type")
    missing = []

    if data_type in ("expense", "income"):
        if result.get("amount") is None:
            missing.append("amount")
    elif data_type == "task":
        if not result.get("description") and not result.get("person"):
            missing.append("description")
    elif data_type == "complete_task":
        if not result.get("description"):
            missing.append("description")
    elif data_type in ("order", "note"):
        if not result.get("description") and not result.get("person"):
            missing.append("description")

    return missing


def ask_for_field_prompt(data_type: str, field: str) -> str:
    label = FIELD_LABELS.get(field, field)
    type_label = TYPE_NAMES.get(data_type, data_type)
    if field == "amount":
        return f"عملية {type_label}: ما هو المبلغ؟ (مثال: 300 أو 300 شيكل)"
    if field == "currency":
        return "ما هي العملة؟ (مثال: شيكل، دولار، دينار)"
    if field == "person":
        return "ما اسم الشخص (المورد/العميل)؟ (مثال: محمد)"
    if field == "date":
        return "ما هو الموعد؟ (مثال: غدًا الساعة 10)"
    return f"أرسل {label} من فضلك."


def fill_field_from_reply(partial: dict, reply_result: dict, field: str) -> None:
    if field == "amount":
        if reply_result.get("amount") is not None:
            partial["amount"] = reply_result["amount"]
        if reply_result.get("currency") and not partial.get("currency"):
            partial["currency"] = reply_result["currency"]
    elif field == "currency":
        if reply_result.get("currency"):
            partial["currency"] = reply_result["currency"]
    elif field == "person":
        if reply_result.get("person"):
            partial["person"] = reply_result["person"]
    elif field == "description":
        if reply_result.get("description"):
            partial["description"] = reply_result["description"]
        elif reply_result.get("person"):
            partial["person"] = reply_result["person"]
    elif field == "date":
        if reply_result.get("date"):
            partial["date"] = reply_result["date"]


def handle_query_intent(result: dict, telegram_user_id: int) -> str:
    db = SessionLocal()
    try:
        query_result = run_query(db, telegram_user_id, result.get("query_details") or {})
    finally:
        db.close()
    return format_query_result(query_result)


async def _start_collect(
    update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, raw_text: str, msg_id: int
) -> None:
    """يخزّن التسجيل الناقص في user_data ويسأل المستخدم عن أول حقل ناقص."""
    data_type = result.get("type")
    missing = missing_fields_for(result)

    context.user_data["pending_record"] = result
    context.user_data["pending_type"] = data_type
    context.user_data["pending_raw"] = raw_text
    context.user_data["pending_missing"] = missing
    context.user_data["pending_message_id"] = msg_id

    first = missing[0]
    reply = (
        f"سجّلت لك {TYPE_NAMES.get(data_type, data_type)} لكن تنقصه بيانات:\n"
        f"• {FIELD_LABELS.get(first, first)}"
    )
    if len(missing) > 1:
        reply += " (وبعدها أرجو إكمال بقية البنود)"
    reply += "\n" + ask_for_field_prompt(data_type, first)
    reply += "\n\n(لإلغاء الأمر أرسل /cancel)"
    await update.message.reply_text(reply)


def _send_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, raw_text: str, msg_id: int
) -> int:
    """يعرض شاشة التأكيد ويحوّل إلى حالة CONFIRM."""
    context.user_data["confirm_result"] = result
    context.user_data["confirm_raw"] = raw_text
    context.user_data["confirm_message_id"] = msg_id
    return CONFIRM


async def _handle_record_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, raw_text: str, msg_id: int
) -> int | None:
    """يوجّه نتيجة record: إنجاز فوري، جمع نواقص، أو شاشة تأكيد."""
    data_type = result.get("type")

    if not data_type or data_type == "unknown" or data_type not in TYPE_NAMES:
        await update.message.reply_text(format_record_result(result, False))
        return None

    if data_type == "complete_task":
        from bot.menus import MAIN_HOME_KEYBOARD

        def _save():
            db = SessionLocal()
            try:
                return save_record(db, update.effective_user.id, result, raw_text, msg_id)
            finally:
                db.close()

        reply = await asyncio.to_thread(_save)
        await update.message.reply_text(
            reply or "لم أستطع فهم مهمة محددة لإنجازها.", reply_markup=MAIN_HOME_KEYBOARD
        )
        return None

    missing = missing_fields_for(result)
    if missing:
        await _start_collect(update, context, result, raw_text, msg_id)
        return COLLECT

    _clear_pending(context)
    await update.message.reply_text(
        _build_confirm_text(result),
        reply_markup=_build_confirm_keyboard(),
    )
    return _send_confirm(update, context, result, raw_text, msg_id)


async def _handle_query(update: Update, result: dict) -> None:
    reply = await asyncio.to_thread(handle_query_intent, result, update.effective_user.id)
    await safe_reply(update.message, reply)
    return None


# ---------- دخول المحادثة ----------


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """نقطة دخول النصوص: تعديل حقل التأكيد → تعديل مهمة → ميزانية → تحويل → جمع نواقص → تسجيل/استعلام."""
    if context.user_data.get("confirm_editing_field"):
        return await edit_confirm_field_value(update, context)
    if context.user_data.get("pending_task_edit_id"):
        return await task_edit_value(update, context)
    if context.user_data.get("pending_record_edit_id"):
        return await record_edit_value(update, context)
    if context.user_data.get("pending_search"):
        return await search_value(update, context)
    if context.user_data.get("pending_budget"):
        return await budget_add_value(update, context)
    if context.user_data.get("pending_convert"):
        return await convert_add_value(update, context)
    if context.user_data.get("pending_ws_invite"):
        return await workspace_invite_value(update, context)
    if context.user_data.get("pending_missing"):
        return await collect_reply(update, context)
    return await fresh_entry(update, context)


async def fresh_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """يحلّل رسالة نصية جديدة ويوجّهها (تسجيل/استعلام/محادثة)."""
    # نوع صريح من زر "تسجيل عملية ← نوع" يُحتفظ به قبل مسح الحالة المعلّقة
    seed = context.user_data.pop("record_seed_type", None)
    _clear_all_pending(context)
    user_text = update.message.text
    telegram_user_id = update.effective_user.id
    msg_id = update.message.message_id

    if is_rate_limited(telegram_user_id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return None

    rate_q = parse_rate_question(user_text)
    if rate_q:
        from app.exchange import CURRENCY_NAMES, convert

        rate_result = await asyncio.to_thread(convert, "1", rate_q["from"], rate_q["to"])
        if "error" in rate_result:
            await update.message.reply_text(rate_result["error"])
        else:
            from_name = CURRENCY_NAMES.get(rate_result["from"], rate_result["from"])
            to_name = CURRENCY_NAMES.get(rate_result["to"], rate_result["to"])
            await update.message.reply_text(
                f"💱 سعر الصرف اليوم:\n"
                f"1 {from_name} = {rate_result['rate']} {to_name}"
            )
        return None

    result = await asyncio.to_thread(analyze_message, user_text)
    logger.info("نتيجة التحليل: %s", result)

    if seed and result.get("intent") == "record":
        result["type"] = seed

    intent = result.get("intent")

    if intent == "record":
        return await _handle_record_result(update, context, result, user_text, msg_id)

    if intent == "query":
        return await _handle_query(update, result)

    if intent == "chat":
        chat_reply = (result or {}).get("reply") or (
            "أهلًا! كيف أقدر أساعدك؟ سجّل عملية أو اسأل عن ميزانيتك."
            if is_greeting(user_text)
            else "أعتذر، ما فهمت رسالتك. سجّل عملية أو اسأل عن ميزانيتك."
        )
        await update.message.reply_text(chat_reply, reply_markup=QUICK_ACTIONS)
    else:
        await update.message.reply_text(
            "أعتذر، ما فهمت رسالتك. جرّب:\n"
            "• تسجيل عملية: \"دفعت 300 شيكل لمحمد\"\n"
            "• سؤال: \"كم لي عند محمد؟\"\n"
            "• سعر صرف: \"كم صرف شيكل على دينار\""
        )
    return None


async def collect_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """يملأ الحقل الناقص من ردّ المستخدم (حالة COLLECT)."""
    partial = context.user_data.get("pending_record")
    data_type = context.user_data.get("pending_type")
    raw = context.user_data.get("pending_raw")
    pending_msg_id = context.user_data.get("pending_message_id")

    if not partial or not data_type:
        await update.message.reply_text("أعتذر، انتهت جلسة الإكمال. أرسل العملية من جديد من فضلك.")
        _clear_all_pending(context)
        return None

    reply_result = await asyncio.to_thread(analyze_message, update.message.text)

    missing = context.user_data.get("pending_missing") or missing_fields_for(partial)
    field = missing[0]
    fill_field_from_reply(partial, reply_result, field)

    remaining = missing_fields_for(partial)
    context.user_data["pending_missing"] = remaining

    if remaining:
        first = remaining[0]
        await update.message.reply_text(
            "لا يزال يلزم:\n"
            f"• {FIELD_LABELS.get(first, first)}\n" + ask_for_field_prompt(data_type, first)
        )
        return COLLECT

    # اكتملت البيانات → شاشة تأكيد (قبلها نمسح بيانات الجمع لكي لا نخلطها بجمع جديد)
    raw_confirm = raw if raw is not None else ""
    _clear_pending(context)
    await update.message.reply_text(
        _build_confirm_text(partial),
        reply_markup=_build_confirm_keyboard(),
    )
    return _send_confirm(update, context, partial, raw_confirm, pending_msg_id)


# ---------- سجل التحليل الخاطئ (تحسين مستمر) ----------


def _record_feedback(
    context: ContextTypes.DEFAULT_TYPE, source: str, telegram_user_id: int
) -> None:
    """يسجّل رسالةً قرر المستخدم أنها خاطئة (إلغاء أو رفض تأكيد).

    تُخزَّن النص الأصلي (مشفّر في قاعدة البيانات) في جدول correction_feedback
    لمراجعتها يدويًا وتحسين الـ prompt. لا يحجب تدفق المحادثة أبدًا.
    """
    raw = context.user_data.get("pending_raw") or context.user_data.get("confirm_raw") or ""
    data_type = (
        (context.user_data.get("pending_record") or {}).get("type")
        or (context.user_data.get("confirm_result") or {}).get("type")
        or None
    )
    if not raw.strip():
        return
    db = SessionLocal()
    try:
        record_correction_feedback(db, telegram_user_id, raw[:2000], source, data_type)
    except Exception:
        logger.exception("فشل تسجيل التحليل الخاطئ (التدفق يستمر)")
    finally:
        db.close()


# ---------- الوسائط (صوت/ملف/مقطع/صورة فاتورة) ----------


class MediaTooLargeError(Exception):
    """الملف الصوتي يتجاوز الحد الأقصى المسموح."""


def _check_file_size(message: Message) -> None:
    """يرمي MediaTooLargeError إذا حجم الملف يتجاوز الحد المسموح."""
    from app.config import settings

    limit_bytes = (settings.max_voice_file_mb or 20) * 1024 * 1024
    file_size = 0
    if message.voice:
        file_size = message.voice.file_size or 0
    elif message.audio:
        file_size = message.audio.file_size or 0
    elif message.video_note:
        file_size = message.video_note.file_size or 0
    elif message.photo:
        file_size = (message.photo[-1].file_size or 0) if message.photo else 0
    elif message.document:
        file_size = message.document.file_size or 0
    if file_size and file_size > limit_bytes:
        raise MediaTooLargeError(
            f"حجم الملف ({file_size // (1024 * 1024)}MB) يتجاوز الحد الأقصى "
            f"({settings.max_voice_file_mb}MB)."
        )


async def _extract_media(update: Update) -> tuple[bytes | None, str | None, str]:
    """ينزّل ملف الوسيط ويعيد (بايتات, mime, نوع الوسيط) — بدون تحليل AI.

    فحص الحجم يتم قبله مباشرة (في media_router) كي نرفض الملفات الضخمة
    قبل أي تنزيل. التحويل/التحليل يُنفَّذ لاحقًا عبر طابور الـ AI (اختياري).
    """
    message = update.message
    if message.voice:
        voice = await message.voice.get_file()
        data = await voice.download_as_bytearray()
        return bytes(data), "audio/ogg", "صوت"
    if message.audio:
        audio = message.audio
        file = await audio.get_file()
        data = await file.download_as_bytearray()
        mime = audio.mime_type
        if not mime:
            name = audio.file_name or ""
            mime = (
                "audio/ogg"
                if name.endswith(".ogg")
                else "audio/mpeg"
                if name.endswith(".mp3")
                else "audio/mp4"
                if name.endswith(".m4a")
                else "audio/ogg"
            )
        return bytes(data), mime, "ملف صوتي"
    if message.video_note:
        file = await message.video_note.get_file()
        data = await file.download_as_bytearray()
        return bytes(data), "video/mp4", "مقطع مرئي"
    if message.photo:
        # أكبر نسخة من الصورة المرسلة = فاتورة/إيصال محتمل
        photo = message.photo[-1]
        file = await photo.get_file()
        data = await file.download_as_bytearray()
        return bytes(data), "image/jpeg", "فاتورة (صورة)"
    if message.document:
        # مستند PDF = فاتورة/إيصال رقمي محتمل
        doc = message.document
        file = await doc.get_file()
        data = await file.download_as_bytearray()
        mime = doc.mime_type or "application/pdf"
        return bytes(data), mime, "فاتورة (PDF)"
    return None, None, ""


def _receipt_to_text(result: dict | None) -> str:
    """يحوّل نتيجة قراءة الفاتورة إلى ملخص نصي (لعرضه في التأكيد/التسجيل)."""
    if not result or result.get("type") == "unknown":
        return ""
    parts = []
    if result.get("amount") is not None:
        currency = result.get("currency") or ""
        parts.append(f"المبلغ {result['amount']} {currency}".strip())
    if result.get("person"):
        parts.append(f"المورد: {result['person']}")
    if result.get("date"):
        parts.append(f"التاريخ: {result['date']}")
    if result.get("description"):
        parts.append(result["description"])
    label = TYPE_NAMES.get(result.get("type"), "فاتورة")
    return f"{label}: " + " — ".join(parts) if parts else label


def _analyze_media_sync(
    media_bytes: bytes, mime: str, media_kind: str
) -> tuple[str, str, dict | None]:
    """تعرف صوتي + تحليل نص، أو قراءة فاتورة (صورة) — دالة متزامنة للطابور.

    يعيد (نص, نوع الوسيط, نتيجة التحليل أو None إن لم يُفهم).
    """
    if mime and (mime.startswith("image/") or mime == "application/pdf"):
        result = analyze_receipt_image(media_bytes, mime_type=mime)
        return media_kind, _receipt_to_text(result), result
    text = transcribe_audio(media_bytes, mime_type=mime) if media_bytes else ""
    if not text:
        return media_kind, "", None
    return media_kind, text, analyze_message(text)


MEDIA_FILTER = filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.PHOTO | filters.Document.PDF


async def media_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """نقطة دخول الوسائط: نسخ → تحليل → نفس مسار التسجيل/الاستعلام."""
    _clear_all_pending(context)
    telegram_user_id = update.effective_user.id

    if is_rate_limited(telegram_user_id):
        await update.message.reply_text("لقد أرسلت الكثير من الرسائل. انتظر قليلًا ثم حاول مجددًا.")
        return None

    try:
        # فحص الحجم قبل أي تنزيل (حماية الموارد/التكلفة)
        _check_file_size(update.message)
        audio_bytes, mime, media_kind = await _extract_media(update)
    except MediaTooLargeError as exc:
        await update.message.reply_text(str(exc))
        return None
    except Exception:
        logger.exception("خطأ في تنزيل الملف الصوتي")
        await update.message.reply_text("حدث خطأ أثناء تحميل الملف الصوتي، حاول مرة أخرى")
        return None

    # التعرف + التحليل عبر طابور الـ AI (إذا مفعّل) — وإلا مباشرةً كالسابق
    media_kind, text, result = await aiq.submit(
        lambda: _analyze_media_sync(audio_bytes, mime, media_kind)
    )

    if (result or {}).get("injection_guard"):
        await update.message.reply_text(
            "هذه الصورة تحتوي على تعليمات مضمّنة داخل نصها؛ تجاهلتها حفاظًا على أمان حسابك."
        )
        return None

    if not text:
        await update.message.reply_text(f"لم أستطع فهم الـ{media_kind}، حاول مرة أخرى أو أرسل نصًا.")
        return None

    intent = (result or {}).get("intent")
    msg_id = update.message.message_id

    if intent == "record":
        return await _handle_record_result(update, context, result, text, msg_id)

    if intent == "query":
        return await _handle_query(update, result)

    rate_q = parse_rate_question(text)
    if rate_q:
        from app.exchange import CURRENCY_NAMES, convert

        rate_result = await asyncio.to_thread(convert, "1", rate_q["from"], rate_q["to"])
        if "error" in rate_result:
            await update.message.reply_text(rate_result["error"])
        else:
            from_name = CURRENCY_NAMES.get(rate_result["from"], rate_result["from"])
            to_name = CURRENCY_NAMES.get(rate_result["to"], rate_result["to"])
            await update.message.reply_text(
                f"💱 سعر الصرف اليوم:\n"
                f"1 {from_name} = {rate_result['rate']} {to_name}"
            )
        return None

    if intent == "chat":
        chat_reply = (result or {}).get("reply") or (
            "أهلًا! كيف أقدر أساعدك؟ سجّل عملية أو اسأل عن ميزانيتك."
            if is_greeting(text)
            else "أعتذر، ما فهمت رسالتك. سجّل عملية أو اسأل عن ميزانيتك."
        )
        await update.message.reply_text(chat_reply, reply_markup=QUICK_ACTIONS)
    else:
        await update.message.reply_text(
            "أعتذر، ما فهمت رسالتك. جرّب:\n"
            "• تسجيل عملية: \"دفعت 300 شيكل لمحمد\"\n"
            "• سؤال: \"كم لي عند محمد؟\"\n"
            "• سعر صرف: \"كم صرف شيكل على دينار\""
        )
    return None


# ---------- التأكيد (أزرار InlineKeyboard) ----------


async def _confirm_resolve(
    update: Update, context: ContextTypes.DEFAULT_TYPE, accept: bool
) -> None:
    query = update.callback_query
    await query.answer()

    from bot.menus import MAIN_HOME_KEYBOARD

    if not accept:
        _record_feedback(context, source="reject_confirm", telegram_user_id=query.from_user.id)
        await query.edit_message_text("تم الإلغاء. لم يُحفظ شيء.", reply_markup=MAIN_HOME_KEYBOARD)
    else:
        result = context.user_data.get("confirm_result")
        raw = context.user_data.get("confirm_raw")
        msg_id = context.user_data.get("confirm_message_id")
        telegram_user_id = query.from_user.id
        data_type = (result or {}).get("type")

        if not data_type or data_type == "unknown" or data_type not in TYPE_NAMES:
            await query.edit_message_text(
                "لم أفهم نوع العملية بوضوح، لم يُحفظ شيء.", reply_markup=MAIN_HOME_KEYBOARD
            )
        else:
            def _save():
                db = SessionLocal()
                try:
                    return save_record(db, telegram_user_id, result, raw, msg_id)
                finally:
                    db.close()

            reply = await asyncio.to_thread(_save)
            await query.edit_message_text(
                reply or "أعتذر، لم أستطع حفظ هذه العملية.", reply_markup=MAIN_HOME_KEYBOARD
            )

    _clear_all_pending(context)
    return None


async def confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    return await _confirm_resolve(update, context, accept=True)


async def confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    return await _confirm_resolve(update, context, accept=False)


async def confirm_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """زر «تكرار العملية» — يحفظ الحالي ثم يبدأ عملية جديدة بنفس التفاصيل.

    يبقي النوع/العملة/الشخص/التصنيف من السجل المحفوظ ويسأل عن المبلغ فقط،
    مفيد للمصاريف المتكررة (إيجار، اشتراكات).
    """
    query = update.callback_query
    await query.answer()

    from bot.menus import MAIN_HOME_KEYBOARD

    result = context.user_data.get("confirm_result") or {}
    raw = context.user_data.get("confirm_raw")
    msg_id = context.user_data.get("confirm_message_id")
    telegram_user_id = query.from_user.id
    data_type = result.get("type")

    if not data_type or data_type == "unknown" or data_type not in TYPE_NAMES:
        await query.edit_message_text(
            "لم أفهم نوع العملية بوضوح، لم يُحفظ شيء.", reply_markup=MAIN_HOME_KEYBOARD
        )
        _clear_all_pending(context)
        return None

    def _save():
        db = SessionLocal()
        try:
            return save_record(db, telegram_user_id, result, raw, msg_id)
        finally:
            db.close()

    reply = await asyncio.to_thread(_save)
    if not reply:
        await query.edit_message_text(
            "أعتذر، لم أستطع حفظ هذه العملية.", reply_markup=MAIN_HOME_KEYBOARD
        )
        _clear_all_pending(context)
        return None

    # نسخة مكررة بنفس النوع والعملة والشخص والتفاصيل، لكن بلا مبلغ
    repeat = {k: v for k, v in result.items() if k in ("type", "currency", "person", "category", "description")}
    repeat["intent"] = "record"

    context.user_data["pending_record"] = repeat
    context.user_data["pending_type"] = data_type
    context.user_data["pending_missing"] = ["amount"]
    context.user_data["pending_raw"] = None
    context.user_data["pending_message_id"] = msg_id

    prompt = (
        f"{SUCCESS} حُفظت العملية.\n"
        "🔁 سأكرّر نفس التفاصيل — أرسل المبلغ فقط (مثال: 300 أو 300 شيكل)"
    )
    await query.edit_message_text(
        prompt, reply_markup=MAIN_HOME_KEYBOARD
    )
    return COLLECT


async def confirm_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """زر تعديل حقل في شاشة التأكيد → يعرض قائمة الحقول القابلة للتعديل."""
    query = update.callback_query
    await query.answer()
    result = context.user_data.get("confirm_result") or {}
    fields = _confirm_editable_fields(result)
    buttons = [
        [InlineKeyboardButton(FIELD_LABELS.get(f, f), callback_data=f"confirm:edit:{f}")]
        for f in fields
    ]
    buttons.append([InlineKeyboardButton(f"{BACK} رجوع للتأكيد", callback_data="confirm:edit:back")])
    await query.edit_message_text(
        "اختر الحقل الذي تريد تعديله:", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CONFIRM


async def _reshow_confirm(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> int:
    result = context.user_data.get("confirm_result") or {}
    await query.edit_message_text(
        _build_confirm_text(result), reply_markup=_build_confirm_keyboard()
    )
    return CONFIRM


async def confirm_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """اختيار حقل من شاشة تعديل التأكيد → يطلب القيمة الجديدة."""
    query = update.callback_query
    await query.answer()
    field = query.data.replace("confirm:edit:", "").strip()
    if field == "back":
        return await _reshow_confirm(query, context)
    result = context.user_data.get("confirm_result") or {}
    if field not in _confirm_editable_fields(result):
        return await _reshow_confirm(query, context)
    context.user_data["confirm_editing_field"] = field
    preview = _confirm_value_preview(result, field)
    await query.edit_message_text(
        f"أرسل القيمة الجديدة لـ **{FIELD_LABELS.get(field, field)}**:\n"
        f"القيمة الحالية: {preview}\n\n(أرسل /cancel للإلغاء)",
        parse_mode="Markdown",
    )
    return CONFIRM


async def edit_confirm_field_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """قيمة حقل التعديل في شاشة التأكيد (رسالة نصية أثناء CONFIRM)."""
    result = context.user_data.get("confirm_result") or {}
    field = context.user_data.pop("confirm_editing_field", None)
    if not field:
        return await text_router(update, context)
    new_result, err = apply_confirm_edit(result, field, update.message.text)
    if err:
        await update.message.reply_text(f"{err}\nأعد المحاولة أو أرسل /cancel.")
        return CONFIRM
    context.user_data["confirm_result"] = new_result
    await update.message.reply_text(
        _build_confirm_text(new_result), reply_markup=_build_confirm_keyboard()
    )
    return CONFIRM


async def task_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """تعديل وصف مهمة عبر زر القائمة (رسالة نصية خارج ConversationHandler)."""
    task_id = context.user_data.pop("pending_task_edit_id", None)
    if not task_id:
        return await fresh_entry(update, context)
    new_desc = (update.message.text or "").strip()
    if not new_desc:
        await update.message.reply_text("الوصف الجديد لا يمكن أن يكون فارغًا. أرسل قيمة أو /cancel.")
        return None
    uid = update.effective_user.id
    db = SessionLocal()
    try:
        from app.database.crud import can_manage_records

        if not can_manage_records(db, uid):
            await update.message.reply_text(
                "أعضاء المساحة المشتركة لا يعدّلون المهام — المرتكز (المالك) فقط."
            )
            return None
        row, _ = get_record_by_id(db, uid, "Task", task_id)
        if row is None:
            await update.message.reply_text("لم أجد المهمة (ربما حُذفت).")
            return None
        update_task(db, row, {"description": new_desc})
        await update.message.reply_text(f"تم تحديث الوصف:\n{new_desc}")
    finally:
        db.close()
    try:
        from bot.menus import send_main_menu

        await send_main_menu(update.message, context)
    except Exception:
        logger.exception("فشل عرض القائمة الرئيسية بعد تعديل المهمة")
    return None


def _normalize_edit_value(field: str, raw: str) -> tuple[object, str | None]:
    """يجعل قيمة إدخال الحقل صالحة للنوع حسب الحقل (عودة: قيمة, خطأ|None)."""
    value = (raw or "").strip()
    if field == "amount":
        m = _AMOUNT_RE.match(value)
        if not m:
            return None, "لم أتعرف على رقم صالح. مثال: 300 أو 300.5"
        num = m.group("num").replace(",", ".")
        try:
            return Decimal(num), None
        except Exception:
            return None, "المبلغ غير صالح."
    if field == "currency":
        norm = normalize_currency(value)
        if not norm:
            return None, "عملة غير معروفة. جرّب: شيكل / دولار / دينار / يورو."
        return norm, None
    if field == "priority":
        return normalize_priority(value), None
    if field in ("due_date", "date"):
        from app.database.crud import parse_date_local

        if parse_date_local(value) is None:
            return None, "تاريخ غير صالح. مثال: 2026-09-10 10:00"
        return value, None
    if not value:
        return None, "القيمة لا يمكن أن تكون فارغة."
    return value, None


async def record_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """تعديل حقل آخر سجل عبر زر القائمة (رسالة نصية خارج ConversationHandler)."""
    record_id = context.user_data.pop("pending_record_edit_id", None)
    model_name = context.user_data.pop("pending_record_edit_model", None)
    field = context.user_data.pop("pending_record_edit_field", None)
    if not record_id or not model_name or not field:
        return await fresh_entry(update, context)

    value, error = _normalize_edit_value(field, update.message.text)
    from bot.menus import MAIN_HOME_KEYBOARD

    if error:
        await update.message.reply_text(error, reply_markup=MAIN_HOME_KEYBOARD)
        return None

    uid = update.effective_user.id
    db = SessionLocal()
    try:
        from app.database.crud import can_manage_records

        if not can_manage_records(db, uid):
            await update.message.reply_text(
                "أعضاء المساحة المشتركة لا يعدّلون السجلات — المرتكز (المالك) فقط.",
                reply_markup=MAIN_HOME_KEYBOARD,
            )
            return None
        row, _ = get_record_by_id(db, uid, model_name, record_id)
        if row is None:
            await update.message.reply_text("لم أجد السجل (ربما حُذف).")
            return None
        fields = {field: value}
        if model_name == "Transaction":
            update_transaction(db, row, fields)
        elif model_name == "Task":
            update_task(db, row, fields)
        else:
            update_note(db, row, fields)
    finally:
        db.close()

    from bot.editing import FIELD_LABELS_AR

    label = FIELD_LABELS_AR.get(field, field)
    await update.message.reply_text(f"تم تحديث {label} بنجاح {SUCCESS}", reply_markup=MAIN_HOME_KEYBOARD)
    return None


async def search_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بحث في السجلات (رسالة نصية بعد زر "🔍 بحث"): يرد بنتائج صفحات بأزرار."""
    term = (update.message.text or "").strip()
    context.user_data.pop("pending_search", None)
    if not term:
        from bot.menus import MAIN_HOME_KEYBOARD

        await update.message.reply_text(
            "أرسل كلمة البحث (شخص، تصنيف، وصف، قيمة أو نوع) أو /cancel.",
            reply_markup=MAIN_HOME_KEYBOARD,
        )
        return None

    uid = update.effective_user.id
    db = SessionLocal()
    try:
        from app.database.crud import search_records

        recs, meta = search_records(db, uid, term, limit=50, return_meta=True)
    finally:
        db.close()

    context.user_data["pending_search_term"] = term
    from bot.menus import _records_page_payload, _search_partial_hint

    hint = _search_partial_hint(meta)
    text, markup = _records_page_payload(recs, term, 1, "sr", search_hint=hint)
    await update.message.reply_text(text, reply_markup=markup)
    return None


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _record_feedback(context, source="cancel", telegram_user_id=update.effective_user.id)
    _clear_all_pending(context)
    from bot.menus import MAIN_HOME_KEYBOARD

    await update.message.reply_text(
        "أُلغيت العملية. يمكنك إرسال عملية جديدة متى شئت.",
        reply_markup=MAIN_HOME_KEYBOARD,
    )
    return None


# ---------- تعريف المحادثة ----------

TEXT_FILTER = filters.TEXT & ~filters.COMMAND

conversation_handler = ConversationHandler(
    entry_points=[
        MessageHandler(TEXT_FILTER, text_router),
        MessageHandler(MEDIA_FILTER, media_router),
    ],
    states={
        COLLECT: [
            # عند الجمع نجيب على ردّ المستخدم (حالة المحادثة تُفحص قبل الـ entry)
            MessageHandler(TEXT_FILTER, collect_reply),
            MessageHandler(MEDIA_FILTER, media_router),
        ],
        CONFIRM: [
            CallbackQueryHandler(confirm_yes, pattern="^confirm:yes$"),
            CallbackQueryHandler(confirm_no, pattern="^confirm:no$"),
            CallbackQueryHandler(confirm_repeat, pattern="^confirm:repeat$"),
            CallbackQueryHandler(confirm_edit, pattern="^confirm:edit$"),
            CallbackQueryHandler(confirm_edit_field, pattern=r"^confirm:edit:.+$"),
            # رسالة جديدة خلال التأكيد → إعادة بدء المحادثة (allow_reentry) بمسح الحالة
            MessageHandler(TEXT_FILTER, text_router),
            MessageHandler(MEDIA_FILTER, media_router),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
    ],
    allow_reentry=True,
    name="record_flow",
)
