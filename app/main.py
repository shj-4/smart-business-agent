"""
FastAPI تطبيق منفصل عن البوت (bot.py).

يعمل كـ health/status API + لوحة تحكم ويب (<div>/dashboard).
يُشغَّل كخدمة مستقلة (SmartBotAPI) على المنفذ 8000 عبر start_api.cmd /
setup_services.ps1. لوحة التحكم محتاجة ملفات قوالب في templates/.

لا علاقة له بمنطق البوت (polling عبر bot.py) ولا يستقبل Updates من Telegram.
"""

import base64
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import setup_audit_log
from app.cache import start_sweeper
from app.config import TELEGRAM_BOT_TOKEN, settings
from app.database.db import engine, get_db
from app.database.models import Budget, Note, Task, Transaction
from app.sentry import install_sentry
from app.timeutil import to_local_naive

setup_audit_log()
start_sweeper()
install_sentry()

app = FastAPI(
    title="Smart Business Agent API",
    description=(
        "Health/status API + لوحة تحكم ويب. منفصل عن bot.py (polling). "
        "لا يقبل Updates من Telegram حاليًا."
    ),
)

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
)

PERIOD_NAMES = {
    "all_time": "منذ البداية",
    "this_month": "هذا الشهر",
    "this_week": "هذا الأسبوع",
    "today": "اليوم",
}

logger = logging.getLogger("app.dashboard")

_DASHBOARD_USERNAME = settings.dashboard_username or "admin"
_DASHBOARD_PASSWORD = settings.dashboard_password
if not _DASHBOARD_PASSWORD:
    _DASHBOARD_PASSWORD = secrets.token_urlsafe(18)
    logger.warning(
        "DASHBOARD_PASSWORD غير مضبوط في .env — حُدِّدت كلمة مرور مؤقتة للوحة/API: %s "
        "(ضع DASHBOARD_PASSWORD في .env لثباتها عبر عمليات إعادة التشغيل، والمستخدم الافتراضي: %s)",
        _DASHBOARD_PASSWORD,
        _DASHBOARD_USERNAME,
    )


def _basic_auth_ok(authorization: str) -> bool:
    """يتحقق من ترويسة Basic Auth (مقارنة ثابتة بلا ثغرة timing)."""
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        credentials = base64.b64decode(authorization[6:]).decode("utf-8", "surrogateescape")
    except Exception:
        return False
    user, sep, password = credentials.partition(":")
    if not sep:
        return False
    return secrets.compare_digest(user, _DASHBOARD_USERNAME) and secrets.compare_digest(
        password, _DASHBOARD_PASSWORD
    )


def require_dashboard_auth(request: Request) -> None:
    """مصادقة أساسية لكل /dashboard/* و /api/* — يمنع وصول أي شخص للمنفذ 8000
    إلى البيانات المالية دون تسجيل دخول (حتى عبر reverse proxy مستقبلاً)."""
    if not _basic_auth_ok(request.headers.get("Authorization", "")):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Smart Business Agent"'},
        )


def _period_start(period: str) -> datetime:
    """حدود بداية الفترة بالتوقيت المحلي محوّلًا إلى UTC naive."""
    from app.timeutil import now_local, to_utc_naive

    local_now = now_local()
    if period == "today":
        local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_week":
        from app.timeutil import first_day_of_week

        fd = first_day_of_week()
        weekday = local_now.weekday()
        local = local_now - timedelta(days=(weekday - fd) % 7)
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "this_month":
        local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return None
    return to_utc_naive(local)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Smart Business Agent API (health check + dashboard)",
        "endpoints": ["/health", "/status", "/dashboard"],
    }


@app.get("/health")
async def health(db: Session = Depends(get_db)):
    """فحص حقيقي: اتصال قاعدة البيانات + صحة الجداول الأساسية."""
    db_ok = False
    try:
        db.query(func.count(Transaction.id)).scalar()
        db_ok = True
    except Exception:
        pass

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
    }


@app.get("/status")
async def status(db: Session = Depends(get_db)):
    """تقرير تشغيلي: فحص اتصال قاعدة البيانات ومؤشرات أساسية سليمة."""
    db_ok = True
    try:
        db.query(func.count(Transaction.id)).scalar()
    except Exception:
        db_ok = False

    now = datetime.now(UTC)

    return {
        "status": "ok" if db_ok else "degraded",
        "checked_at": now.isoformat(),
        "database": {
            "connected": db_ok,
            "file": os.path.basename(str(engine.url).replace("sqlite:///", "")),
        },
        "config": {
            "telegram_token_set": bool(TELEGRAM_BOT_TOKEN),
        },
        "counts": {
            "transactions": db.query(func.count(Transaction.id)).scalar() if db_ok else None,
            "tasks": db.query(func.count(Task.id)).scalar() if db_ok else None,
            "notes": db.query(func.count(Note.id)).scalar() if db_ok else None,
        },
    }


