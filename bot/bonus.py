"""
أمر /bonus وقائمة البونس والمكافآت.

يقدّم واجهة موحّدة لميزة «حدث البون»:
  1) بونس (معاملات بتصنيف بونس) — منح/استلام + تقرير مخصص
  2) فعاليات ترويجية — إنشاء/عرض/إنهاء
  3) مكافآت موظفين — إضافة/عرض/تعطيل خطة + سقف شهري
  4) نقاط ولاء — تفعيل/تعطيل/إضافة نقاط/استبدال + عرض الحسابات

مع تذكيرات تلقائية بخطط الموظفين المستحقة ونهاية الفعاليات.
"""

import asyncio
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.database.db import SessionLocal
from bot.icons import ERROR, EXPENSE, INCOME, SUCCESS, WARNING

logger = logging.getLogger(__name__)

COMMANDS_HELP = (
    "🎁 البونس والمكافآت:\n"
    "/bonus — ملخص البونس والنقاط والفعاليات والخطط\n"
    "/bonus report [فترة] — تقرير بونس مفصّل\n"
    "/bonus report person اسم — تقرير بونس لشخص محدد\n"
    "/bonus add expense 500 [ILS] [محمد] [وصف] — منح بونس\n"
    "/bonus add income 200 [ILS] [أحمد] [استلام بونس]\n"
    "/bonus event إضافة اسم الفعالية [ميزانية] [عملة] — إنشاء فعالية\n"
    "/bonus event قائمة — عرض الفعاليات\n"
    "/bonus event إنهاء <رقم> — إنهاء فعالية\n"
    "/bonus plan إضافة محمد 1000 [monthly] [2000] — خطة مكافأة\n"
    "/bonus plan قائمة — عرض الخطط\n"
    "/bonus plan تعطيل <رقم> — تعطيل خطة\n"
    "/bonus points تفعيل [1] [0.01] [0] — فعّل نقاط الولاء\n"
    "/bonus points تعطيل — تعطيل نقاط الولاء\n"
    "/bonus points منح أحمد 50 — إضافة نقاط يدوي\n"
    "/bonus points استبدال أحمد 100 — استبدال نقاط خصمًا\n"
    "/bonus points حسابات — عرض حسابات الولاء"
)

PERIOD_MAP = {
    "today": "today",
    "day": "today",
    "اليوم": "today",
    "week": "this_week",
    "weekly": "this_week",
    "this_week": "this_week",
    "الأسبوع": "this_week",
    "month": "this_month",
    "monthly": "this_month",
    "this_month": "this_month",
    "الشهر": "this_month",
    "year": "this_year",
    "السنة": "this_year",
    "all": "all_time",
    "all_time": "all_time",
    "الكل": "all_time",
}

PERIOD_LABELS = {
    "today": "اليوم",
    "this_week": "هذا الأسبوع",
    "this_month": "هذا الشهر",
    "this_year": "هذه السنة",
    "all_time": "كل الفترات",
}

EVENT_STATUS_LABELS = {"planned": "مجدولة", "active": "جارية", "ended": "منتهية"}

PLAN_FREQ_LABELS = {"monthly": "شهري", "quarterly": "ربع سنوي", "one_off": "مرة واحدة"}


# ---------- أدوات مساعدة ----------


def _fmt_amount(amount, currency: str | None = None) -> str:
    if amount is None:
        return "—"
    val = float(amount)
    cur = f" {currency}" if currency else ""
    return f"{val:,.2f}{cur}"


def _known_currency(val: str) -> str | None:
    """يعيد رمز العملة إن كان الوسيط عملة معروفة، وإلا None.

    يعتمد على قوائم رموز/أسماء العملات (CURRENCY_ALIASES + CURRENCY_NAMES)
    بدل المقارنات الشكلية (isalpha() و len<=5) التي كانت تخلط أسماء الأشخاص
    العربية القصيرة (علي، خالد، سامر...) بالعملات وتخزّن عملات غير صالحة.
    """
    from app.database.crud import normalize_currency
    from app.exchange import CURRENCY_NAMES

    norm = normalize_currency(val.strip()).upper()
    if norm in CURRENCY_NAMES:
        return norm
    return None


