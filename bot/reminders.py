"""
تذكيرات تلقائية بالمهام المتأخرة عبر Job Queue.

تستخدم job_queue من python-telegram-bot (APScheduler خلفية) لإرسال
إشعارات للمستخدمين عند وجود مهام متأخرة لم تُرسل لها تذكير بعد.

تتضمن:
- فحص المهام المتأخرة (كل 15 دقيقة)
- ملخص صباحي مختصر (يومي)
- تقارير دورية (يومي/أسبوعي/شهري)
- تنبيهات الميزانيات / الحدود الائتمانية / الانحراف

يُفعَّل تلقائيًا عند تشغيل البوت عبر bot.py → register_handlers.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from telegram.ext import Application, ContextTypes

from app.database.crud import mark_overdue_tasks
from app.database.db import SessionLocal
from app.database.models import Budget, CreditLimit, Invoice, ReportPref, Task
from app.formatting import fmt_amount
from app.timeutil import now_local, now_utc
from bot.icons import EXPENSE, TASK, WARNING

logger = logging.getLogger(__name__)


def _user_pref_flag(db, uid: int, field: str) -> bool:
    """يعيد True إذا لم يكن الإعداد مُعطَّلًا (True أو لا يوجد سجل)."""
    from app.database.models import UserPref

    pref = db.query(UserPref).filter(UserPref.telegram_user_id == uid).first()
    if pref is None:
        return True
    return getattr(pref, field, True)


# الفاصل الزمني بين كل فحص (بالدقائق)
CHECK_INTERVAL_MINUTES = 15


async def overdue_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """الدالة الرئيسية التي تُنفَّذ كل 15 دقيقة.

    1. تجلب جميع المستخدمين الذين لديهم مهام pending.
    2. تحدّث المهام المتأخرة (mark_overdue_tasks).
    3. تبحث عن مهام متأخرة جديدة (reminder_sent=False).
    4. تُرسل إشعارًا تيليجرام لكل مهمة.
    5. تُحدّث reminder_sent=True.

    يجب أن تكون دالة async: JobQueue في python-telegram-bot v20+ ينفّذ
    callbacks عبر `await callback(context)` — أي دالة sync تُرجع None
    فتفشل بـ TypeError في كل تشغيل.
    """
    db = SessionLocal()
    try:
        # جميع المستخدمين الذين لديهم مهام pending
        user_ids = (
            db.query(Task.telegram_user_id)
            .filter(
                Task.status == "pending",
                Task.deleted_at.is_(None),
            )
            .distinct()
            .all()
        )

        for (uid,) in user_ids:
            # تحديث المهام المتأخرة
            if not _user_pref_flag(db, uid, "notif_task_reminder"):
                continue
            mark_overdue_tasks(db, uid)

            # المهام المتأخرة التي لم تُرسل لها تذكير بعد
            overdue_tasks = (
                db.query(Task)
                .filter(
                    Task.telegram_user_id == uid,
                    Task.status == "overdue",
                    Task.deleted_at.is_(None),
                    Task.reminder_sent == False,  # noqa: E712
                )
                .all()
            )

            if not overdue_tasks:
                continue

            # بناء رسالة التذكير
            lines = ["⚠️ **لديك مهام متأخرة:**\n"]
            for i, t in enumerate(overdue_tasks, 1):
                due_str = ""
                if t.due_date:
                    from app.timeutil import to_local_naive

                    due_local = to_local_naive(t.due_date)
                    due_str = f" (موعد: {due_local.strftime('%Y-%m-%d %H:%M')})"
                lines.append(f"{i}. {t.description}{due_str}")

            lines.append("\nأرسل `شو المهام المتأخرة؟` لعرضها بالتفصيل.")

            message = "\n".join(lines)

            # الإرسال عبر context.bot
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=message,
                    parse_mode="Markdown",
                )
                logger.info("تم إرسال تذكير %d مهمة للمستخدم %s", len(overdue_tasks), uid)
            except Exception as exc:
                logger.error("فشل إرسال تذكير للمستخدم %s: %s", uid, exc)
                continue

            # تحديث reminder_sent
            for t in overdue_tasks:
                t.reminder_sent = True
            db.commit()

    except Exception:
        logger.exception("خطأ في فحص المهام المتأخرة")
    finally:
        db.close()


def setup_overdue_reminder(app: Application) -> None:
    """يُسجّل مهمة متكررة في job_queue لفحص المهام المتأخرة."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — التذكيرات التلقائية لن تعمل.")
        return

    app.job_queue.run_repeating(
        overdue_check,
        interval=timedelta(minutes=CHECK_INTERVAL_MINUTES),
        first=timedelta(seconds=30),  # أول فحص بعد 30 ثانية من التشغيل
        name="overdue_reminder",
    )
    logger.info(
        "تم تسجيل فحص المهام المتأخرة كل %d دقيقة",
        CHECK_INTERVAL_MINUTES,
    )