# ---------- لوحة التحكم ----------

TRANSACTION_COLUMNS = [
    {"label": "التاريخ", "key": "date", "cls": "time"},
    {"label": "النوع", "key": "type_label", "badge": "type_cls"},
    {"label": "المبلغ", "key": "amount"},
    {"label": "العملة", "key": "currency"},
    {"label": "الشخص", "key": "person"},
    {"label": "التصنيف", "key": "category"},
    {"label": "الوصف", "key": "description"},
]

TASK_COLUMNS = [
    {"label": "الوصف", "key": "description"},
    {"label": "الشخص", "key": "person"},
    {"label": "الموعد", "key": "due_date", "cls": "time"},
    {"label": "الحالة", "key": "status_label", "badge": "status_cls"},
]

NOTE_COLUMNS = [
    {"label": "التاريخ", "key": "date", "cls": "time"},
    {"label": "النوع", "key": "type_label"},
    {"label": "الوصف", "key": "description"},
    {"label": "الشخص", "key": "person"},
    {"label": "التصنيف", "key": "category"},
]


def _recent_transactions(db: Session, limit: int = 10):
    rows = (
        db.query(Transaction)
        .filter(Transaction.deleted_at.is_(None))
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for r in rows:
        local_dt = to_local_naive(r.created_at)
        out.append(
            {
                "date": local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else "",
                "type": r.type,
                "amount": float(r.amount) if r.amount else 0,
                "currency": r.currency or "",
                "person": r.person or "",
                "category": r.category or "",
                "description": r.description or "",
            }
        )
    return out


def _recent_tasks(db: Session, limit: int = 50):
    rows = (
        db.query(Task)
        .filter(Task.deleted_at.is_(None))
        .order_by(Task.due_date.asc().nulls_last())
        .limit(limit)
        .all()
    )

    out = []
    for r in rows:
        due_str = to_local_naive(r.due_date).strftime("%Y-%m-%d %H:%M") if r.due_date else ""
        out.append(
            {
                "id": r.id,
                "description": r.description or "",
                "person": r.person or "",
                "due_date": due_str,
                "status": r.status,
            }
        )
    return out


def _recent_notes(db: Session, limit: int = 10):
    rows = (
        db.query(Note)
        .filter(Note.deleted_at.is_(None))
        .order_by(Note.created_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for n in rows:
        local_dt = to_local_naive(n.created_at)
        out.append(
            {
                "date": local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else "",
                "note_type": n.note_type,
                "description": n.description or "",
                "person": n.person or "",
                "category": n.category or "",
            }
        )
    return out


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """لوحة التحكم الرئيسية."""
    counts = {
        "transactions": db.query(func.count(Transaction.id))
        .filter(Transaction.deleted_at.is_(None))
        .scalar(),
        "tasks": db.query(func.count(Task.id)).filter(Task.deleted_at.is_(None)).scalar(),
        "notes": db.query(func.count(Note.id)).filter(Note.deleted_at.is_(None)).scalar(),
        "users": db.query(Transaction.telegram_user_id).distinct().count(),
    }

    overdue_tasks = [t for t in _recent_tasks(db, limit=100) if t["status"] == "overdue"]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "counts": counts,
            "transactions": _recent_transactions(db, limit=10),
            "notes": _recent_notes(db, limit=10),
            "overdue_tasks": overdue_tasks,
        },
    )


@app.get("/dashboard/transactions", response_class=HTMLResponse)
async def dashboard_transactions(
    request: Request,
    period: str = "all_time",
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """جدول المعاملات المالية مع تصفية بالحالة/الفترة."""
    q = db.query(Transaction).filter(Transaction.deleted_at.is_(None))
    start = _period_start(period)
    if start:
        q = q.filter(Transaction.created_at >= start)
    rows = q.order_by(Transaction.created_at.desc()).limit(200).all()

    entity_rows = []
    for r in rows:
        local_dt = to_local_naive(r.created_at)
        entity_rows.append(
            {
                "date": local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else "",
                "type_label": "إيراد" if r.type == "income" else "مصروف",
                "type_cls": "income" if r.type == "income" else "expense",
                "amount": r.amount,
                "currency": r.currency or "",
                "person": r.person or "",
                "category": r.category or "",
                "description": r.description or "",
            }
        )

    return templates.TemplateResponse(
        request,
        "table_list.html",
        {
            "title": "المعاملات المالية",
            "rows": entity_rows,
            "columns": TRANSACTION_COLUMNS,
            "period": period,
            "periods": PERIOD_NAMES,
        },
    )


@app.get("/dashboard/tasks", response_class=HTMLResponse)
async def dashboard_tasks(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """جدول المهام مع حالة كل مهمة."""
    from app.database.crud import mark_overdue_tasks, workspace_for_user

    # تحديث المهام المتأخرة (كل المستخدمين) — مرة واحدة لكل مساحة مشتركة
    # (mark_overdue_tasks يحدّث كل أعضاء accessible_user_ids، فالتكرار على كل
    # عضوٍ في مساحة من N أعضاء يُنفّذ نفس التحديث N مرات بلا داعٍ).
    try:
        db.query(func.count(Task.id)).scalar()  # التأكد من اتصال
        seen = set()
        for (uid,) in db.query(Task.telegram_user_id).distinct().all():
            wid = workspace_for_user(db, uid)
            anchor = wid if wid is not None else uid
            if anchor in seen:
                continue
            seen.add(anchor)
            mark_overdue_tasks(db, anchor)
    except Exception:
        pass

    rows = _recent_tasks(db, limit=200)

    status_map = {
        "pending": ("قيد الانتظار", "pending"),
        "overdue": ("متأخرة", "overdue"),
        "done": ("مكتملة", "done"),
    }

    entity_rows = []
    for t in rows:
        label, cls = status_map.get(t["status"], (t["status"], t["status"]))
        entity_rows.append(
            {
                "description": t["description"],
                "person": t["person"],
                "due_date": t["due_date"],
                "status_label": label,
                "status_cls": cls,
            }
        )

    return templates.TemplateResponse(
        request,
        "table_list.html",
        {
            "title": "المهام",
            "rows": entity_rows,
            "columns": TASK_COLUMNS,
            "period": None,
            "periods": PERIOD_NAMES,
        },
    )


@app.get("/dashboard/notes", response_class=HTMLResponse)
async def dashboard_notes(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """جدول الطلبيات والملاحظات."""
    rows = _recent_notes(db, limit=200)

    entity_rows = []
    for n in rows:
        entity_rows.append(
            {
                "date": n["date"],
                "type_label": "طلبية" if n["note_type"] == "order" else "ملاحظة",
                "description": n["description"],
                "person": n["person"],
                "category": n["category"],
            }
        )

    return templates.TemplateResponse(
        request,
        "table_list.html",
        {
            "title": "الطلبيات والملاحظات",
            "rows": entity_rows,
            "columns": NOTE_COLUMNS,
            "period": None,
            "periods": PERIOD_NAMES,
        },
    )


# ---------- الميزانيات (لوحة التحكم + JSON) ----------


def _budget_rows(db: Session) -> list[dict]:
    from app.database.crud import budget_monthly_reset, budget_usage
    from app.exchange import CURRENCY_NAMES

    rows = []
    budgets = db.query(Budget).order_by(Budget.created_at.asc()).all()
    for b in budgets:
        budget_monthly_reset(db, b)
        usage = budget_usage(db, b)
        if b.scope == "currency":
            target = CURRENCY_NAMES.get(b.currency, b.currency)
        else:
            target = f"الشخص {b.person}"
        rows.append(
            {
                "id": b.id,
                "scope": b.scope,
                "target": target,
                "spent": float(usage["spent"]),
                "limit": float(usage["limit"]),
                "percent": usage["percent"],
                "over": usage["over"],
            }
        )
    return rows


@app.get("/dashboard/budgets", response_class=HTMLResponse)
async def dashboard_budgets(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """عرض الميزانيات الشهرية وحالة كل منها."""
    budgets = _budget_rows(db)
    return templates.TemplateResponse(
        request,
        "budgets.html",
        {"budgets": budgets},
    )


@app.get("/api/budgets")
async def api_budgets(
    db: Session = Depends(get_db), _auth: None = Depends(require_dashboard_auth)
):
    """الميزانيات الشهرية بصيغة JSON."""
    return _budget_rows(db)


# ---------- APIs للبيانات (JSON) ----------


@app.get("/api/transactions")
async def api_transactions(
    period: str = "all_time",
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """معاملات مالية بصيغة JSON (أحدث 500)."""
    q = db.query(Transaction).filter(Transaction.deleted_at.is_(None))
    start = _period_start(period)
    if start:
        q = q.filter(Transaction.created_at >= start)
    rows = q.order_by(Transaction.created_at.desc()).limit(500).all()
    return [
        {
            "id": r.id,
            "date": (
                to_local_naive(r.created_at).strftime("%Y-%m-%d %H:%M") if r.created_at else None
            ),
            "type": r.type,
            "amount": float(r.amount) if r.amount else None,
            "currency": r.currency,
            "person": r.person,
            "category": r.category,
            "description": r.description,
        }
        for r in rows
    ]


@app.get("/api/tasks")
async def api_tasks(
    db: Session = Depends(get_db), _auth: None = Depends(require_dashboard_auth)
):
    """المهام بصيغة JSON."""
    rows = _recent_tasks(db, limit=500)
    return rows
