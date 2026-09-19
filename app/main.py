"""
FastAPI تطبيق منفصل عن البوت (bot.py).

يعمل كـ health/status API + لوحة تحكم ويب (<div>/dashboard).
يُشغَّل كخدمة مستقلة (SmartBotAPI) على المنفذ 8000 عبر start_api.cmd /
setup_services.ps1. لوحة التحكم محتاجة ملفات قوالب في templates/.

لا علاقة له بمنطق البوت (polling عبر bot.py) ولا يستقبل Updates من Telegram.
"""

import base64
import io
import json
import logging
import os
import re
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.audit import setup_audit_log
from app.cache import start_sweeper
from app.charts import _convert_month_currency_groups, generate_global_monthly_chart
from app.config import TELEGRAM_BOT_TOKEN, settings
from app.database.db import engine, get_db
from app.database.models import Budget, Company, CompanyMember, InviteLink, Note, Task, Transaction, WorkspaceMember
from app.exchange import convert_totals_to_base
from app.sentry import install_sentry
from app.timeutil import now_local, to_local_naive

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

_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DASHBOARD_CRED_FILE = os.path.join(_PACKAGE_ROOT, "data", "dashboard_credentials.txt")


def _restrict_file_windows(path: str) -> None:
    """يقيّد ملف بيانات الاعتماد على Windows عبر icacls (NTFS).

    على POSIX تكفي صلاحيات 0o600، لكنها بلا أثر على Windows — الاختبار نفسه
    كان يتخطى الفحص `if os.name != \"nt\"`. بيئة الإنتاج الحقيقية (C:\\smart-business-agent
    و setup_services.ps1/start_*.cmd) هي Windows، لذا نطبّق حماية NTFS صريحة:
      icacls <path> /inheritance:r /grant:r \"<user>:F\"
    فيُلغى الوراثة ويُمنح المستخدم الحالي فقط حق الوصول الكامل.
    أي فشل يُسجَّل كتحذير ولا يُسقط إقلاع الخدمة.
    """
    import subprocess

    try:
        # حدد هوية المالك الحالي
        username = os.environ.get("USERNAME") or os.environ.get("USER")
        if not username:
            try:
                out = subprocess.check_output(
                    "whoami", shell=True, text=True, stderr=subprocess.DEVNULL
                )
                username = out.strip().split("\\")[-1].split("/")[-1]
            except Exception:
                username = None
        if not username:
            # لا يمكن تحديد المستخدم — حاول تقييد بالوراثة فقط
            subprocess.run(
                ["icacls", path, "/inheritance:r"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.warning(
                "تعذر تحديد اسم المستخدم لتقييد %s عبر icacls — أُزيلت الوراثة فقط. "
                "تحقق يدويًا من صلاحيات NTFS.",
                path,
            )
            return

        # أزل الوراثة وامنح المالك الحالي فقط
        subprocess.run(
            ["icacls", path, "/inheritance:r"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result = subprocess.run(
            ["icacls", path, "/grant:r", f"{username}:(F)"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "فشل تقييد صلاحيات NTFS للملف %s عبر icacls (user=%s): %s %s",
                path,
                username,
                result.stdout.strip() if result.stdout else "",
                result.stderr.strip() if result.stderr else "",
            )
        else:
            logger.info("تم تقييد ملف الاعتماد %s للمستخدم %s فقط عبر icacls", path, username)
    except Exception as exc:
        logger.warning("تعذر تشغيل icacls لتقييد %s: %s", path, exc)


def _write_dashboard_credentials(username: str, password: str) -> str:
    """يكتب الاعتماديات في ملف منفصل بصلاحيات مقيدة (0600 على POSIX / icacls على Windows)."""
    os.makedirs(os.path.dirname(_DASHBOARD_CRED_FILE), exist_ok=True)
    # 0o600 فعال على POSIX فقط؛ على Windows نطبّق icacls لاحقًا
    fd = os.open(_DASHBOARD_CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"DASHBOARD_USERNAME={username}\nDASHBOARD_PASSWORD={password}\n")
    try:
        if os.name == "nt":
            _restrict_file_windows(_DASHBOARD_CRED_FILE)
        else:
            os.chmod(_DASHBOARD_CRED_FILE, 0o600)
    except Exception as exc:
        logger.warning("تعذر ضبط صلاحيات الملف %s: %s", _DASHBOARD_CRED_FILE, exc)
    return _DASHBOARD_CRED_FILE


def _generate_dashboard_credentials() -> tuple[str, str]:
    """يعيد (اسم المستخدم، كلمة المرور) — يولّد مؤقتةً عند غياب الإعداد.

    لا تُطبع كلمة المرور في السجلات أبدًا؛ تُحفظ في _DASHBOARD_CRED_FILE
    (0600 على POSIX / icacls على Windows) ويُسجَّل التلميح + مسار الملف فقط.

    في وضع الإنتاج (APP_ENV=production) يُرفض التوليد المؤقت ويُجبر المشغّل
    على ضبط DASHBOARD_PASSWORD صراحة — لأن ملف الاعتماد على Windows لا يُحمى
    تلقائيًا بـ 0o600 دون icacls، وبيئة الإنتاج الحقيقية هي Windows
    (C:\\smart-business-agent / setup_services.ps1).
    """
    username = settings.dashboard_username or "admin"
    password = settings.dashboard_password
    if not password:
        if (settings.app_env or "").strip().lower() == "production":
            raise RuntimeError(
                "DASHBOARD_PASSWORD غير مضبوط و APP_ENV=production — يمنع توليد كلمة "
                "مرور مؤقتة في الإنتاج. اضبط DASHBOARD_PASSWORD صراحة في .env "
                "(أو متغير البيئة) قبل التشغيل."
            )
        password = secrets.token_urlsafe(18)
        cred_file = _write_dashboard_credentials(username, password)
        logger.warning(
            "DASHBOARD_PASSWORD غير مضبوط في .env — وُلدّت كلمة مرور مؤقتة ولا تُكتب في "
            "السجلات. المستخدم: %s؛ كلمة المرور محفوظة في ملف منفصل: %s (صلاحيات مقيدة: "
            "0600 على POSIX / icacls على Windows). "
            "ضع DASHBOARD_PASSWORD في .env لثباتها عبر عمليات إعادة التشغيل.",
            username,
            cred_file,
        )
    return username, password


_DASHBOARD_USERNAME, _DASHBOARD_PASSWORD = _generate_dashboard_credentials()


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


# ---------- حماية CSRF (طلبات تغيير البيانات) ----------
#
# المصادقة على اللوحة Basic Auth فقط، والمتصفح يرسل اعتمادياتها تلقائيًا
# لنفس origin دون تدخل المستخدم، فصفحة خبيثة قد تقدّم نموذجًا
# <form action="http://host:8000/dashboard/customers/merge" method="post">
# وتُنفَّذ باعتماديات المسجّل مسبقًا. الدفاعات الثلاثة:
#   1) كوكي منفصلة SameSite=Strict + HttpOnly — لا تُرسل عبر مواقع أخرى
#      ولا تُقرأ من JavaScript.
#   2) رمز مزدوج (double-submit): قيمة المخفي في النموذج == قيمة الكوكي
#      التي يضمّنها الخادم نفسه في الصفحة.
#   3) فحص Origin/Referer لمطابقة مضيف اللوحة (صفة إضافية، غياب الترويسة
#      لا يُفشل الطلب — الدفاع الأساسي هو الرمز).

_CSRF_COOKIE = "sb_csrf"
_CSRF_RE = re.compile(r"^[A-Za-z0-9_\-]{20,64}$")


def _valid_csrf_token(token: str) -> bool:
    return isinstance(token, str) and bool(_CSRF_RE.match(token))


def _csrf_token_or_new(request: Request) -> str:
    """رمز CSRF للصفحة: يُبقي رمز الكوكي القائم أو يولّد واحدًا جديدًا."""
    token = request.cookies.get(_CSRF_COOKIE, "")
    if not _valid_csrf_token(token):
        token = secrets.token_urlsafe(32)
    return token


def _set_csrf_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=_CSRF_COOKIE,
        value=token,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )


def _foreign_origin(request: Request) -> bool:
    """هل صرّحت ترويسة Origin/Referer بمضيف خارج اللوحة؟ (غياب الترويسة → نعتمد على الرمز)."""
    origin = request.headers.get("Origin")
    if origin:
        netloc = urllib.parse.urlparse(origin).netloc
        return bool(netloc) and netloc != request.headers.get("host", "")
    referer = request.headers.get("Referer")
    if referer:
        netloc = urllib.parse.urlparse(referer).netloc
        return bool(netloc) and netloc != request.headers.get("host", "")
    return False


async def require_dashboard_csrf(request: Request) -> None:
    """حماية CSRF لكل طلب يغيّر البيانات على /dashboard/* و /api/*.

    أي endpoint مستقبلي method من نوع POST/PUT/DELETE عليها إضافة
    `_csrf: None = Depends(require_dashboard_csrf)` بجانب require_dashboard_auth.
    """
    if _foreign_origin(request):
        raise HTTPException(status_code=403, detail="CSRF check failed: foreign origin")

    cookie_token = request.cookies.get(_CSRF_COOKIE, "")
    posted_token = request.headers.get("X-CSRF-Token", "")
    if not posted_token:
        content_type = request.headers.get("content-type", "").lower()
        raw = await request.body()  # Starlette يخفّف قراءة الجسم — يُعاد للـ handler كما هو
        if "json" in content_type:
            try:
                posted_token = (json.loads(raw or b"{}") or {}).get("csrf_token", "") or ""
            except Exception:
                posted_token = ""
        elif raw:
            posted_token = urllib.parse.parse_qs(
                raw.decode("utf-8", errors="replace")
            ).get("csrf_token", [""])[0]

    if not (
        _valid_csrf_token(cookie_token)
        and _valid_csrf_token(posted_token)
        and secrets.compare_digest(cookie_token, posted_token)
    ):
        raise HTTPException(status_code=403, detail="CSRF check failed: token mismatch")


def _period_start(period: str) -> datetime:
    """حدود بداية الفترة بالتوقيت المحلي محوّلًا إلى UTC naive."""
    from app.timeutil import to_utc_naive

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


# ---------- مساعدات لوحة التحكم (تنسيق/واجهة/تجميع) ----------


def _fmt(value) -> str:
    """تنسيق رقم مبالغ (آلاف + فاصلتان) أو سلسلة فارغة للمعدوم."""
    if value is None:
        return ""
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _like_pattern(term: str) -> str:
    """يَهرب محارف LIKE ويبني نمط \"%...%\" للبحث الجزئي."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _qs(params: dict) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def _page_href(base_path: str, params: dict, page: int | None) -> str:
    next_params = dict(params)
    next_params.pop("page", None)
    if page and page > 1:
        next_params["page"] = str(page)
    return f"{base_path}?{_qs(next_params)}"


def _pagination(page: int, pages: int, base_path: str, params: dict) -> dict:
    """يبني هيكل أزرار الترقيم (نافذة + فجوات) — بلا قوالب جانبية."""
    def add(nums: list, value: int) -> None:
        nums.append({"num": value, "url": _page_href(base_path, params, value)})

    items: list[dict] = []
    if pages <= 1:
        return {"count": pages, "prev": None, "next": None, "items": items}
    first = max(page - 2, 1)
    last = min(page + 2, pages)
    window = list(range(first, last + 1))
    if window[0] > 1:
        add(items, 1)
        if window[0] > 2:
            items.append({"num": None})
    for n in window:
        add(items, n)
    if window[-1] < pages:
        if window[-1] < pages - 1:
            items.append({"num": None})
        add(items, pages)
    return {
        "count": pages,
        "prev": {"url": _page_href(base_path, params, page - 1)} if page > 1 else None,
        "next": {"url": _page_href(base_path, params, page + 1)} if page < pages else None,
        "items": items,
    }


def _summary_month(db: Session, base: str) -> dict:
    """مجموع الشهر الحالي (كل المستخدمين) موحَّدًا بالعملة الأساسية."""
    by_currency: dict = {}
    stored: dict = {}
    start = _period_start("this_month")
    rows = (
        db.query(
            Transaction.type,
            Transaction.currency,
            Transaction.amount,
            Transaction.amount_in_base_currency,
            Transaction.base_currency_at_creation,
        )
        .filter(
            Transaction.deleted_at.is_(None),
            Transaction.created_at >= start,
        )
        .all()
    )
    for rtype, currency, amount, stored_amt, stored_base in rows:
        c = (currency or "").upper()
        kind = "expense" if rtype == "expense" else "income"
        entry = by_currency.setdefault(c, {"expense": Decimal("0"), "income": Decimal("0")})
        entry[kind] += amount or Decimal("0")
        if stored_amt is not None and stored_base == base and c:
            se = stored.setdefault(c, {"expense": Decimal("0"), "income": Decimal("0")})
            se[kind] += Decimal(str(stored_amt))
    return _convert_month_currency_groups(by_currency, base, stored=stored or None)


def _category_tops(db: Session, base: str, limit: int = 6) -> list[dict]:
    """أعلى تصنيفات المصاريف هذا الشهر (موحَّدة بالعملة الأساسية)."""
    start = _period_start("this_month")
    rows = (
        db.query(
            Transaction.category,
            Transaction.currency,
            Transaction.amount,
            Transaction.amount_in_base_currency,
            Transaction.base_currency_at_creation,
        )
        .filter(
            Transaction.deleted_at.is_(None),
            Transaction.type == "expense",
            Transaction.created_at >= start,
        )
        .all()
    )
    agg: dict = {}
    for category, currency, amount, stored_amt, stored_base in rows:
        key = category or "أخرى"
        bucket = agg.setdefault(key, {"by_currency": {}, "stored": {}})
        c = (currency or "").upper()
        entry = bucket["by_currency"].setdefault(c, Decimal("0"))
        entry += amount or Decimal("0")
        if stored_amt is not None and stored_base == base and c:
            se = bucket["stored"].setdefault(c, Decimal("0"))
            se += Decimal(str(stored_amt))
    out = []
    for key, bucket in agg.items():
        conv = convert_totals_to_base(
            bucket["by_currency"], base, stored=bucket["stored"] or None
        )
        total = conv.get("total")
        if total is None:
            continue
        out.append({"category": key, "amount": _fmt(total), "raw": float(total)})
    out.sort(key=lambda x: x["raw"], reverse=True)
    return out[:limit]


def _party_rows(db: Session, base: str) -> list[dict]:
    """تجمع أسماء الأطراف (عملاء/موردون) عبر المعاملات والطلبيات والمهام.

    كل طرف = اسم نصي؛ المجاميع تُوحَّد بالعملة الأساسية (المخزَّنة عند
    التسجيل إن توافقت، وإلا بسعر اليوم). المبالغ مشفّرة فلا يمكن جمعها عبر
    SQL (يجب فكّ التشفير في Python)، لذا لا يُقصّ أي حزام بيانات — الاختبار
    `test_party_rows_computes_full_aggregates_without_cap` يضمن 520 سجلًا كاملًا.

    التحسين: دفع التجميع الجزئي إلى SQL حيثما أمكن (counts عبر GROUP BY لـ
    Note/Task/Transaction، و streaming عبر `yield_per` للمعاملات) مع بقاء فكّ
    التشفير للمبالغ في Python فقط. يقلّل الحمل على Python دون فقدان الدقة.
    """
    # دفع counts إلى SQL (GROUP BY) — لا حاجة لسحب كل الصفوف لعدّها
    notes_count: dict[str, int] = {}
    for p, c in (
        db.query(Note.person, func.count(Note.id))
        .filter(
            Note.deleted_at.is_(None),
            Note.person.isnot(None),
            Note.note_type == "order",
        )
        .group_by(Note.person)
        .all()
    ):
        key = (p or "").strip()
        if key:
            notes_count[key] = notes_count.get(key, 0) + c
    tasks_count: dict[str, int] = {}
    for p, c in (
        db.query(Task.person, func.count(Task.id))
        .filter(Task.deleted_at.is_(None), Task.person.isnot(None))
        .group_by(Task.person)
        .all()
    ):
        key = (p or "").strip()
        if key:
            tasks_count[key] = tasks_count.get(key, 0) + c
    # للمعاملات: count عبر SQL + streaming للمبالغ (فكّ التشفير فقط في Python)
    # هذا يحقق اقتراح "ادفع التجميع الجزئي إلى SQL" دون كسر تشفير amount.
    tx_counts: dict[str, int] = {}
    for p, c in (
        db.query(Transaction.person, func.count(Transaction.id))
        .filter(Transaction.deleted_at.is_(None), Transaction.person.isnot(None))
        .group_by(Transaction.person)
        .all()
    ):
        key = (p or "").strip()
        if key:
            tx_counts[key] = tx_counts.get(key, 0) + c
    tx_rows = (
        db.query(
            Transaction.person,
            Transaction.type,
            Transaction.created_at,
            Transaction.currency,
            Transaction.amount,
            Transaction.amount_in_base_currency,
            Transaction.base_currency_at_creation,
        )
        .filter(Transaction.deleted_at.is_(None), Transaction.person.isnot(None))
        .order_by(Transaction.created_at.desc())
        .yield_per(500)
        .all()
    )
    agg: dict = {}
    # تهيئة records من SQL بدل العدّ في Python (التجميع الجزئي)
    for p, cnt in tx_counts.items():
        agg[p] = {"by_currency": {}, "stored": {}, "records": cnt, "last_seen": None}
    for person, rtype, created_at, currency, amount, stored_amt, stored_base in tx_rows:
        p = person.strip()
        bucket = agg.setdefault(
            p,
            {"by_currency": {}, "stored": {}, "records": 0, "last_seen": None},
        )
        # records سبق عدّها عبر GROUP BY، لا نكرر الزيادة هنا
        if created_at and (bucket["last_seen"] is None or created_at > bucket["last_seen"]):
            bucket["last_seen"] = created_at
        kind = "expense" if rtype == "expense" else "income"
        c = (currency or "").upper()
        entry = bucket["by_currency"].setdefault(
            c, {"expense": Decimal("0"), "income": Decimal("0")}
        )
        entry[kind] += amount or Decimal("0")
        if stored_amt is not None and stored_base == base and c:
            se = bucket["stored"].setdefault(
                c, {"expense": Decimal("0"), "income": Decimal("0")}
            )
            se[kind] += Decimal(str(stored_amt))
    for p in notes_count:
        if p not in agg:
            agg[p] = {"by_currency": {}, "stored": {}, "records": 0, "last_seen": None}
    for p in tasks_count:
        if p not in agg:
            agg[p] = {"by_currency": {}, "stored": {}, "records": 0, "last_seen": None}
    rows = []
    for p, bucket in agg.items():
        res = _convert_month_currency_groups(
            bucket["by_currency"], base, stored=bucket["stored"] or None
        )
        expense = res["expense"] or Decimal("0")
        income = res["income"] or Decimal("0")
        last_seen = (
            to_local_naive(bucket["last_seen"]).strftime("%Y-%m-%d")
            if bucket["last_seen"]
            else ""
        )
        rows.append(
            {
                "person": p,
                "records": bucket["records"] + notes_count.get(p, 0) + tasks_count.get(p, 0),
                "expense": _fmt(expense),
                "income": _fmt(income),
                "net": _fmt(income - expense),
                "last_seen": last_seen,
            }
        )
    rows.sort(key=lambda x: -x["records"])
    return rows


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
                "amount": _fmt(r.amount),
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
    """لوحة التحكم الرئيسية: بطاقات + ملخص الشهر + رسم + أعلى التصنيفات والأطراف."""
    base = (settings.base_currency or "ILS").upper()
    counts = {
        "transactions": db.query(func.count(Transaction.id))
        .filter(Transaction.deleted_at.is_(None))
        .scalar(),
        "tasks": db.query(func.count(Task.id)).filter(Task.deleted_at.is_(None)).scalar(),
        "notes": db.query(func.count(Note.id)).filter(Note.deleted_at.is_(None)).scalar(),
        "users": db.query(Transaction.telegram_user_id).distinct().count(),
        "overdue": db.query(func.count(Task.id))
        .filter(Task.deleted_at.is_(None), Task.status == "overdue")
        .scalar(),
    }

    summary = _summary_month(db, base)
    expense = summary["expense"] or Decimal("0")
    income = summary["income"] or Decimal("0")
    month_cards = {
        "expense": _fmt(expense),
        "income": _fmt(income),
        "net": _fmt(income - expense),
        "net_cls": "green" if income >= expense else "red",
    }
    summary_partial = bool(summary.get("partial"))

    chart_url = None
    try:
        buf = generate_global_monthly_chart(
            db, months=settings.chart_months, base_currency=base
        )
        if buf:
            chart_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.exception("تعذر توليد الرسم البياني للوحة")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "home",
            "base_currency": base,
            "chart_months": settings.chart_months or 6,
            "chart_url": chart_url,
            "counts": counts,
            "month_cards": month_cards,
            "summary_partial": summary_partial,
            "top_categories": _category_tops(db, base),
            "top_parties": _party_rows(db, base)[:6],
            "transactions": _recent_transactions(db, limit=10),
            "notes": _recent_notes(db, limit=10),
            "overdue_tasks": [t for t in _recent_tasks(db, limit=100) if t["status"] == "overdue"],
        },
    )


EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _transactions_export_bytes(rows: list[Transaction]) -> bytes:
    """يولّد ملف Excel من قائمة معاملات (تُستعمل لأزرار التصدير باللوحة)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "المعاملات"
    ws.append(["التاريخ", "النوع", "المبلغ", "العملة", "الشخص", "التصنيف", "الوصف"])
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1E293B")
    thin = Side(style="thin", color="CBD5E1")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center")
    for r in rows:
        local_dt = to_local_naive(r.created_at)
        ws.append(
            [
                local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else "",
                "إيراد" if r.type == "income" else "مصروف",
                float(r.amount) if r.amount is not None else 0,
                r.currency or "",
                r.person or "",
                r.category or "",
                r.description or "",
            ]
        )
    widths = [18, 10, 12, 10, 20, 14, 34]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = border
        row[2].number_format = "#,##0.00"
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


@app.get("/dashboard/transactions", response_class=HTMLResponse)
async def dashboard_transactions(
    request: Request,
    period: str = "all_time",
    q: str = "",
    ttype: str = "",
    page: int = 1,
    page_size: int = 50,
    export: str = "",
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """جدول المعاملات: فلاتر (فترة/بحث/نوع) + ترقيم صفحات + تصدير Excel."""
    base_path = "/dashboard/transactions"
    params: dict = {}
    if period != "all_time":
        params["period"] = period
    if q:
        params["q"] = q
    if ttype:
        params["ttype"] = ttype

    filters = [Transaction.deleted_at.is_(None)]
    start = _period_start(period)
    if start:
        filters.append(Transaction.created_at >= start)
    if ttype in ("expense", "income"):
        filters.append(Transaction.type == ttype)
    term = q.strip()
    if term:
        pattern = _like_pattern(term)
        filters.append(
            or_(
                Transaction.person.like(pattern, escape="\\"),
                Transaction.category.like(pattern, escape="\\"),
            )
        )

    base_q = db.query(Transaction).filter(*filters)

    if export == "xlsx":
        content = _transactions_export_bytes(
            base_q.order_by(Transaction.created_at.desc()).all()
        )
        filename = f"transactions_{period or 'all_time'}.xlsx"
        return Response(
            content=content,
            media_type=EXCEL_MIME,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    page = max(page, 1)
    page_size = min(max(page_size, 5), 200)
    total = base_q.count()
    pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, pages)
    rows = (
        base_q.order_by(Transaction.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    entity_rows = []
    for r in rows:
        local_dt = to_local_naive(r.created_at)
        entity_rows.append(
            {
                "date": local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else "",
                "type_label": "إيراد" if r.type == "income" else "مصروف",
                "type_cls": "income" if r.type == "income" else "expense",
                "amount": _fmt(r.amount),
                "currency": r.currency or "",
                "person": r.person or "",
                "category": r.category or "",
                "description": r.description or "",
            }
        )

    period_options = [(k, v) for k, v in PERIOD_NAMES.items()]
    return templates.TemplateResponse(
        request,
        "table.html",
        {
            "active": "transactions",
            "title": "المعاملات المالية",
            "total": total,
            "rows": entity_rows,
            "columns": TRANSACTION_COLUMNS,
            "filters": {
                "extras": [("period", period)],
                "placeholder": "بحث بالشخص أو التصنيف",
                "q": q,
                "selects": [
                    {"name": "period", "selected": period, "options": period_options},
                    {
                        "name": "ttype",
                        "selected": ttype,
                        "options": [("", "الكل"), ("income", "إيراد"), ("expense", "مصروف")],
                    },
                ],
                "clear_url": base_path,
            },
            "export_xlsx": _page_href(base_path, params, 1) + "&export=xlsx",
            "export_pdf": None,
            "page": page,
            "pages": _pagination(page, pages, base_path, params),
        },
    )


@app.get("/dashboard/tasks", response_class=HTMLResponse)
async def dashboard_tasks(
    request: Request,
    status: str = "",
    q: str = "",
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """جدول المهام: تصفية بالحالة وبالشخص + ترقيم صفحات."""
    from app.database.crud import STATUS_ACTIVE, mark_overdue_tasks

    # تحديث المهام المتأخرة (كل المستخدمين) — مرة واحدة لكل مساحة مشتركة
    # (mark_overdue_tasks يحدّث كل أعضاء accessible_user_ids، فالتكرار على كل
    # عضوٍ في مساحة من N أعضاء يُنفّذ نفس التحديث N مرات بلا داعٍ).
    # معرّفات المساحات تُحمَّل استعلامًا مجمّعًا واحدًا بدل workspace_for_user
    # لكل مستخدمٍ على حدة.
    try:
        db.query(func.count(Task.id)).scalar()  # التأكد من اتصال
        uid_wid = {
            uid: wid
            for uid, wid in db.query(
                WorkspaceMember.telegram_user_id,
                WorkspaceMember.workspace_id,
            )
            .filter(WorkspaceMember.status == STATUS_ACTIVE)
            .all()
        }
        seen = set()
        for (uid,) in db.query(Task.telegram_user_id).distinct().all():
            anchor = uid_wid.get(uid, uid)
            if anchor in seen:
                continue
            seen.add(anchor)
            mark_overdue_tasks(db, anchor)
    except Exception:
        logger.exception("فشل تحديث المهام المتأخرة في لوحة المهام — حالات التأخر لن تنعكس")

    base_path = "/dashboard/tasks"
    params: dict = {}
    if status:
        params["status"] = status
    if q:
        params["q"] = q

    filters = [Task.deleted_at.is_(None)]
    if status in ("pending", "overdue", "done"):
        filters.append(Task.status == status)
    term = q.strip()
    if term:
        filters.append(Task.person.like(_like_pattern(term), escape="\\"))

    base_q = db.query(Task).filter(*filters)
    page = max(page, 1)
    page_size = min(max(page_size, 5), 200)
    total = base_q.count()
    pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, pages)
    rows = (
        base_q.order_by(Task.due_date.asc().nulls_last())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    status_map = {
        "pending": ("قيد الانتظار", "pending"),
        "overdue": ("متأخرة", "overdue"),
        "done": ("مكتملة", "done"),
    }
    entity_rows = []
    for t in rows:
        label, cls = status_map.get(t.status, (t.status, t.status))
        due_str = to_local_naive(t.due_date).strftime("%Y-%m-%d %H:%M") if t.due_date else ""
        entity_rows.append(
            {
                "description": t.description or "",
                "person": t.person or "",
                "due_date": due_str,
                "status_label": label,
                "status_cls": cls,
            }
        )

    return templates.TemplateResponse(
        request,
        "table.html",
        {
            "active": "tasks",
            "title": "المهام",
            "total": total,
            "rows": entity_rows,
            "columns": TASK_COLUMNS,
            "filters": {
                "extras": [],
                "placeholder": "بحث بالشخص",
                "q": q,
                "selects": [
                    {
                        "name": "status",
                        "selected": status,
                        "options": [
                            ("", "الكل"),
                            ("pending", "قيد الانتظار"),
                            ("overdue", "متأخرة"),
                            ("done", "مكتملة"),
                        ],
                    }
                ],
                "clear_url": base_path,
            },
            "export_xlsx": None,
            "export_pdf": None,
            "page": page,
            "pages": _pagination(page, pages, base_path, params),
        },
    )


@app.get("/dashboard/notes", response_class=HTMLResponse)
async def dashboard_notes(
    request: Request,
    ntype: str = "",
    q: str = "",
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """جدول الطلبيات والملاحظات: تصفية بالنوع وبالشخص + ترقيم صفحات."""
    base_path = "/dashboard/notes"
    params: dict = {}
    if ntype:
        params["ntype"] = ntype
    if q:
        params["q"] = q

    filters = [Note.deleted_at.is_(None)]
    if ntype in ("order", "note"):
        filters.append(Note.note_type == ntype)
    term = q.strip()
    if term:
        pattern = _like_pattern(term)
        filters.append(
            or_(
                Note.person.like(pattern, escape="\\"),
                Note.category.like(pattern, escape="\\"),
            )
        )

    base_q = db.query(Note).filter(*filters)
    page = max(page, 1)
    page_size = min(max(page_size, 5), 200)
    total = base_q.count()
    pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, pages)
    rows = (
        base_q.order_by(Note.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    entity_rows = []
    for n in rows:
        local_dt = to_local_naive(n.created_at)
        entity_rows.append(
            {
                "date": local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else "",
                "type_label": "طلبية" if n.note_type == "order" else "ملاحظة",
                "description": n.description or "",
                "person": n.person or "",
                "category": n.category or "",
            }
        )

    return templates.TemplateResponse(
        request,
        "table.html",
        {
            "active": "notes",
            "title": "الطلبيات والملاحظات",
            "total": total,
            "rows": entity_rows,
            "columns": NOTE_COLUMNS,
            "filters": {
                "extras": [],
                "placeholder": "بحث بالشخص أو التصنيف",
                "q": q,
                "selects": [
                    {
                        "name": "ntype",
                        "selected": ntype,
                        "options": [
                            ("", "الكل"),
                            ("order", "طلبية"),
                            ("note", "ملاحظة"),
                        ],
                    }
                ],
                "clear_url": base_path,
            },
            "export_xlsx": None,
            "export_pdf": None,
            "page": page,
            "pages": _pagination(page, pages, base_path, params),
        },
    )


@app.get("/dashboard/customers", response_class=HTMLResponse)
async def dashboard_customers(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """سجلّ الأطراف (عملاء/موردون): أسماء مجمّعة من المعاملات والطلبيات والمهام."""
    base = (settings.base_currency or "ILS").upper()
    csrf_token = _csrf_token_or_new(request)
    rows = _party_rows(db, base)
    for row in rows:
        row["net_cls"] = "positive" if _to_pos(row["net"]) >= 0 else "negative"
    merged = request.query_params.get("merged")
    resp = templates.TemplateResponse(
        request,
        "customers.html",
        {
            "active": "customers",
            "base_currency": base,
            "rows": rows,
            "merged": merged,
            "csrf_token": csrf_token,
        },
    )
    _set_csrf_cookie(resp, csrf_token, request)
    return resp


def _to_pos(value: str) -> float:
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


@app.post("/dashboard/customers/merge", response_class=RedirectResponse)
async def dashboard_customers_merge(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
    _csrf: None = Depends(require_dashboard_csrf),
):
    """دمج اسمين لنفس الطرف: استبدال source بـ target في كل السجلات."""
    from app.database.crud import merge_person

    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw)
    source = (parsed.get("source") or [""])[0].strip()
    target = (parsed.get("target") or [""])[0].strip()
    if source and target:
        merge_person(db, source, target)
    return RedirectResponse(url="/dashboard/customers?merged=1", status_code=303)


@app.get("/dashboard/export", response_class=Response)
async def dashboard_export(
    request: Request,
    fmt: str = "xlsx",
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """تقرير شامل لكل المستخدمين بتصدير Excel أو PDF (أزرار اللوحة الرئيسية)."""
    if fmt == "pdf":
        from bot.exporters import generate_export_pdf

        buf = generate_export_pdf(db, None)
        media_type = "application/pdf"
        ext = "pdf"
    else:
        from bot.exporters import generate_export_excel

        buf = generate_export_excel(db, None)
        media_type = EXCEL_MIME
        ext = "xlsx"
    filename = f"report_{datetime.now().strftime('%Y%m%d')}.{ext}"
    return Response(
        content=buf.getvalue(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
        {"active": "budgets", "budgets": budgets},
    )


@app.get("/dashboard/company", response_class=HTMLResponse)
async def dashboard_company(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """صفحة الشركة: الأعضاء وأدوارهم والدعوات النشطة."""
    csrf_token = _csrf_token_or_new(request)
    companies = db.query(Company).order_by(Company.created_at.desc()).all()
    rows = []
    for comp in companies:
        members = db.query(CompanyMember).filter(CompanyMember.company_id == comp.id).order_by(CompanyMember.joined_at.asc()).all()
        invites = db.query(InviteLink).filter(InviteLink.company_id == comp.id, InviteLink.revoked == False).order_by(InviteLink.created_at.desc()).limit(20).all()  # noqa: E712
        rows.append({"company": comp, "members": members, "invites": invites})
    resp = templates.TemplateResponse(request, "company.html", {"active": "company", "rows": rows, "csrf_token": csrf_token})
    _set_csrf_cookie(resp, csrf_token, request)
    return resp


@app.post("/dashboard/company/update", response_class=RedirectResponse)
async def dashboard_company_update(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
    _csrf: None = Depends(require_dashboard_csrf),
):
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw)
    cid = int((parsed.get("company_id") or ["0"])[0] or 0)
    name = (parsed.get("name") or [""])[0].strip()
    description = (parsed.get("description") or [""])[0].strip()
    # للوحة التحكم: نعتبر المالك هو الفاعل (أول عضو owner)
    comp = db.query(Company).filter(Company.id == cid).first()
    if comp and name:
        from app.database.crud.company import update_company

        # استخدم owner كممثل
        update_company(db, comp.owner_telegram_user_id, cid, name=name, description=description)
    return RedirectResponse(url="/dashboard/company", status_code=303)


@app.post("/dashboard/company/invite/revoke", response_class=RedirectResponse)
async def dashboard_company_revoke(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
    _csrf: None = Depends(require_dashboard_csrf),
):
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw)
    invite_id = int((parsed.get("invite_id") or ["0"])[0] or 0)
    invite = db.query(InviteLink).filter(InviteLink.id == invite_id).first()
    if invite:
        comp = db.query(Company).filter(Company.id == invite.company_id).first()
        if comp:
            from app.database.crud.company import revoke_invite

            revoke_invite(db, comp.owner_telegram_user_id, invite_id)
    return RedirectResponse(url="/dashboard/company", status_code=303)


@app.post("/dashboard/company/transfer", response_class=RedirectResponse)
async def dashboard_company_transfer(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
    _csrf: None = Depends(require_dashboard_csrf),
):
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw)
    cid = int((parsed.get("company_id") or ["0"])[0] or 0)
    target = int((parsed.get("target_id") or ["0"])[0] or 0)
    comp = db.query(Company).filter(Company.id == cid).first()
    if comp and target:
        from app.database.crud.company import transfer_company_ownership

        transfer_company_ownership(db, comp.owner_telegram_user_id, target)
    return RedirectResponse(url="/dashboard/company", status_code=303)


@app.post("/dashboard/company/delete", response_class=RedirectResponse)
async def dashboard_company_delete(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
    _csrf: None = Depends(require_dashboard_csrf),
):
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw)
    cid = int((parsed.get("company_id") or ["0"])[0] or 0)
    confirm = (parsed.get("confirm") or [""])[0]
    if confirm != "DELETE":
        return RedirectResponse(url="/dashboard/company", status_code=303)
    comp = db.query(Company).filter(Company.id == cid).first()
    if comp:
        from app.database.crud.company import delete_company

        delete_company(db, comp.owner_telegram_user_id, cid, confirm=True)
    return RedirectResponse(url="/dashboard/company", status_code=303)


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