def _parse_optional_args(args: list[str], defaults: dict | None = None) -> dict:
    """يحلّل وسيطات اختيارية (عملة/شخص/وصف/أرقام) بشكل ذكي."""
    defaults = defaults or {}
    currency = defaults.get("currency")
    person = defaults.get("person")
    desc = defaults.get("desc")
    for token in args:
        val = token.strip()
        if not val:
            continue
        cur = _known_currency(val)
        if cur and currency is None:
            currency = cur
        elif person is None:
            person = val
        else:
            desc = val
    return {"currency": currency, "person": person, "desc": desc}


def _db_call(fn):
    """ينفّذ fn على جلسة DB مُغلقة آليًا."""
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


# ---------- تنسيق النصوص ----------


def format_bonus_overview(data: dict) -> str:
    lines = ["🎁 ملخص البونس — هذا الشهر:\n"]
    report = data["report"]
    total_exp = sum(report["expense"].values()) if report["expense"] else Decimal("0")
    total_inc = sum(report["income"].values()) if report["income"] else Decimal("0")
    if total_exp or total_inc:
        lines.append(f"{EXPENSE} منح: {_fmt_amount(total_exp)}")
        lines.append(f"{INCOME} استلام: {_fmt_amount(total_inc)}")
        lines.append(f"🔢 عدد العمليات: {report['count_expense'] + report['count_income']}")
    else:
        lines.append("لا توجد معاملات بونس هذا الشهر بعد.")
    lines.append(f"\n🎯 فعالية ترويجية نشطة: {data['events_count']}")
    if data["plans"]:
        lines.append(f"\n👥 خطط مكافآت: {data['plans_count']}")
        if data["due_plans_count"]:
            lines.append(f"   ⚠️ مستحقة الآن: {data['due_plans_count']}")
        for p in data["plans"][:5]:
            freq = PLAN_FREQ_LABELS.get(p["frequency"], p["frequency"])
            lines.append(f"   • {p['person']}: {_fmt_amount(p['amount'], p['currency'])} ({freq})")
            if p["next_due_at"]:
                lines.append(f"     ⏰ الموعد: {p['next_due_at'].strftime('%Y-%m-%d')}")
            if p["monthly_cap"]:
                cap_status = {"over": f"{WARNING} تجاوز", "near": f"{WARNING} قريب", "ok": SUCCESS}.get(p["cap_status"], "—")
                lines.append(f"     📊 الشهر: {_fmt_amount(p['spent_this_month'])} / {_fmt_amount(p['monthly_cap'])} {cap_status}")
    else:
        lines.append("\n👥 لا توجد خطط مكافآت بعد.")
    if data["loyalty_enabled"]:
        lines.append("\n⭐ نقاط الولاء:")
        cfg = data["loyalty"]
        lines.append(f"   المعدّل: {float(cfg.points_rate):g} نقطة/وحدة — قيمة النقطة: {_fmt_amount(cfg.points_value)}")
        if data["total_points"]:
            lines.append(f"   🪙 إجمالي النقاط (أعلى 5): {data['total_points']}")
        for a in data.get("loyalty_accounts", [])[:5]:
            lines.append(f"   • {a.person}: {a.points_balance} نقطة")
    else:
        lines.append("\n⭐ نقاط الولاء: غير مفعّلة")
    lines.append("\n💡 للتفصيل: /bonus report")
    return "\n".join(lines)


def format_bonus_report(report: dict, person: str | None = None) -> str:
    period_label = PERIOD_LABELS.get(report["period"], report["period"])
    title = f"📊 تقرير البونس — {period_label}"
    if person:
        title += f" — {person}"
    lines = [title + "\n"]
    if report["expense"]:
        lines.append(f"{EXPENSE} مصروفات بونس:")
        for cur, total in report["expense"].items():
            lines.append(f"   {cur}: {_fmt_amount(total, cur)}")
        lines.append(f"   الإجمالي: {_fmt_amount(sum(report['expense'].values()))}")
    if report["income"]:
        lines.append(f"\n{INCOME} إيرادات بونس:")
        for cur, total in report["income"].items():
            lines.append(f"   {cur}: {_fmt_amount(total, cur)}")
        lines.append(f"   الإجمالي: {_fmt_amount(sum(report['income'].values()))}")
    if not report["expense"] and not report["income"]:
        lines.append("لا توجد معاملات بونس في هذه الفترة.")
    if report["rows"]:
        lines.append(f"\n📝 آخر {min(len(report['rows']), 10)} معاملات:")
        for tx in report["rows"][:10]:
            sign = EXPENSE if tx.type == "expense" else INCOME
            person_txt = f" — {tx.person}" if tx.person else ""
            desc = tx.description or ""
            lines.append(f"  {sign} {_fmt_amount(tx.amount, tx.currency)}{person_txt}{' — ' + desc[:40] if desc else ''}")
    return "\n".join(lines)