# ---------- تنبيهات الميزانيات الشهرية ----------

WARNING_THRESHOLD = 0.8  # تنبيه اقتراب عند تجاوز 80%


async def budget_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يفحص كل الميزانيات الشهرية ويرسل تنبيهات (اقتراب/تجاوز).

    يُنفَّذ مع بقية المهام الدورية. لكل ميزانية:
    - إذا تغيّر الشهر (month_key) → إعادة ضبط حالة التنبيه.
    - إذا الاستهلاك ≥ السقف ولم يُنبه بالتجاوز بعد → تنبيه "تجاوز".
    - وإلا إذا الاستهلاك ≥ 80% ولم يُنبه بالاقتراب → تنبيه "اقتراب".

    تُعالَج كل ميزانية مرة واحدة بالضبط لكل دورة: بدل التكرار على مالكي
    الميزانيات ثم list_budgets (الذي يوسّع لكل أعضاء المساحة)، والذي كان
    يُعيد الميزانية نفسها مرة واحدة لكل مبتدئ ميزانية في المساحة — فتُحسب
    N مرات دون داعٍ، وتحتمل أي تعديل مستقبلي تكرار تنبيهات فعلية.

    تحسين الأداء: جلسة DB واحدة لكل الدورة (بدل N اتصالات كل 15 دقيقة) مع
    عزل أخطاء كل ميزانية عبر try/rollback و commit دوري. مع مئات الميزانيات
    كان النمط السابق `for bid: db=SessionLocal()` مكلفًا.
    """
    from app.database.crud import budget_monthly_reset, budget_usage
    from app.database.models import Budget

    db = SessionLocal()
    try:
        try:
            budget_ids = [
                bid
                for (bid,) in db.query(Budget.id).order_by(Budget.created_at.asc()).all()
            ]
        except Exception:
            logger.exception("خطأ في جلب الميزانيات")
            return

        for bid in budget_ids:
            try:
                budget = db.query(Budget).filter(Budget.id == bid).first()
                if budget is None:
                    continue
                if not _user_pref_flag(db, budget.telegram_user_id, "notif_budget_alert"):
                    continue
                budget_monthly_reset(db, budget)
                usage = budget_usage(db, budget)
                await _notify_budget(context, db, budget, usage)
            except Exception:
                logger.exception("خطأ في فحص ميزانية #%s", bid)
                try:
                    db.rollback()
                except Exception:
                    pass
    finally:
        try:
            db.close()
        except Exception:
            pass


async def _broadcast_budget_alert(
    context: ContextTypes.DEFAULT_TYPE, db: Session, budget: Budget, lines: list[str], status: int
) -> None:
    """يرسل تنبيه ميزانية لكل أعضاء المساحة المشتركة (لا المُنشئ فقط).

    budget_usage يحسب الاستهلاك عبر accessible_user_ids (كل الأعضاء)، لذا كل من
    يرى الميزانية يجب أن يصلَه التنبيه — وإلا تجاوزَ أحد الأعضاء السقف ولم يعلم.
    """
    from app.database.crud import accessible_user_ids

    sent_any = False
    for uid in accessible_user_ids(db, budget.telegram_user_id):
        try:
            await context.bot.send_message(chat_id=uid, text="\n".join(lines), parse_mode="Markdown")
            sent_any = True
        except Exception as exc:
            logger.error("فشل إرسال تنبيه ميزانية %s للمستخدم %s: %s", budget.id, uid, exc)
    if sent_any:
        budget.alerted_status = status
        budget.updated_at = now_utc()
        db.commit()
        logger.info("تنبيه ميزانية %s أُرسل بنجاح (status=%s)", budget.id, status)


async def _notify_budget(
    context: ContextTypes.DEFAULT_TYPE, db: Session, budget: Budget, usage: dict[str, object]
) -> None:
    """يرسل تنبيه اقتراب/تجاوز لميزانية واحدة إذا استحق (مرة واحدة كل شهر).

    الإرسال موحَّد لكل أعضاء المساحة المشتركة عبر _broadcast_budget_alert.
    """
    from app.exchange import CURRENCY_NAMES

    limit = usage["limit"]
    spent = usage["spent"]
    percent = usage["percent"]
    over = usage["over"]

    if budget.scope == "currency":
        target_txt = CURRENCY_NAMES.get(budget.currency, budget.currency)
        budget_name = budget.name or f"مصروفات {target_txt}"
    elif budget.scope == "category":
        target_txt = budget.category
        budget_name = budget.name or f"مصروفات {budget.category}"
    else:
        budget_name = budget.name or f"مصروفات {budget.person}"
        target_txt = budget.person

    if over and budget.alerted_status < 2:
        lines = [
            f"⚠️ تجاوزت ميزانيتك لـ**{budget_name}**!",
            f"المصروف: {fmt_amount(spent)} {budget.currency or ''}",
            f"السقف: {fmt_amount(limit)}",
            f"الاستهلاك: {percent}%",
        ]
        await _broadcast_budget_alert(context, db, budget, lines, status=2)
        return

    if percent >= WARNING_THRESHOLD * 100 and budget.alerted_status < 1:
        lines = [
            f"⚠️ اقتربت من سقف ميزانيتك لـ**{budget_name}**",
            f"المصروف: {fmt_amount(spent)} {budget.currency or ''} من أصل {fmt_amount(limit)}",
            f"الاستهلاك: {percent}%",
        ]
        await _broadcast_budget_alert(context, db, budget, lines, status=1)


def setup_budget_check(app: Application) -> None:
    """يُسجّل مهمة متكررة لفحص الميزانيات الشهرية."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — تنبيهات الميزانيات لن تعمل.")
        return
    app.job_queue.run_repeating(
        budget_check,
        interval=timedelta(minutes=CHECK_INTERVAL_MINUTES),
        first=timedelta(seconds=40),
        name="budget_check",
    )
    logger.info("تم تسجيل فحص الميزانيات كل %d دقيقة", CHECK_INTERVAL_MINUTES)


