"""
تذكيرات تلقائية بالمهام المتأخرة عبر Job Queue.

تستخدم job_queue من python-telegram-bot (APScheduler خلفية) لإرسال
إشعارات للمستخدمين عند وجود مهام متأخرة لم تُرسل لها تذكير بعد.

يُفعَّل تلقائيًا عند تشغيل البوت عبر bot.py → register_handlers.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from telegram.ext import Application, ContextTypes

from app.database.crud import mark_overdue_tasks
from app.database.db import SessionLocal
from app.database.models import Budget, CreditLimit, ReportPref, Task
from app.timeutil import now_utc

logger = logging.getLogger(__name__)

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
    """
    from app.database.crud import budget_monthly_reset, budget_usage, list_budgets
    from app.database.models import Budget

    db = SessionLocal()
    try:
        user_ids = db.query(Budget.telegram_user_id).distinct().all()
    except Exception:
        logger.exception("خطأ في جلب المستخدمين للميزانيات")
        db.close()
        return
    db.close()

    for (uid,) in user_ids:
        db = SessionLocal()
        try:
            budgets = list_budgets(db, uid)
            for budget in budgets:
                budget_monthly_reset(db, budget)
                usage = budget_usage(db, budget)
                await _notify_budget(context, db, budget, usage)
        except Exception:
            logger.exception("خطأ في فحص ميزانية المستخدم %s", uid)
        finally:
            db.close()


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
            f"المصروف: {spent} {budget.currency or ''}",
            f"السقف: {limit}",
            f"الاستهلاك: {percent}%",
        ]
        await _broadcast_budget_alert(context, db, budget, lines, status=2)
        return

    if percent >= WARNING_THRESHOLD * 100 and budget.alerted_status < 1:
        lines = [
            f"⚠️ اقتربت من سقف ميزانيتك لـ**{budget_name}**",
            f"المصروف: {spent} {budget.currency or ''} من أصل {limit}",
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
            if inv.alerted:
                db.close()
                continue
            owner = inv.telegram_user_id
            lines = [
                "⚠️ فاتورة آجلة استحقت ولم تُسدَّد!",
                f"#{inv.id} {inv.person or 'بدون شخص'}: {inv.amount} {inv.currency or ''}",
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
    """يرسل تنبيه اقتراب/تجاوز لكل حد ائتماني عند تحقيقه (مرة واحدة لكل مستوى)."""
    from app.database.crud import credit_usage, list_credit_limits
    from app.database.models import CreditLimit

    db = SessionLocal()
    try:
        user_ids = db.query(CreditLimit.telegram_user_id).distinct().all()
    except Exception:
        logger.exception("خطأ في جلب المستخدمين للحدود الائتمانية")
        db.close()
        return
    db.close()

    for (uid,) in user_ids:
        db = SessionLocal()
        try:
            limits = list_credit_limits(db, uid)
            for lim in limits:
                usage = credit_usage(db, lim)
                if usage["amount"] <= 0 or usage["limit"] <= 0:
                    continue
                await _notify_credit(context, db, lim, usage)
        except Exception:
            logger.exception("خطأ في فحص حد ائتماني للمستخدم %s", uid)
        finally:
            db.close()


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
    detail = f"{relation} من أصل {usage['limit']} ({usage['percent']}%)"

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

FREQUENCY_NAMES = {"daily": "اليومي", "weekly": "الأسبوعي", "monthly": "الشهري"}


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