def format_events_list(events: list) -> str:
    if not events:
        return "لا توجد فعاليات ترويجية بعد."
    lines = ["🎯 فعاليات البونس:\n"]
    for ev in events:
        status = EVENT_STATUS_LABELS.get(ev.status, ev.status)
        budget = _fmt_amount(ev.budget, ev.currency) if ev.budget else "—"
        dates = ""
        if ev.start_at:
            dates = f" من {ev.start_at.strftime('%Y-%m-%d')}"
            if ev.end_at:
                dates += f" إلى {ev.end_at.strftime('%Y-%m-%d')}"
        lines.append(f"#{ev.id} {ev.name} [{status}]")
        lines.append(f"   {EXPENSE} الميزانية: {budget}{dates}")
        if ev.note:
            lines.append(f"   📝 {ev.note[:60]}")
    return "\n".join(lines)


def format_plans_list(plans_data: list) -> str:
    if not plans_data:
        return "لا توجد خطط مكافآت بعد."
    lines = ["👥 خطط مكافآت الموظفين:\n"]
    for p in plans_data:
        freq = PLAN_FREQ_LABELS.get(p["frequency"], p["frequency"])
        lines.append(f"#{p['id']} {p['person']}: {_fmt_amount(p['amount'], p['currency'])} ({freq})")
        if p["next_due_at"]:
            lines.append(f"   ⏰ الموعد: {p['next_due_at'].strftime('%Y-%m-%d')}")
        if p["monthly_cap"]:
            cap_status = {"over": f"{WARNING} تجاوز", "near": f"{WARNING} قريب", "ok": ""}.get(p["cap_status"], "")
            lines.append(f"   📊 الشهر: {_fmt_amount(p['spent_this_month'])} / {_fmt_amount(p['monthly_cap'])} {cap_status}")
    return "\n".join(lines)


def format_loyalty_accounts(accounts: list, config) -> str:
    if not accounts:
        return "لا توجد حسابات ولاء بعد."
    lines = ["⭐ حسابات نقاط الولاء:\n"]
    lines.append(f"المعدّل: {float(config.points_rate):g} نقطة/وحدة — قيمة النقطة: {_fmt_amount(config.points_value)}")
    lines.append(f"الحد الأدنى للاستبدال: {config.min_redeem_points} نقطة\n")
    for a in accounts:
        lines.append(f"• {a.person}: {a.points_balance} نقطة (مكتسب: {a.total_earned} — مستهلَك: {a.total_redeemed})")
    return "\n".join(lines)


# ---------- معالج /bonus ----------


async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = update.effective_user.id
    args = context.args or []
    if not args:
        await _cmd_overview(update, telegram_user_id)
        return
    action = args[0].strip().lower()
    if action in ("report", "تقرير"):
        await _cmd_report(update, telegram_user_id, args[1:])
    elif action in ("add", "إضافة", "منح", "تسجيل"):
        await _cmd_add(update, telegram_user_id, args[1:])
    elif action in ("event", "فعالية"):
        await _cmd_event(update, telegram_user_id, args[1:])
    elif action in ("plan", "خطة"):
        await _cmd_plan(update, telegram_user_id, args[1:])
    elif action in ("points", "نقاط"):
        await _cmd_points(update, telegram_user_id, args[1:])
    else:
        await update.message.reply_text(f"لم أفهم الأمر. الاستخدام:\n\n{COMMANDS_HELP}")


async def _cmd_overview(update: Update, uid: int) -> None:
    from app.database.crud import bonus_overview

    data = await asyncio.to_thread(lambda: _db_call(lambda db: bonus_overview(db, uid)))
    await update.message.reply_text(format_bonus_overview(data))