# ---------- تنبيهات الفواتير الآجلة (#22) ----------


async def invoice_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعلّم الفواتير المعلّقة المتأخرة ويرسل تنبيهًا (مرة واحدة) لكل أصحابها.

    يُشار إلى الفواتير التي لا تزال pending مع تاريخ استحقاق ماضٍ على أنها
    overdue، ويُنبَّه الأعضاء بلا تكرار بفضل عمود alerted (لا يُعاد إلا عند
    إنشاء فاتورة جديدة متأخرة).
    """
    from app.database.crud import accessible_user_ids, mark_overdue_invoices

    db = SessionLocal()
    try:
        overdue = mark_overdue_invoices(db)
    except Exception:
        logger.exception("خطأ في جلب الفواتير المتأخرة")
        db.close()
        return
    db.close()

    for inv in overdue:
        db = SessionLocal()
        try:
            if not _user_pref_flag(db, inv.telegram_user_id, "notif_invoice_alert"):
                continue
            if inv.alerted:
                db.close()
                continue
            owner = inv.telegram_user_id
            lines = [
                "⚠️ فاتورة آجلة استحقت ولم تُسدَّد!",
                f"#{inv.id} {inv.person or 'بدون شخص'}: {fmt_amount(inv.amount)} {inv.currency or ''}",
            ]
            if inv.description:
                lines.append(inv.description[:80])
            sent_any = False
            for uid in accessible_user_ids(db, owner):
                try:
                    await context.bot.send_message(chat_id=uid, text="\n".join(lines), parse_mode="Markdown")
                    sent_any = True
                except Exception as exc:
                    logger.error("فشل إرسال تنبيه فاتورة %s للمستخدم %s: %s", inv.id, uid, exc)
            if sent_any:
                inv.alerted = True
                inv.updated_at = now_utc()
                db.commit()
                logger.info("تنبيه فاتورة متأخرة %s أُرسل", inv.id)
        except Exception:
            logger.exception("خطأ في إرسال تنبيه فاتورة %s", inv.id)
        finally:
            db.close()


def setup_invoice_check(app: Application) -> None:
    """يُسجّل مهمة متكررة لفحص الفواتير الآجلة المتأخرة."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — تنبيهات الفواتير لن تعمل.")
        return
    app.job_queue.run_repeating(
        invoice_check,
        interval=timedelta(minutes=CHECK_INTERVAL_MINUTES),
        first=timedelta(seconds=50),
        name="invoice_check",
    )
    logger.info("تم تسجيل فحص الفواتير المتأخرة كل %d دقيقة", CHECK_INTERVAL_MINUTES)


