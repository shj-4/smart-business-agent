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

import asyncio
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

    1. تجلب جميع المستخدمين الذين لديهم مهام pending أو overdue بلا تذكير.
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
        # جميع المستخدمين الذين لديهم مهام pending أو overdue لم يُذكّروا بعد
        # (كان pending فقط فيُفوّت مستخدم كل مهامه صارت overdue قبل الفحص)
        user_ids = (
            db.query(Task.telegram_user_id)
            .filter(
                Task.status.in_(["pending", "overdue"]),
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
            lines = ["لديك مهام متأخرة:\n"]
            for i, t in enumerate(overdue_tasks, 1):
                due_str = ""
                if t.due_date:
                    from app.timeutil import to_local_naive

                    due_local = to_local_naive(t.due_date)
                    due_str = f" (موعد: {due_local.strftime('%Y-%m-%d %H:%M')})"
                # إزالة Markdown (** و `) لأن الإرسال بلا parse_mode لتجنب BadRequest مع نصوص المستخدم
                safe_desc = str(t.description).replace("*", "").replace("`", "").replace("[", "").replace("_", " ")
                lines.append(f"{i}. {safe_desc}{due_str}")

            lines.append("\nأرسل: شو المهام المتأخرة؟ لعرضها بالتفصيل.")

            message = "\n".join(lines)

            # الإرسال عبر context.bot — بدون Markdown (الأوصاف قد تحوي _ * ` [)
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=message,
                )
                logger.info("تم إرسال تذكير %d مهمة للمستخدم %s", len(overdue_tasks), uid)
            except Exception as exc:
                logger.error("فشل إرسال تذكير للمستخدم %s: %s", uid, exc)
                # حتى مع فشل الإرسال، علّم كمرسل لتجنب التكرار كل 15 دقيقة
                for t in overdue_tasks:
                    t.reminder_sent = True
                db.commit()
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
    """يفحص كل الميزانيات الشهرية ويرسل تنبيهات (اقتراب/تجاوز) دون حظر الحلقة."""
    from app.database.models import Budget

    # جلب المعرفات فقط بجلسة قصيرة (سريعة)
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
    finally:
        try:
            db.close()
        except Exception:
            pass

    for bid in budget_ids:
        try:
            # العمل الثقيل (فك تشفير + تحويل عملات) خارج الحلقة
            def _eval(_bid=bid):  # noqa: B023
                s = SessionLocal()
                try:
                    b = s.query(Budget).filter(Budget.id == _bid).first()
                    if b is None:
                        return None
                    from app.database.crud import budget_monthly_reset, budget_usage

                    budget_monthly_reset(s, b)
                    usage = budget_usage(s, b)
                    # نحتاج نسخ البيانات قبل إغلاق الجلسة
                    return (b.id, usage)
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

            result = await asyncio.to_thread(_eval)
            if result is None:
                continue
            bid_real, usage = result
            # الإشعار يحتاج جلسة لإرسال + تحديث alerted_status
            db2 = SessionLocal()
            try:
                budget = db2.query(Budget).filter(Budget.id == bid_real).first()
                if budget is None:
                    continue
                await _notify_budget(context, db2, budget, usage)
            finally:
                try:
                    db2.close()
                except Exception:
                    pass
        except Exception:
            logger.exception("خطأ في فحص ميزانية #%s", bid)


async def _broadcast_budget_alert(
    context: ContextTypes.DEFAULT_TYPE, db: Session, budget: Budget, lines: list[str], status: int
) -> None:
    """يرسل تنبيه ميزانية لكل أعضاء المساحة المشتركة (لا المُنشئ فقط).

    يحترم صلاحية العضو (budget.manage أو report.view_all) وتفضيله الشخصي
    notif_budget_alert — لا يُزعج من عطّل التنبيه أو لا يملك حق الرؤية.
    """
    from app.database.crud import accessible_user_ids
    from app.database.crud.company import has_permission

    sent_any = False
    for uid in accessible_user_ids(db, budget.telegram_user_id):
        if not has_permission(db, uid, "budget.manage") and not has_permission(db, uid, "report.view_all"):
            continue
        # المالك يُنبَّه دائمًا (حتى لو عطّل الأعضاء)
        is_owner = uid == getattr(budget, "telegram_user_id", None)
        if not is_owner and not _user_pref_flag(db, uid, "notif_budget_alert"):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text="\n".join(lines))
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
            f"⚠️ تجاوزت ميزانيتك لـ {budget_name}!",
            f"المصروف: {fmt_amount(spent)} {budget.currency or ''}",
            f"السقف: {fmt_amount(limit)}",
            f"الاستهلاك: {percent}%",
        ]
        await _broadcast_budget_alert(context, db, budget, lines, status=2)
        return

    if percent >= WARNING_THRESHOLD * 100 and budget.alerted_status < 1:
        lines = [
            f"⚠️ اقتربت من سقف ميزانيتك لـ {budget_name}",
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
        mark_overdue_invoices(db)
        # احتفظ بمعرّفات فقط لتجنّب detached instances بعد إغلاق الجلسة
        overdue_ids = [inv.id for inv in db.query(Invoice).filter(Invoice.status == 'overdue', Invoice.alerted.is_(False)).all()]
    except Exception:
        logger.exception("خطأ في جلب الفواتير المتأخرة")
        db.close()
        return
    db.close()

    for inv_id in overdue_ids:
        db = SessionLocal()
        try:
            fresh = db.query(Invoice).filter(Invoice.id == inv_id).first()
            if fresh is None or fresh.alerted:
                db.close()
                continue
            owner = fresh.telegram_user_id
            lines = [
                "⚠️ فاتورة آجلة استحقت ولم تُسدَّد!",
                f"#{fresh.id} {fresh.person or 'بدون شخص'}: {fmt_amount(fresh.amount)} {fresh.currency or ''}",
            ]
            if fresh.description:
                lines.append(fresh.description[:80])
            from app.database.crud.company import has_permission

            sent_any = False
            for uid in accessible_user_ids(db, owner):
                if not has_permission(db, uid, "report.view_all") and uid != owner:
                    continue
                if not _user_pref_flag(db, uid, "notif_invoice_alert"):
                    continue
                try:
                    await context.bot.send_message(chat_id=uid, text="\n".join(lines))
                    sent_any = True
                except Exception as exc:
                    logger.error("فشل إرسال تنبيه فاتورة %s للمستخدم %s: %s", fresh.id, uid, exc)
            if sent_any:
                fresh.alerted = True
                fresh.updated_at = now_utc()
                db.commit()
                logger.info("تنبيه فاتورة متأخرة %s أُرسل", fresh.id)
        except Exception:
            logger.exception("خطأ في إرسال تنبيه فاتورة %s", inv_id)
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
    """يرسل تنبيه اقتراب/تجاوز لكل حد ائتماني دون حظر الحلقة."""
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
    finally:
        try:
            db.close()
        except Exception:
            pass

    for lid in limit_ids:
        try:
            def _eval(_lid=lid):  # noqa: B023
                s = SessionLocal()
                try:
                    row = s.query(CreditLimit).filter(CreditLimit.id == _lid).first()
                    if row is None:
                        return None
                    from app.database.crud import credit_monthly_reset, credit_usage

                    credit_monthly_reset(s, row)
                    usage = credit_usage(s, row)
                    return (row.id, usage)
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

            result = await asyncio.to_thread(_eval)
            if result is None:
                continue
            rid, usage = result
            if not usage.get("unified_ok"):
                continue
            if not usage.get("amount") or usage.get("limit", 0) <= 0:
                continue
            db2 = SessionLocal()
            try:
                limit_row = db2.query(CreditLimit).filter(CreditLimit.id == rid).first()
                if limit_row is None:
                    continue
                await _notify_credit(context, db2, limit_row, usage)
            finally:
                try:
                    db2.close()
                except Exception:
                    pass
        except Exception:
            logger.exception("خطأ في فحص حد ائتماني #%s", lid)


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
        name_line = f"⚠️ تجاوزت حدّك الائتماني مع {name}!"
        notify_status = 2
    elif percent >= WARNING_THRESHOLD * 100 and limit_row.alerted_status < 1:
        name_line = f"⚠️ اقتربت من حدّك الائتماني مع {name}"
        notify_status = 1
    else:
        return
    detail = f"{relation} من أصل {fmt_amount(usage['limit'])} ({usage['percent']}%)"

    from app.database.crud.company import has_permission

    sent_any = False
    for uid in accessible_user_ids(db, limit_row.telegram_user_id):
        if not has_permission(db, uid, "budget.manage") and not has_permission(db, uid, "report.view_all"):
            continue
        if not _user_pref_flag(db, uid, "notif_credit_alert"):
            continue
        try:
            await context.bot.send_message(
                chat_id=uid, text=f"{name_line}\n{detail}"
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
    """يرسل التقارير الدورية دون حظر الحلقة ومع تعويض الفائت."""
    from app.database.crud import list_report_prefs
    from app.timeutil import now_local
    from bot.reports import build_periodic_summary

    now_dt = now_local()
    db = SessionLocal()
    try:
        prefs = list_report_prefs(db)
        prefs_data = [(p.telegram_user_id, p.frequency, p.last_sent_at, p.deliver_time) for p in prefs]
    except Exception:
        logger.exception("خطأ في جلب تفضيلات التقارير")
        try:
            db.close()
        except Exception:
            pass
        return
    try:
        db.close()
    except Exception:
        pass

    for uid, freq, _last_sent, _deliver in prefs_data:
        try:
            # تحميل تفضيل طازج للتحقق من _report_due مع منطق التعويض
            def _check(u):
                s = SessionLocal()
                try:
                    from app.database.crud import get_report_pref

                    pref = get_report_pref(s, u)
                    if pref is None:
                        return False, None
                    return _report_due(pref, now_dt) or _report_due_with_catchup(pref, now_dt), pref
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

            due, pref_obj = await asyncio.to_thread(_check, uid)
            if not due or pref_obj is None:
                continue
            db_tmp = SessionLocal()
            try:
                if not _user_pref_flag(db_tmp, uid, "notif_periodic_report"):
                    continue
            finally:
                try:
                    db_tmp.close()
                except Exception:
                    pass

            def _build(u, f, ndt):
                s = SessionLocal()
                try:
                    return build_periodic_summary(s, u, f, now_dt=ndt)
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

            message = await asyncio.to_thread(_build, uid, freq, now_dt)
            await context.bot.send_message(chat_id=uid, text=message)
            # حدث last_sent في جلسة جديدة
            def _mark(u):
                s = SessionLocal()
                try:
                    from app.database.crud import get_report_pref, mark_report_sent

                    p = get_report_pref(s, u)
                    if p:
                        mark_report_sent(s, p)
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

            await asyncio.to_thread(_mark, uid)
            logger.info("أُرسل التقرير %s للمستخدم %s", freq, uid)
        except Exception as exc:
            logger.error("فشل إرسال التقرير الدوري للمستخدم %s: %s", uid, exc)


def _report_due_with_catchup(pref: ReportPref, now_local_dt: datetime) -> bool:
    """تعويض التقرير الفائت إذا كان البوت متوقفًا يوم الاستحقاق (خلال 48h)."""
    try:
        from app.timeutil import to_local_naive

        if pref.last_sent_at is None:
            return False
        last_local = to_local_naive(pref.last_sent_at)
        if pref.frequency == "weekly":
            # إذا مضى أسبوع ولم يُرسل (بسبب توقف)، عوض خلال يومين
            if (now_local_dt.date() - last_local.date()).days >= 7:
                return (now_local_dt.date() - last_local.date()).days < 9
        if pref.frequency == "monthly":
            if (now_local_dt.year, now_local_dt.month) != (last_local.year, last_local.month):
                # فائت هذا الشهر ولم يُرسل بعد، عوض خلال أول 3 أيام
                if now_local_dt.day <= 3:
                    return True
    except Exception:
        pass
    return False


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
    """يرسل تنبيهًا يوميًا واحدًا لكل فضاء عمل (dedupe per workspace) دون حظر الحلقة."""
    from app.database.crud import deviation_summary, user_ids_with_data
    from app.database.crud.company import has_permission

    # خريطة workspace -> representative uid لمنع N إرسالات
    db = SessionLocal()
    try:
        today = now_utc().strftime("%Y-%m-%d")
        # حمّل كل uid ثم حدّد representative لكل workspace
        all_uids = user_ids_with_data(db)
    finally:
        try:
            db.close()
        except Exception:
            pass

    # اجمع workspaces
    seen_ws: set = set()
    for uid in all_uids:
        # حدد anchor للمساحة
        def _anchor(u):
            s = SessionLocal()
            try:
                from app.database.crud.company import company_id_for_user

                cid = company_id_for_user(s, u)
                return cid if cid is not None else u
            finally:
                try:
                    s.close()
                except Exception:
                    pass

        anchor = await asyncio.to_thread(_anchor, uid)
        if anchor in seen_ws:
            continue
        seen_ws.add(anchor)
        if _LAST_DEVIATION_SENT.get(anchor) == today:
            continue

        def _calc(u):
            s = SessionLocal()
            try:
                return deviation_summary(s, u)
            finally:
                try:
                    s.close()
                except Exception:
                    pass

        summary = await asyncio.to_thread(_calc, uid)
        if summary.get("month_partial") and summary.get("month_elapsed_pct", 100) < 50:
            continue
        flagged = [d for d in summary["deviations"] if d.get("significant")]
        if not flagged:
            continue

        msg = (
            "🚨 انحراف واضح في إنفاقاتك هذا الشهر\n\n"
            + "\n".join(
                f"• {('مصاريف' if d['kind'] == 'expense' else 'إيرادات')} ({d['currency']}): {d['pct']}%"
                for d in flagged
            )
            + "\n\nللمفاصلة: /deviation"
        )
        # إرسال لكل أعضاء المساحة مرة واحدة
        def _members(u):
            s = SessionLocal()
            try:
                from app.database.crud import accessible_user_ids

                return list(accessible_user_ids(s, u))
            finally:
                try:
                    s.close()
                except Exception:
                    pass

        members = await asyncio.to_thread(_members, uid)
        sent_any = False
        for member in members:
            db2 = SessionLocal()
            try:
                if not has_permission(db2, member, "report.view_all") and member != uid:
                    continue
                if not _user_pref_flag(db2, member, "notif_deviation"):
                    continue
            finally:
                try:
                    db2.close()
                except Exception:
                    pass
            try:
                await context.bot.send_message(chat_id=member, text=msg)
                sent_any = True
            except Exception:
                logger.exception("فشل إرسال تنبيه الانحراف للمستخدم %s", member)
        if sent_any:
            _LAST_DEVIATION_SENT[anchor] = today


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

    يحترم report.view_all: من لا يملكها يرى مهامه وفواتيره فقط.
    """
    from app.database.crud import budget_usage, list_budgets
    from app.database.crud.company import report_company_filter
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

    tasks = (
        db.query(Task)
        .filter(
            report_company_filter(db, uid, Task),
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
            report_company_filter(db, uid, Invoice),
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
                def _digest(_uid=uid):  # noqa: B023
                    s = SessionLocal()
                    try:
                        return build_proactive_digest(s, _uid)
                    finally:
                        s.close()

                digest = await asyncio.to_thread(_digest)
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
    """نسخة احتياطية يومية دون حظر الحلقة."""
    from app.database.backup import _has_backup_for_today, run_backup

    try:
        has = await asyncio.to_thread(_has_backup_for_today)
        if has:
            logger.info("تخطي النسخ الاحتياطي اليومي — نسخة اليوم موجودة بالفعل")
            return
        await asyncio.to_thread(run_backup)
    except Exception:
        logger.exception("فشل النسخ الاحتياطي اليومي")


def setup_daily_backup(app: Application) -> None:
    """يُسجّل دورة يومية للنسخ الاحتياطي (مرة يوميًا في 02:05، لا بعد كل إعادة تشغيل بساعة)."""
    if app.job_queue is None:
        logger.warning("job_queue غير مُفعّل — النسخ الاحتياطي اليومي لن يعمل.")
        return
    # احسب التأخير حتى 02:05 القادمة بدل أول نسخة بعد ساعة من كل تشغيل
    from app.timeutil import now_local

    now = now_local()
    target = now.replace(hour=2, minute=5, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    first_delay = target - now
    app.job_queue.run_repeating(
        daily_backup_job,
        interval=timedelta(hours=BACKUP_INTERVAL_HOURS),
        first=first_delay,
        name="daily_backup",
    )
    logger.info("تم تسجيل النسخ الاحتياطي اليومي (يوميًا 02:05، كل %d ساعة)", BACKUP_INTERVAL_HOURS)


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
        parts.append(f"آخر موعد الساعة {times[-1]}")

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