async def _cmd_report(update: Update, uid: int, args: list[str]) -> None:
    period = "this_month"
    person = None
    i = 0
    while i < len(args):
        arg = args[i].strip().lower()
        if arg in ("person", "شخص", "الشخص") and i + 1 < len(args):
            person = args[i + 1].strip()
            i += 2
            continue
        mapped = PERIOD_MAP.get(arg)
        if mapped:
            period = mapped
            i += 1
            continue
        if not person and args[i].strip():
            if i + 1 < len(args) and PERIOD_MAP.get(args[i + 1].strip().lower()):
                person = args[i].strip()
                i += 2
                continue
        i += 1

    from app.database.crud import bonus_report_summary
    report = await asyncio.to_thread(lambda: _db_call(lambda db: bonus_report_summary(db, uid, period, person)))
    await update.message.reply_text(format_bonus_report(report, person))


async def _cmd_add(update: Update, uid: int, args: list[str]) -> None:
    if len(args) < 2:
        await update.message.reply_text(
            "الاستخدام:\n"
            "/bonus add expense 500 [ILS] [محمد] [وصف]\n"
            "/bonus add income 200 [ILS] [أحمد] [استلام بونس]"
        )
        return
    direction = args[0].strip().lower()
    if direction not in ("expense", "income", "مصروف", "إيراد"):
        await update.message.reply_text("النوع يجب أن يكون expense أو income (مصروف/إيراد).")
        return
    if direction == "مصروف":
        direction = "expense"
    elif direction == "إيراد":
        direction = "income"
    try:
        amount = Decimal(args[1])
    except InvalidOperation:
        await update.message.reply_text(f"المبلغ غير صالح: {args[1]}")
        return
    parsed = _parse_optional_args(args[2:])
    from app.database.crud import record_bonus_grant
    tx = await asyncio.to_thread(lambda: _db_call(lambda db: record_bonus_grant(
        db, uid, amount, parsed["currency"], direction=direction,
        person=parsed["person"], description=parsed["desc"],
    )))
    if tx:
        await update.message.reply_text(
            f"{SUCCESS} تم تسجيل البونس:\n"
            f"   {f'{EXPENSE} مصروف' if direction == 'expense' else f'{INCOME} إيراد'}: {_fmt_amount(tx.amount, tx.currency)}"
            f"{f' — {tx.person}' if tx.person else ''}"
        )
    else:
        await update.message.reply_text("تعذّر تسجيل البونس. تحقق من المدخلات.")


async def _cmd_event(update: Update, uid: int, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            "استخدام:\n"
            "/bonus event إضافة اسم الميزانية [ميزانية] [عملة]\n"
            "/bonus event قائمة\n"
            "/bonus event إنهاء <رقم>"
        )
        return
    sub = args[0].strip().lower()

    if sub in ("list", "قائمة", "عرض"):
        from app.database.crud import list_bonus_events
        events = await asyncio.to_thread(lambda: _db_call(lambda db: list_bonus_events(db, uid)))
        await update.message.reply_text(format_events_list(events))
        return

    if sub in ("end", "إنهاء", "نهاية"):
        if len(args) < 2 or not args[1].strip().isdigit():
            await update.message.reply_text("استخدم: /bonus event إنهاء <رقم الفعالية>")
            return
        event_id = int(args[1])
        from app.database.crud import end_bonus_event
        ev = await asyncio.to_thread(lambda: _db_call(lambda db: end_bonus_event(db, uid, event_id)))
        if ev:
            await update.message.reply_text(f"{SUCCESS} تم إنهاء الفعالية: {ev.name}")
        else:
            await update.message.reply_text("لم أجد هذه الفعالية.")
        return

    if sub in ("add", "إضافة", "جديد"):
        if len(args) < 2:
            await update.message.reply_text("استخدم: /bonus event إضافة اسم الفعالية [ميزانية] [عملة]")
            return
        name = args[1].strip()
        budget = None
        currency = None
        for raw in args[2:]:
            val = raw.strip()
            if not val:
                continue
            if budget is None:
                try:
                    budget = Decimal(val)
                    continue
                except InvalidOperation:
                    pass
            cur = _known_currency(val)
            if cur:
                currency = cur

        from app.database.crud import create_bonus_event
        ev = await asyncio.to_thread(lambda: _db_call(lambda db: create_bonus_event(db, uid, name, budget=budget, currency=currency)))
        if ev:
            budget_txt = _fmt_amount(ev.budget, ev.currency) if ev.budget else "—"
            await update.message.reply_text(
                f"{SUCCESS} أُنشئت الفعالية: {ev.name}\n"
                f"   {EXPENSE} الميزانية: {budget_txt}\n"
                f"   📅 الحالة: {EVENT_STATUS_LABELS.get(ev.status, ev.status)}"
            )
        else:
            await update.message.reply_text("تعذّر إنشاء الفعالية. تحقق من الاسم والمبلغ.")
        return

    await update.message.reply_text("لم أفهم. الاستخدام:\n/bonus event إضافة|قائمة|إنهاء ...")