# ---------- تنبيهات الحدود الائتمانية (#26) ----------


async def credit_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل تنبيه اقتراب/تجاوز لكل حد ائتماني عند تحقيقه (مرة واحدة لكل مستوى).

    مثل الميزانيات: يُعاد ضبط حالة التنبيه عند تغيّر الشهر المحلي
    (credit_monthly_reset) — فلا يعلّق السقف المارَّ التنبيهات للأبد.

    تُعالَج كل حدود ائتمانية مرة واحدة بالضبط لكل دورة — كما في budget_check
    (الوضع السابق كان يكرر الحساب مرة لكل منشئ حدّ في المساحة المشتركة).

    تحسين الأداء: جلسة واحدة لكل الدورة بدل N اتصالات كل 15 دقيقة، مع عزل
    أخطاء كل حدّ عبر rollback.
    """
    from app.database.crud import credit_monthly_reset, credit_usage
    from app.database.models import CreditLimit

    db = SessionLocal()
    try:
        try:
            limit_ids = [
                lid for (lid,) in db.query(CreditLimit.id).order_by(CreditLimit.created_at.asc()).all()
            ]
        except Exception:
            logger.exception("خطأ في جلب الحدود الائتمانية")
            return

        for lid in limit_ids:
            try:
                limit_row = db.query(CreditLimit).filter(CreditLimit.id == lid).first()
                if limit_row is None:
                    continue
                if not _user_pref_flag(db, limit_row.telegram_user_id, "notif_credit_alert"):
                    continue
                credit_monthly_reset(db, limit_row)
                usage = credit_usage(db, limit_row)
                # عملات متعددة بلا توحيد (لا أسعار) — لا نبني رقمًا مختلطًا ولا ننبه عليه
                if not usage.get("unified_ok"):
                    continue
                if not usage.get("amount") or usage.get("limit", 0) <= 0:
                    continue
                await _notify_credit(context, db, limit_row, usage)
            except Exception:
                logger.exception("خطأ في فحص حد ائتماني #%s", lid)
                try:
                    db.rollback()
                except Exception:
                    pass
    finally:
        try:
            db.close()
        except Exception:
            pass


async def _notify_credit(
    context: ContextTypes.DEFAULT_TYPE, db: Session, limit_row: CreditLimit, usage: dict[str, object]
) -> None:
    """ينبّه على اقتراب/تجاوز حد ائتماني واحد إن استحق (مرة لكل مستوى).

    يعمل في الاتجاهين: «عليك له» (دَيْن مورد) أو «مدين لك» (رصيد عميل) —
    نفس التَّرحيب مع تسمية الاتجاه حسب side.
    """
    from app.database.crud import accessible_user_ids

    over = usage["over"]
    percent = usage["percent"]
    name = limit_row.person
    if usage["side"] == "receivable":
        relation = f"مدين لك {usage['amount']}"
    elif usage["side"] == "payable":
        relation = f"الدين عليك له {usage['outstanding']}"
    else:
        relation = "صفر"

    notify_status = None
    if over and limit_row.alerted_status < 2:
        name_line = f"⚠️ تجاوزت حدّك الائتماني مع **{name}**!"
        notify_status = 2
    elif percent >= WARNING_THRESHOLD * 100 and limit_row.alerted_status < 1:
        name_line = f"⚠️ اقتربت من حدّك الائتماني مع **{name}**"
        notify_status = 1
    else:
        return
    detail = f"{relation} من أصل {fmt_amount(usage['limit'])} ({usage['percent']}%)"

    sent_any = False
    for uid in accessible_user_ids(db, limit_row.telegram_user_id):
        try:
            await context.bot.send_message(
                chat_id=uid, text=f"{name_line}\n{detail}", parse_mode="Markdown"
            )
            sent_any = True
        except Exception as exc:
            logger.error("فشل إرسال تنبيه حد ائتماني %s للمستخدم %s: %s", limit_row.id, uid, exc)
    if sent_any:
        limit_row.alerted_status = notify_status
        limit_row.updated_at = now_utc()
        db.commit()
        logger.info("تنبيه حد ائتماني %s أُرسل (status=%s)", limit_row.id, notify_status)


def setup_credit_check(app: Application) -> None:
    """يُسجّل مهمة متكررة لفحص الحدود الائتمانية."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — تنبيهات الحدود الائتمانية لن تعمل.")
        return
    app.job_queue.run_repeating(
        credit_check,
        interval=timedelta(minutes=CHECK_INTERVAL_MINUTES),
        first=timedelta(seconds=55),
        name="credit_check",
    )
    logger.info("تم تسجيل فحص الحدود الائتمانية كل %d دقيقة", CHECK_INTERVAL_MINUTES)


# ---------- التقارير الدورية التلقائية ----------

REPORT_CHECK_INTERVAL_MINUTES = 30  # نفحص كل 30 دقيقة ونرسل عند استحقاق وقت الإرسال


def _parse_deliver_time(pref: ReportPref) -> tuple[int, int]:
    from app.config import settings

    deliver = pref.deliver_time or settings.report_time
    try:
        hh, mm = (int(p) for p in deliver.split(":", 1))
    except Exception:
        return 19, 0
    return hh, mm


def _report_due(pref: ReportPref, now_local_dt: datetime) -> bool:
    """هل حان وقت إرسال تقرير المستخدم (الدورية + الوقت + عدم التكرار)؟"""
    from app.timeutil import first_day_of_week, to_local_naive

    hh, mm = _parse_deliver_time(pref)
    if (now_local_dt.hour, now_local_dt.minute) < (hh, mm):
        return False

    last = pref.last_sent_at
    freq = pref.frequency

    if freq == "daily":
        if last is None:
            return True
        return to_local_naive(last).date() < now_local_dt.date()

    if freq == "weekly":
        # يُرسل أول يوم في الأسبوع (افتراضيًا الأحد) — يراجع الأسبوع المنتهي
        if now_local_dt.weekday() != first_day_of_week():
            return False
        if last is None:
            return True
        return to_local_naive(last).date() < now_local_dt.date()

    if freq == "monthly":
        # يُرسل أول يوم من الشهر — يراجع الشهر المنتهي
        if now_local_dt.day != 1:
            return False
        if last is None:
            return True
        last_local = to_local_naive(last)
        return (last_local.year, last_local.month) != (now_local_dt.year, now_local_dt.month)

    return False


async def periodic_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل التقارير الدورية للمستخدمين الذين فعّلوها (عند استحقاق الموعد)."""
    from app.database.crud import list_report_prefs, mark_report_sent
    from app.timeutil import now_local
    from bot.reports import build_periodic_summary

    now_dt = now_local()
    db = SessionLocal()
    try:
        prefs = list_report_prefs(db)
    except Exception:
        logger.exception("خطأ في جلب تفضيلات التقارير")
        db.close()
        return

    for pref in prefs:
        try:
            if not _user_pref_flag(db, pref.telegram_user_id, "notif_periodic_report"):
                continue
            if not _report_due(pref, now_dt):
                continue
            message = build_periodic_summary(
                db, pref.telegram_user_id, pref.frequency, now_dt=now_dt
            )
            await context.bot.send_message(chat_id=pref.telegram_user_id, text=message)
            mark_report_sent(db, pref)
            logger.info("أُرسل التقرير %s للمستخدم %s", pref.frequency, pref.telegram_user_id)
        except Exception as exc:
            logger.error("فشل إرسال التقرير الدوري للمستخدم %s: %s", pref.telegram_user_id, exc)
    db.close()


def setup_periodic_reports(app: Application) -> None:
    """يُسجّل مهمة متكررة لفحص التقارير الدورية وإرسالها عند الاستحقاق."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — التقارير الدورية لن تعمل.")
        return
    app.job_queue.run_repeating(
        periodic_report_job,
        interval=timedelta(minutes=REPORT_CHECK_INTERVAL_MINUTES),
        first=timedelta(seconds=60),
        name="periodic_reports",
    )
    logger.info("تم تسجيل فحص التقارير الدورية كل %d دقيقة", REPORT_CHECK_INTERVAL_MINUTES)