async def _cmd_plan(update: Update, uid: int, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            "استخدام:\n"
            "/bonus plan إضافة <الشخص> <المبلغ> [monthly|quarterly|one_off] [سقف شهري]\n"
            "/bonus plan قائمة\n"
            "/bonus plan تعطيل <رقم>"
        )
        return
    sub = args[0].strip().lower()

    if sub in ("list", "قائمة", "عرض"):
        from app.database.crud import employee_bonus_overview
        plans = await asyncio.to_thread(lambda: _db_call(lambda db: employee_bonus_overview(db, uid)["plans"]))
        await update.message.reply_text(format_plans_list(plans))
        return

    if sub in ("disable", "تعطيل", "حذف"):
        if len(args) < 2 or not args[1].strip().isdigit():
            await update.message.reply_text("استخدم: /bonus plan تعطيل <رقم الخطة>")
            return
        plan_id = int(args[1])
        from app.database.crud import disable_employee_bonus_plan
        plan = await asyncio.to_thread(lambda: _db_call(lambda db: disable_employee_bonus_plan(db, uid, plan_id)))
        if plan:
            await update.message.reply_text(f"{SUCCESS} تعطّلت خطة مكافأة {plan.person}.")
        else:
            await update.message.reply_text("لم أجد هذه الخطة.")
        return

    if sub in ("add", "إضافة", "جديد"):
        if len(args) < 3:
            await update.message.reply_text("استخدم: /bonus plan إضافة <الشخص> <المبلغ> [monthly|quarterly|one_off] [سقف شهري]")
            return
        person = args[1].strip()
        try:
            amount = Decimal(args[2])
        except InvalidOperation:
            await update.message.reply_text(f"المبلغ غير صالح: {args[2]}")
            return
        freq = "monthly"
        if len(args) > 3:
            raw = args[3].strip().lower()
            freq_map = {"monthly": "monthly", "شهري": "monthly", "quarterly": "quarterly", "ربع سنوي": "quarterly", "one_off": "one_off", "مرة واحدة": "one_off", "مرة": "one_off"}
            freq = freq_map.get(raw, "monthly")
        cap = None
        if len(args) > 4:
            try:
                cap = Decimal(args[4])
            except InvalidOperation:
                pass
        from app.database.crud import create_employee_bonus_plan
        plan = await asyncio.to_thread(lambda: _db_call(lambda db: create_employee_bonus_plan(db, uid, person, amount, frequency=freq, monthly_cap=cap)))
        if plan:
            freq_label = PLAN_FREQ_LABELS.get(plan.frequency, plan.frequency)
            cap_txt = _fmt_amount(plan.monthly_cap, plan.currency) if plan.monthly_cap else "—"
            await update.message.reply_text(
                f"{SUCCESS} أُنشئت خطة مكافأة:\n"
                f"   👤 الشخص: {plan.person}\n"
                f"   {EXPENSE} المبلغ: {_fmt_amount(plan.amount, plan.currency)}\n"
                f"   🔄 الدورية: {freq_label}\n"
                f"   📊 السقف الشهري: {cap_txt}"
            )
        else:
            await update.message.reply_text("تعذّر إنشاء الخطة. تحقق من المدخلات.")
        return

    await update.message.reply_text("لم أفهم. الاستخدام:\n/bonus plan إضافة|قائمة|تعطيل ...")