# ---------- تنبيه انحراف الإنفاق عن المتوسط (#37) ----------

_LAST_DEVIATION_SENT: dict[int, str] = {}


async def deviation_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل تنبيهًا يوميًا واحدًا لكل مساحة عمل عند انحراف شهرٍ فوق حدٍّ.

    الحارس في الذاكرة (date لكل مستخدم) — يُعاد التنبيه في اليوم التالي فقط.
    """
    from app.database.crud import accessible_user_ids, deviation_summary, user_ids_with_data

    db = SessionLocal()
    try:
        today = now_utc().strftime("%Y-%m-%d")
        for uid in user_ids_with_data(db):
            try:
                if not _user_pref_flag(db, uid, "notif_deviation"):
                    continue
                summary = deviation_summary(db, uid)
                flagged = [d for d in summary["deviations"] if d.get("significant")]
                if not flagged or _LAST_DEVIATION_SENT.get(uid) == today:
                    continue

                msg = (
                    "🚨 انحراف واضح في إنفاقاتك هذا الشهر\n\n"
                    + "\n".join(
                        f"• {('مصاريف' if d['kind'] == 'expense' else 'إيرادات')} ({d['currency']}): {d['pct']}%"
                        for d in flagged
                    )
                    + "\n\nللمفاصلة: /deviation"
                )
                for member in accessible_user_ids(db, uid):
                    try:
                        await context.bot.send_message(chat_id=member, text=msg)
                    except Exception:  # noqa: BLE001
                        logger.exception("فشل إرسال تنبيه الانحراف للمستخدم %s", member)
                _LAST_DEVIATION_SENT[uid] = today
            except Exception:  # noqa: BLE001
                logger.exception("خطأ في حسابات الانحراف للمستخدم %s", uid)
    finally:
        db.close()


def setup_deviation_check(app: Application) -> None:
    """يُسجّل مهمة متكررة لفحص انحراف الإنفاق."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — تنبيهات الانحراف لن تعمل.")
        return
    app.job_queue.run_repeating(
        deviation_check,
        interval=timedelta(minutes=CHECK_INTERVAL_MINUTES),
        first=timedelta(seconds=90),
        name="deviation_check",
    )
    logger.info("تم تسجيل فحص الانحراف كل %d دقيقة", CHECK_INTERVAL_MINUTES)


# ---------- النشرة الاستباقية اليومية (تنبيهات مجمّعة) ----------

# يُرسَل مرة واحدة يوميًا لكل مستخدم (حارس في الذاكرة). تُغطّي ما يحتاج
# انتباهًا *قبل* أن يصير متأخرًا: مهام وفواتير تستحق خلال DAYS_AHEAD يومًا +
# ميزانيات على السقف — بسقف إسهاب (لا تُغرق المستخدم بإشعارات منفصلة).
PROACTIVE_DAYS_AHEAD = 3
PROACTIVE_MAX_ITEMS_PER_GROUP = 3
_PROACTIVE_HOUR = 9  # الساعة 09:00 بالتوقيت المحلي

_LAST_PROACTIVE_SENT: dict[int, str] = {}


def build_proactive_digest(db: Session, uid: int, *, days_ahead: int = PROACTIVE_DAYS_AHEAD) -> str | None:
    """يبني نشرة تنبيهات استباقية (سطر واحد بلا استدعاء شبكة).

    يرجع None إذا لم يكن هناك ما يستحق التنبيه (لا رسالة فارغة). الفترة الآتية
    تُحسب بحدود local → UTC عبر to_utc_naive. الأسماء/المبالغ من القاعدة — ليست
    نصوصًا قابلة للتنفيذ.
    """
    from app.database.crud import accessible_user_ids, budget_usage, list_budgets
    from app.timeutil import to_local_naive, to_utc_naive

    now = now_local()
    now_utc_dt = to_utc_naive(now)
    soon_utc = to_utc_naive(now + timedelta(days=days_ahead))
    lines: list[str] = []

    def _in_window(dt) -> str | None:
        """يعيد «متأخرة»/«تستحق قريبًا» حسب الموعد، أو None خارج النافذة."""
        if not dt:
            return None
        if dt < now_utc_dt:
            return "متأخرة"
        if dt <= soon_utc:
            return "قريبًا"
        return None

    ids = accessible_user_ids(db, uid)

    tasks = (
        db.query(Task)
        .filter(
            Task.telegram_user_id.in_(ids),
            Task.deleted_at.is_(None),
            Task.status.in_(["pending", "overdue"]),
        )
        .all()
    )
    task_items = []
    for t in tasks:
        state = _in_window(t.due_date)
        if state is None:
            continue
        when = to_local_naive(t.due_date).strftime("%m-%d %H:%M") if t.due_date else ""
        task_items.append(f"{TASK} {t.description[:50]}{' — ' + when if when else ''} ({state})")
    if task_items:
        shown = task_items[:PROACTIVE_MAX_ITEMS_PER_GROUP]
        lines.append("🗓️ مهام تستحق الانتباه:")
        lines.extend(f"  {row}" for row in shown)
        if len(task_items) > len(shown):
            lines.append(f"  … و{len(task_items) - len(shown)} أخرى")

    invoices = (
        db.query(Invoice)
        .filter(
            Invoice.telegram_user_id.in_(ids),
            Invoice.status.in_(["pending", "overdue"]),
        )
        .all()
    )
    inv_items = []
    for inv in invoices:
        state = _in_window(inv.due_date)
        if state is None:
            continue
        when = to_local_naive(inv.due_date).strftime("%m-%d") if inv.due_date else ""
        inv_items.append(
            f"#{inv.id} {inv.person or 'بدون شخص'}: {fmt_amount(inv.amount)} {inv.currency or ''}"
            f"{' — ' + when if when else ''} ({state})"
        )
    if inv_items:
        shown = inv_items[:PROACTIVE_MAX_ITEMS_PER_GROUP]
        lines.append("🧾 فواتير تستحق الانتباه:")
        lines.extend(f"  {row}" for row in shown)
        if len(inv_items) > len(shown):
            lines.append(f"  … و{len(inv_items) - len(shown)} أخرى")

    budget_warns = []
    for b in list_budgets(db, uid):
        usage = budget_usage(db, b)
        if usage.get("over"):
            budget_warns.append((b, usage, "تجاوزت السقف"))
        elif usage.get("percent", 0) >= 80:
            budget_warns.append((b, usage, "قريبة من السقف"))
    if budget_warns:
        shown = budget_warns[:PROACTIVE_MAX_ITEMS_PER_GROUP]
        lines.append("💰 الميزانيات:")
        for b, usage, status in shown:
            target = b.name or b.person or b.currency or b.category or "ميزانية"
            lines.append(f"  {EXPENSE} {target}: {usage['spent']} / {usage['limit']} ({usage['percent']}%) — {status}")
        if len(budget_warns) > len(shown):
            lines.append(f"  … و{len(budget_warns) - len(shown)} أخرى")

    if not lines:
        return None
    lines.append("\nللتفاصيل: /kpi · /tasks · /invoices · /budget")
    return "\n".join(lines)


async def proactive_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل النشرة الاستباقية اليومية لكل مستخدم لديه ما يستحق التنبيه."""
    from app.database.crud.reports import user_ids_with_data

    db = SessionLocal()
    try:
        today = now_utc().strftime("%Y-%m-%d")
        for uid in user_ids_with_data(db):
            try:
                if not _user_pref_flag(db, uid, "notif_proactive"):
                    continue
                if _LAST_PROACTIVE_SENT.get(uid) == today:
                    continue
                digest = build_proactive_digest(db, uid)
                if digest is None:
                    continue
                await context.bot.send_message(chat_id=uid, text=digest)
                _LAST_PROACTIVE_SENT[uid] = today
            except Exception:  # noqa: BLE001
                logger.exception("فشل إرسال النشرة الاستباقية للمستخدم %s", uid)
    finally:
        db.close()


def setup_proactive_check(app: Application) -> None:
    """يُسجّل مهمة يومية (الساعة 09:05) للنشرة الاستباقية المجمّعة."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — النشرة الاستباقية لن تعمل.")
        return

    target = now_local().replace(hour=_PROACTIVE_HOUR, minute=5, second=0, microsecond=0)
    if target <= now_local():
        target += timedelta(days=1)
    delay = target - now_local()
    app.job_queue.run_repeating(
        proactive_check,
        interval=timedelta(hours=24),
        first=delay,
        name="proactive_check",
    )
    logger.info("تم تسجيل النشرة الاستباقية اليومية (%d:05)", _PROACTIVE_HOUR)