async def _cmd_points(update: Update, uid: int, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            "استخدام:\n"
            "/bonus points تفعيل [معدّل النقاط] [قيمة النقطة] [حد الاستبدال]\n"
            "/bonus points تعطيل\n"
            "/bonus points منح <الشخص> <عدد النقاط>\n"
            "/bonus points استبدال <الشخص> <عدد النقاط>\n"
            "/bonus points حسابات"
        )
        return
    sub = args[0].strip().lower()

    if sub in ("enable", "تفعيل", "تشغيل"):
        points_rate = float(args[1]) if len(args) > 1 else 1.0
        points_value = float(args[2]) if len(args) > 2 else 0.01
        min_redeem = int(args[3]) if len(args) > 3 else 0
        from app.database.crud import loyalty_config_enable
        cfg = await asyncio.to_thread(lambda: _db_call(lambda db: loyalty_config_enable(db, uid, points_rate, points_value, min_redeem)))
        await update.message.reply_text(
            f"{SUCCESS} فُعّلت نقاط الولاء:\n"
            f"   المعدّل: {float(cfg.points_rate):g} نقطة/وحدة\n"
            f"   قيمة النقطة: {_fmt_amount(cfg.points_value)}\n"
            f"   الحد الأدنى للاستبدال: {cfg.min_redeem_points} نقطة"
        )
        return

    if sub in ("disable", "تعطيل", "إيقاف"):
        from app.database.crud import disable_loyalty
        ok = await asyncio.to_thread(lambda: _db_call(lambda db: disable_loyalty(db, uid)))
        await update.message.reply_text(f"{SUCCESS} تعطّلت نقاط الولاء." if ok else "نقاط الولاء غير مفعّلة أصلاً.")
        return

    if sub in ("add", "منح", "إضافة"):
        if len(args) < 3:
            await update.message.reply_text("استخدم: /bonus points منح <الشخص> <عدد النقاط>")
            return
        person = args[1].strip()
        try:
            points = int(args[2])
        except ValueError:
            await update.message.reply_text(f"عدد النقاط غير صالح: {args[2]}")
            return
        from app.database.crud import loyalty_add_points
        account = await asyncio.to_thread(lambda: _db_call(lambda db: loyalty_add_points(db, uid, person, points)))
        await update.message.reply_text(f"{SUCCESS} أُضيف {points} نقطة لـ {account.person}.\n   الرصيد: {account.points_balance} نقطة")
        return

    if sub in ("redeem", "استبدال", "خصم"):
        if len(args) < 3:
            await update.message.reply_text("استخدم: /bonus points استبدال <الشخص> <عدد النقاط>")
            return
        person = args[1].strip()
        try:
            points = int(args[2])
        except ValueError:
            await update.message.reply_text(f"عدد النقاط غير صالح: {args[2]}")
            return
        from app.database.crud import loyalty_redeem_points
        result = await asyncio.to_thread(lambda: _db_call(lambda db: loyalty_redeem_points(db, uid, person, points)))
        if result["ok"]:
            await update.message.reply_text(
                f"{SUCCESS} تم استبدال {result['points']} نقطة من {result['person']}.\n"
                f"   {INCOME} قيمة الخصم: {_fmt_amount(result['value'])}\n"
                f"   🪙 الرصيد المتبقي: {result['balance']} نقطة"
            )
        else:
            await update.message.reply_text(f"{ERROR} {result.get('error', 'حدث خطأ.')}")
        return

    if sub in ("list", "قائمة", "حسابات", "عرض"):
        from app.database.crud import list_loyalty_accounts, loyalty_config_get
        accounts, cfg = await asyncio.to_thread(lambda: _db_call(lambda db: (list_loyalty_accounts(db, uid), loyalty_config_get(db, uid))))
        if cfg is None:
            await update.message.reply_text("نقاط الولاء غير مفعّلة. استخدم: /bonus points تفعيل")
        else:
            await update.message.reply_text(format_loyalty_accounts(accounts, cfg))
        return

    await update.message.reply_text("لم أفهم. الاستخدام:\n/bonus points تفعيل|تعطيل|منح|استبدال|حسابات ...")


# ---------- أزرار القائمة ----------