# ---------- النسخ الاحتياطي اليومي التلقائي ----------

BACKUP_INTERVAL_HOURS = 24


async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """نسخة احتياطية يومية من قاعدة SQLite عبر خدمة app.database.backup."""
    from app.database.backup import run_backup

    try:
        run_backup()
    except Exception:
        logger.exception("فشل النسخ الاحتياطي اليومي")


def setup_daily_backup(app: Application) -> None:
    """يُسجّل دورة يومية للنسخ الاحتياطي (أول نسخة بعد ~ساعة من التشغيل)."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — النسخ الاحتياطي اليومي لن يعمل.")
        return
    app.job_queue.run_repeating(
        daily_backup_job,
        interval=timedelta(hours=BACKUP_INTERVAL_HOURS),
        first=timedelta(hours=1),
        name="daily_backup",
    )
    logger.info("تم تسجيل النسخ الاحتياطي اليومي (%d ساعة)", BACKUP_INTERVAL_HOURS)


# ---------- الملخص الصباحي المختصر ----------

MORNING_SUMMARY_HOUR = 8  # الساعة 08:00 بالتوقيت المحلي


def build_morning_summary(db: Session, uid: int) -> str:
    """يبني ملخصًا صباحيًا مختصرًا (سطر واحد)."""
    from app.timeutil import to_local_naive, to_utc_naive

    today = now_local()
    today_start_local = today.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = to_utc_naive(today_start_local)
    today_end_utc = to_utc_naive(today_start_local + timedelta(days=1))

    tasks = (
        db.query(Task)
        .filter(
            Task.telegram_user_id == uid,
            Task.status.in_(["pending", "overdue"]),
            Task.deleted_at.is_(None),
        )
        .all()
    )
    overdue = [t for t in tasks if t.status == "overdue"]
    total_pending = len(tasks)

    def _in_today(dt) -> bool:
        if not dt:
            return False
        return today_start_utc <= dt < today_end_utc

    due_today = [t for t in tasks if _in_today(t.due_date)]

    invoices = (
        db.query(Invoice)
        .filter(
            Invoice.telegram_user_id == uid,
            Invoice.status.in_(["pending", "overdue"]),
        )
        .all()
    )
    invoices_due_today = [i for i in invoices if _in_today(i.due_date)]

    parts: list[str] = []

    if total_pending == 0:
        parts.append("لا توجد مهام معلّقة")
    else:
        task_txt = f"{total_pending} مهام"
        if overdue:
            task_txt += f" ({len(overdue)} {WARNING} متأخرة)"
        parts.append(task_txt)

    if due_today:
        times = sorted(to_local_naive(t.due_date).strftime("%H:%M") for t in due_today if t.due_date)
        parts.append(f"آخر موعد الساعة {times[0]}")

    if not invoices:
        parts.append("لا فواتير مستحقة")
    elif invoices_due_today:
        parts.append(f"{len(invoices_due_today)} فاتورة تستحق اليوم")

    return f"☀️ اليوم: {'، '.join(parts)}."


async def morning_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل ملخصًا صباحيًا مختصرًا لكل مستخدم لديه بيانات."""
    from app.database.crud.reports import user_ids_with_data

    db = SessionLocal()
    try:
        for uid in user_ids_with_data(db):
            try:
                if not _user_pref_flag(db, uid, "notif_morning_summary"):
                    continue
                summary = build_morning_summary(db, uid)
                await context.bot.send_message(chat_id=uid, text=summary)
            except Exception:  # noqa: BLE001
                logger.exception("فشل إرسال الملخص الصباحي للمستخدم %s", uid)
    finally:
        db.close()


def setup_morning_summary(app: Application) -> None:
    """يُسجّل مهمة يومية لإرسال ملخص صباحي."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — الملخص الصباحي لن يعمل.")
        return

    now_dt = now_local()
    target = now_dt.replace(hour=MORNING_SUMMARY_HOUR, minute=5, second=0, microsecond=0)
    if target <= now_dt:
        target += timedelta(days=1)
    delay = target - now_dt

    app.job_queue.run_repeating(
        morning_summary_job,
        interval=timedelta(hours=24),
        first=delay,
        name="morning_summary",
    )
    logger.info("تم تسجيل الملخص الصباحي (يوميًا الساعة %d:05)", MORNING_SUMMARY_HOUR)