async def _handle_bonus_menu(query, context, parts: list[str]) -> None:
    uid = query.from_user.id
    act = parts[0] if parts else "overview"

    if act == "overview":
        from app.database.crud import bonus_overview
        data = await asyncio.to_thread(lambda: _db_call(lambda db: bonus_overview(db, uid)))
        from bot.menus import _home_keyboard
        await query.edit_message_text(format_bonus_overview(data), reply_markup=_home_keyboard([("📊 تقرير مفصل", "bn:report")]))
        return

    if act == "report":
        from app.database.crud import bonus_report_summary
        report = await asyncio.to_thread(lambda: _db_call(lambda db: bonus_report_summary(db, uid, "this_month")))
        from bot.menus import _home_keyboard
        await query.edit_message_text(format_bonus_report(report), reply_markup=_home_keyboard())
        return

    if act == "events":
        from app.database.crud import list_bonus_events
        events = await asyncio.to_thread(lambda: _db_call(lambda db: list_bonus_events(db, uid)))
        from bot.menus import _home_keyboard
        await query.edit_message_text(format_events_list(events), reply_markup=_home_keyboard())
        return

    if act == "plans":
        from app.database.crud import employee_bonus_overview
        plans = await asyncio.to_thread(lambda: _db_call(lambda db: employee_bonus_overview(db, uid)["plans"]))
        from bot.menus import _home_keyboard
        await query.edit_message_text(format_plans_list(plans), reply_markup=_home_keyboard())
        return

    if act == "points":
        from app.database.crud import list_loyalty_accounts, loyalty_config_get
        accounts, cfg = await asyncio.to_thread(lambda: _db_call(lambda db: (list_loyalty_accounts(db, uid), loyalty_config_get(db, uid))))
        from bot.menus import _home_keyboard
        if cfg is None:
            await query.edit_message_text("نقاط الولاء غير مفعّلة.\nلتفعيل: /bonus points تفعيل", reply_markup=_home_keyboard())
        else:
            await query.edit_message_text(format_loyalty_accounts(accounts, cfg), reply_markup=_home_keyboard())
        return

    from bot.menus import PAGES, _tools_keyboard
    await query.edit_message_text(PAGES["tools"][0], reply_markup=_tools_keyboard())


# ---------- تسجيل ----------


def register_bonus_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("bonus", bonus_command))
    from bot.menus import HANDLERS
    HANDLERS["bn"] = _handle_bonus_menu


# ---------- تذكيرات ----------


async def bonus_reminder_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.database.crud import (
        advance_employee_bonus_due,
        due_employee_bonus_plans,
        employee_bonus_monthly_spent,
        list_bonus_events,
        record_bonus_grant,
    )
    from app.database.models import BonusEvent
    from app.timeutil import now_utc

    db = SessionLocal()
    try:
        due_plans = due_employee_bonus_plans(db)
        seen_owners: dict = {}
        for plan in due_plans:
            uid = plan.telegram_user_id
            seen_owners.setdefault(uid, []).append(plan)

        for uid, plans in seen_owners.items():
            for plan in plans:
                skip = False
                if plan.monthly_cap is not None:
                    spent = employee_bonus_monthly_spent(db, plan)
                    if spent >= plan.monthly_cap:
                        skip = True
                if not skip:
                    tx = record_bonus_grant(db, uid, plan.amount, plan.currency, direction="expense", person=plan.person, description=f"مكافأة دورية ({PLAN_FREQ_LABELS.get(plan.frequency, plan.frequency)})")
                    if tx:
                        try:
                            await context.bot.send_message(
                                chat_id=uid,
                                text=(
                                    f"🎁 تم منح مكافأة لـ {plan.person}!\n"
                                    f"   {EXPENSE} المبلغ: {float(plan.amount):,.2f} {plan.currency or ''}\n"
                                    f"   📝 مكافأة دورية ({PLAN_FREQ_LABELS.get(plan.frequency, plan.frequency)})"
                                ),
                            )
                        except Exception as exc:
                            logger.error("فشل إرسال تذكير مكافأة للمستخدم %s: %s", uid, exc)
                advance_employee_bonus_due(db, plan)

        today = now_utc().date()
        event_owners = db.query(BonusEvent.telegram_user_id).filter(BonusEvent.status.in_(["planned", "active"])).distinct().all()
        for (uid,) in event_owners:
            events = list_bonus_events(db, uid)
            for ev in events:
                if ev.end_at and ev.end_at.date() == today and ev.status != "ended":
                    try:
                        await context.bot.send_message(
                            chat_id=uid,
                            text=f"🎯 الفعالية «{ev.name}» تنتهي اليوم!\nأرسل /bonus event إنهاء {ev.id} لإنهائها.",
                        )
                    except Exception as exc:
                        logger.error("فشل إرسال تذكير فعالية للمستخدم %s: %s", uid, exc)

    except Exception:
        logger.exception("خطأ في فحص تذكيرات البونس")
    finally:
        db.close()


def setup_bonus_check(app: Application) -> None:
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — تذكيرات البونس لن تعمل.")
        return
    app.job_queue.run_repeating(bonus_reminder_check, interval=timedelta(minutes=30), first=timedelta(seconds=120), name="bonus_reminder_check")
    logger.info("تم تسجيل فحص تذكيرات البونس كل 30 دقيقة")
