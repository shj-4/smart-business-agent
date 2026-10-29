"""
FastAPI تطبيق منفصل عن البوت (bot.py).

يعمل كـ health/status API + لوحة تحكم ويب (<div>/dashboard).
يُشغَّل كخدمة مستقلة (SmartBotAPI) على المنفذ 8000 عبر start_api.cmd /
setup_services.ps1. لوحة التحكم محتاجة ملفات قوالب في templates/.

⚠️ تحذير عزل المستأجرين: لوحة التحكم أداة مشغّل فقط (operator-only).
صفحات المعاملات/المهام/الملاحظات/الميزانيات و /api/* كانت تعرض بيانات كل الشركات
معًا — الفلتر company_id موجود فقط في الرئيسية والأطراف. لا تعرّض اللوحة
لأصحاب الشركات كمستأجرين منفصلين حتى يُطبَّق عزل كامل. تمت إضافة فلتر
اختياري company_id لبقية الصفحات، لكن العرض الافتراضي (بدون فلتر) يبقى
عرضًا تشغيليًا شاملاً. للحماية الإضافية: مصادقة Basic مع حدّ محاولات، ويُنصح
بتشغيلها خلف HTTPS (HSTS) وعدم كشف المنفذ 8000 مباشرة للإنترنت.

لا علاقة له بمنطق البوت (polling عبر bot.py) ولا يستقبل Updates من Telegram.
"""

import asyncio
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
from app.database.models import (
    Budget,
    Company,
    CompanyMember,
    InviteLink,
    Note,
    Task,
    Transaction,
    WorkspaceMember,
)
from app.exchange import convert_totals_to_base
from app.logging_config import configure_logging
from app.sentry import install_sentry
from app.timeutil import now_local, to_local_naive

configure_logging(service="api")
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
        # Preserve the domain-qualified identity; USERNAME alone can resolve
        # to a different local account when NSSM runs under a service identity.
        try:
            out = subprocess.check_output(
                ["whoami"], text=True, stderr=subprocess.DEVNULL
            )
            username = out.strip()
        except Exception:
            username = os.environ.get("USERNAME") or os.environ.get("USER")
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

        # أزل الوراثة وامنح المالك الحالي + SYSTEM (للخدمة)
        subprocess.run(
            ["icacls", path, "/inheritance:r"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # امنح المستخدم الحالي
        subprocess.run(
            ["icacls", path, "/grant:r", f"{username}:(F)"],
            check=False,
            capture_output=True,
            text=True,
        )
        # امنح SYSTEM أيضًا لخدمات nssm (LocalSystem)
        subprocess.run(
            ["icacls", path, "/grant:r", "SYSTEM:(F)"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result = subprocess.run(
            ["icacls", path, "/grant:r", f"{username}:(F)"],
            check=False,
            capture_output=True,
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
    path = _DASHBOARD_CRED_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except PermissionError:
        # A previous service identity may have left this file with a restrictive ACL.
        # Preserve it and write a fresh, private file for this process instead.
        path = f"{_DASHBOARD_CRED_FILE}.{os.getpid()}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"DASHBOARD_USERNAME={username}\nDASHBOARD_PASSWORD={password}\n")
    try:
        if os.name == "nt":
            _restrict_file_windows(path)
        else:
            os.chmod(path, 0o600)
    except Exception as exc:
        logger.warning("تعذر ضبط صلاحيات الملف %s: %s", path, exc)
    return path


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
    # قارن bytes لدعم non-ASCII (كلمات مرور عربية) بدون TypeError
    return secrets.compare_digest(user.encode("utf-8"), _DASHBOARD_USERNAME.encode("utf-8")) and secrets.compare_digest(
        password.encode("utf-8"), _DASHBOARD_PASSWORD.encode("utf-8")
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


_ALL_COMPANIES = object()


def _summary_month(db: Session, base: str, company_id: int | None | object = None) -> dict:
    """مجموع الشهر الحالي (موحَّدًا بالعملة الأساسية).

    عزل المستأجرين: إن حُدد company_id يُفلتر عليه، وإلا فهو عرض مشغّل عالمي.
    """
    by_currency: dict = {}
    stored: dict = {}
    start = _period_start("this_month")
    q = db.query(
        Transaction.type,
        Transaction.currency,
        Transaction.amount,
        Transaction.amount_in_base_currency,
        Transaction.base_currency_at_creation,
    ).filter(
        Transaction.deleted_at.is_(None),
        func.coalesce(Transaction.occurred_at, Transaction.created_at) >= start,
    )
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q = q.filter(Transaction.company_id.is_(None))
    else:
        q = q.filter(Transaction.company_id == company_id)
    from app.database.crud.common import _cash_filter as _main_cash
    q = q.filter(_main_cash(Transaction))
    rows = q.all()
    for rtype, currency, amount, stored_amt, stored_base in rows:
        c = (currency or "").upper()
        kind = "expense" if rtype == "expense" else "income"
        if stored_amt is not None and stored_base == base and c and c != base:
            se = stored.setdefault(c, {"expense": Decimal("0"), "income": Decimal("0")})
            se[kind] += Decimal(str(stored_amt))
        else:
            entry = by_currency.setdefault(c, {"expense": Decimal("0"), "income": Decimal("0")})
            entry[kind] += amount or Decimal("0")
    return _convert_month_currency_groups(by_currency, base, stored=stored or None)


def _category_tops(db: Session, base: str, limit: int = 6, company_id: int | None | object = None) -> list[dict]:
    """أعلى تصنيفات المصاريف هذا الشهر (موحَّدة بالعملة الأساسية).

    عزل المستأجرين: إن حُدد company_id يُفلتر عليه، وإلا يُعرض الكل
    (للوحة المشغّل) لكن مع تجميع per-category لا يزال عابرًا — يُفضل
    فلترة اللوحة حسب الشركة في الإنتاج.
    """
    start = _period_start("this_month")
    q = db.query(
        Transaction.category,
        Transaction.currency,
        Transaction.amount,
        Transaction.amount_in_base_currency,
        Transaction.base_currency_at_creation,
    ).filter(
        Transaction.deleted_at.is_(None),
        Transaction.type == "expense",
        func.coalesce(Transaction.occurred_at, Transaction.created_at) >= start,
    )
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q = q.filter(Transaction.company_id.is_(None))
    else:
        q = q.filter(Transaction.company_id == company_id)
    from app.database.crud.common import _cash_filter as _main_cash
    q = q.filter(_main_cash(Transaction))
    rows = q.all()
    agg: dict = {}
    for category, currency, amount, stored_amt, stored_base in rows:
        key = category or "أخرى"
        bucket = agg.setdefault(key, {"by_currency": {}, "live": {}, "stored": {}})
        c = (currency or "").upper()
        entry = bucket["by_currency"].setdefault(c, Decimal("0"))
        entry += amount or Decimal("0")
        is_stored = stored_amt is not None and stored_base == base and c and c != base
        if is_stored:
            se = bucket["stored"].setdefault(c, Decimal("0"))
            se += Decimal(str(stored_amt))
        else:
            le = bucket["live"].setdefault(c, Decimal("0"))
            le += amount or Decimal("0")
    out = []
    for key, bucket in agg.items():
        live = bucket.get("live") or {}
        # العقد الموحّد: حي فقط + مثبّت فقط
        conv = convert_totals_to_base(
            live, base, stored=bucket["stored"] or None
        )
        total = conv.get("total")
        if total is None:
            continue
        out.append({"category": key, "amount": _fmt(total), "raw": float(total)})
    out.sort(key=lambda x: x["raw"], reverse=True)
    return out[:limit]


def _party_rows(db: Session, base: str, company_id: int | None | object = None) -> list[dict]:
    """تجمع أسماء الأطراف (عملاء/موردون) عبر المعاملات والطلبيات والمهام.

    كل طرف = اسم نصي؛ المجاميع تُوحَّد بالعملة الأساسية (المخزَّنة عند
    التسجيل إن توافقت، وإلا بسعر اليوم). المبالغ مشفّرة فلا يمكن جمعها عبر
    SQL (يجب فكّ التشفير في Python)، لذا لا يُقصّ أي حزام بيانات — الاختبار
    `test_party_rows_computes_full_aggregates_without_cap` يضمن 520 سجلًا كاملًا.

    التحسين: دفع التجميع الجزئي إلى SQL حيثما أمكن (counts عبر GROUP BY لـ
    Note/Task/Transaction، و streaming عبر `yield_per` للمعاملات) مع بقاء فكّ
    التشفير للمبالغ في Python فقط. يقلّل الحمل على Python دون فقدان الدقة.

    عزل المستأجرين: التجميع الآن على (company_id, person) — أطراف بنفس الاسم
    في شركتين لا يُجمعان. لو حُدد company_id يُفلتر عليه، وإلا يُعرض الكل
    لكن مع فصل حسب الشركة (لا دمج عابر).
    """
    # دفع counts إلى SQL (GROUP BY) — لا حاجة لسحب كل الصفوف لعدّها
    # نُجمّع على (company_id, person) لمنع دمج أسماء متطابقة عبر شركات مختلفة
    notes_count: dict[tuple, int] = {}
    q_notes = db.query(Note.company_id, Note.person, func.count(Note.id)).filter(
        Note.deleted_at.is_(None),
        Note.person.isnot(None),
        Note.note_type == "order",
    )
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q_notes = q_notes.filter(Note.company_id.is_(None))
    else:
        q_notes = q_notes.filter(Note.company_id == company_id)
    for cid, p, c in q_notes.group_by(Note.company_id, Note.person).all():
        key = (cid, (p or "").strip())
        if key[1]:
            notes_count[key] = notes_count.get(key, 0) + c
    tasks_count: dict[tuple, int] = {}
    q_tasks = db.query(Task.company_id, Task.person, func.count(Task.id)).filter(
        Task.deleted_at.is_(None), Task.person.isnot(None)
    )
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q_tasks = q_tasks.filter(Task.company_id.is_(None))
    else:
        q_tasks = q_tasks.filter(Task.company_id == company_id)
    for cid, p, c in q_tasks.group_by(Task.company_id, Task.person).all():
        key = (cid, (p or "").strip())
        if key[1]:
            tasks_count[key] = tasks_count.get(key, 0) + c
    # للمعاملات: count عبر SQL + streaming للمبالغ (فكّ التشفير فقط في Python)
    tx_counts: dict[tuple, int] = {}
    from app.database.crud.common import _cash_filter as _main_cash
    q_tx = db.query(Transaction.company_id, Transaction.person, func.count(Transaction.id)).filter(
        Transaction.deleted_at.is_(None), Transaction.person.isnot(None), _main_cash(Transaction)
    )
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q_tx = q_tx.filter(Transaction.company_id.is_(None))
    else:
        q_tx = q_tx.filter(Transaction.company_id == company_id)
    for cid, p, c in q_tx.group_by(Transaction.company_id, Transaction.person).all():
        key = (cid, (p or "").strip())
        if key[1]:
            tx_counts[key] = tx_counts.get(key, 0) + c
    q_rows = db.query(
        Transaction.company_id,
        Transaction.person,
        Transaction.type,
        Transaction.created_at,
        Transaction.currency,
        Transaction.amount,
        Transaction.amount_in_base_currency,
        Transaction.base_currency_at_creation,
    ).filter(Transaction.deleted_at.is_(None), Transaction.person.isnot(None), _main_cash(Transaction))
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q_rows = q_rows.filter(Transaction.company_id.is_(None))
    else:
        q_rows = q_rows.filter(Transaction.company_id == company_id)
    # yield_per دون .all() — كان .yield_per(500).all() يبطل الفائدة (يجلب الكل مرة واحدة)
    tx_rows = q_rows.order_by(Transaction.created_at.desc()).yield_per(500)
    agg: dict = {}
    # تهيئة records من SQL بدل العدّ في Python (التجميع الجزئي)
    # المفتاح هو (company_id, person) لمنع دمج أسماء متطابقة عبر شركات مختلفة
    for (cid, person), cnt in tx_counts.items():
        key = (cid, person)
        agg[key] = {"company_id": cid, "by_currency": {}, "live_by_currency": {}, "stored": {}, "records": cnt, "last_seen": None, "person": person}
    for cid, person, rtype, created_at, currency, amount, stored_amt, stored_base in tx_rows:
        key = (cid, person.strip() if person else "")
        if not key[1]:
            continue
        bucket = agg.setdefault(
            key,
            {"company_id": cid, "person": key[1], "by_currency": {}, "live_by_currency": {}, "stored": {}, "records": 0, "last_seen": None},
        )
        # تأكد من وجود live_by_currency للسجلات القادمة من tx_counts
        bucket.setdefault("live_by_currency", {})
        # records سبق عدّها عبر GROUP BY، لا نكرر الزيادة هنا
        if created_at and (bucket["last_seen"] is None or created_at > bucket["last_seen"]):
            bucket["last_seen"] = created_at
        kind = "expense" if rtype == "expense" else "income"
        c = (currency or "").upper()
        entry = bucket["by_currency"].setdefault(
            c, {"expense": Decimal("0"), "income": Decimal("0")}
        )
        entry[kind] += amount or Decimal("0")
        is_stored = stored_amt is not None and stored_base == base and c and c != base
        if is_stored:
            se = bucket["stored"].setdefault(
                c, {"expense": Decimal("0"), "income": Decimal("0")}
            )
            se[kind] += Decimal(str(stored_amt))
        else:
            live_entry = bucket["live_by_currency"].setdefault(
                c, {"expense": Decimal("0"), "income": Decimal("0")}
            )
            live_entry[kind] += amount or Decimal("0")
    for (cid, person), _cnt in notes_count.items():
        key = (cid, person)
        if key not in agg:
            agg[key] = {"company_id": cid, "person": person, "by_currency": {}, "live_by_currency": {}, "stored": {}, "records": 0, "last_seen": None}
        else:
            # حدّث العدّ إن وجد مسبقًا من المعاملات
            pass
    for (cid, person), _cnt in tasks_count.items():
        key = (cid, person)
        if key not in agg:
            agg[key] = {"company_id": cid, "person": person, "by_currency": {}, "live_by_currency": {}, "stored": {}, "records": 0, "last_seen": None}
    rows = []
    for (cid, person), bucket in agg.items():
        live = bucket.get("live_by_currency") or {}
        # العقد الموحّد: live + stored
        if bucket.get("stored"):
            by_for_conv = live
        else:
            by_for_conv = bucket["by_currency"]
        res = _convert_month_currency_groups(
            by_for_conv, base, stored=bucket["stored"] or None
        )
        expense = res["expense"] or Decimal("0")
        income = res["income"] or Decimal("0")
        last_seen = (
            to_local_naive(bucket["last_seen"]).strftime("%Y-%m-%d")
            if bucket["last_seen"]
            else ""
        )
        # نجمع السجلات من كل المصادر بنفس المفتاح المركّب
        total_records = bucket["records"] + notes_count.get((cid, person), 0) + tasks_count.get((cid, person), 0)
        rows.append(
            {
                "person": person,
                "company_id": cid,
                "records": total_records,
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
def health(db: Session = Depends(get_db), _auth: None = Depends(require_dashboard_auth)):
    """فحص حقيقي: اتصال قاعدة البيانات + صحة الجداول الأساسية (خفيف وسريع).

    محمي بمصادقة لوحة التحكم: يكشف حالة ENCRYPTION_KEY والهجرات والإصدارات
    وحجم قاعدة البيانات — لا يجب أن يكون عامًا.
    """
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
def status(db: Session = Depends(get_db), _auth: None = Depends(require_dashboard_auth)):
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


def _recent_transactions(db: Session, limit: int = 10, company_id: int | None | object = None):
    q = db.query(Transaction).filter(Transaction.deleted_at.is_(None))
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q = q.filter(Transaction.company_id.is_(None))
    else:
        q = q.filter(Transaction.company_id == company_id)
    rows = q.order_by(Transaction.created_at.desc()).limit(limit).all()
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


def _recent_tasks(db: Session, limit: int = 50, company_id: int | None | object = None):
    q = db.query(Task).filter(Task.deleted_at.is_(None))
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q = q.filter(Task.company_id.is_(None))
    else:
        q = q.filter(Task.company_id == company_id)
    rows = q.order_by(Task.due_date.is_(None).asc(), Task.due_date.asc()).limit(limit).all()

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


def _recent_notes(db: Session, limit: int = 10, company_id: int | None | object = None):
    q = db.query(Note).filter(Note.deleted_at.is_(None))
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q = q.filter(Note.company_id.is_(None))
    else:
        q = q.filter(Note.company_id == company_id)
    rows = q.order_by(Note.created_at.desc()).limit(limit).all()
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
def dashboard(
    request: Request,
    company_id: int | None = None,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """لوحة التحكم الرئيسية: بطاقات + ملخص الشهر + رسم + أعلى التصنيفات والأطراف.

    عزل المستأجرين: اللوحة أداة مشغّل — إن حُدد company_id تُفلتر كل البطاقات
    والرسوم والأطراف لتلك الشركة فقط، وإلا فهو عرض عالمي مع فصل حسب الشركة
    (لا دمج عابر لأسماء متطابقة).
    """
    # دعم __all__/__none__ عبر query param (string) — FastAPI param int لا يقبلها
    raw_cid = request.query_params.get("company_id")
    scope_all = False
    if raw_cid is not None and str(raw_cid).strip():
        val = str(raw_cid).strip()
        if val == "__all__":
            scope_all = True
            company_id = None
        elif val == "__none__":
            company_id = None
        else:
            try:
                company_id = int(val)
            except ValueError:
                company_id = None
    cid_for_helpers = _ALL_COMPANIES if scope_all else company_id

    # استخدم عملة الشركة إن حُددت، وإلا العملة العامة — يصلح استخدام
    # Company.base_currency الذي كان مُهمَلًا سابقًا
    if company_id is not None:
        comp = db.query(Company).filter(Company.id == company_id).first()
        if comp and comp.base_currency:
            base = comp.base_currency.upper()
        else:
            base = (settings.base_currency or "ILS").upper()
    else:
        base = (settings.base_currency or "ILS").upper()
    # فلاتر اختيارية حسب الشركة
    def _add_company_filter(q, model):
        if not hasattr(model, "company_id"):
            return q
        if scope_all:
            return q
        if company_id is None:
            return q.filter(model.company_id.is_(None))
        return q.filter(model.company_id == company_id)

    counts = {
        "transactions": _add_company_filter(db.query(func.count(Transaction.id)).filter(Transaction.deleted_at.is_(None)), Transaction).scalar(),
        "tasks": _add_company_filter(db.query(func.count(Task.id)).filter(Task.deleted_at.is_(None)), Task).scalar(),
        "notes": _add_company_filter(db.query(func.count(Note.id)).filter(Note.deleted_at.is_(None)), Note).scalar(),
        "users": _add_company_filter(db.query(Transaction.telegram_user_id).distinct(), Transaction).count(),
        "overdue": _add_company_filter(db.query(func.count(Task.id)).filter(Task.deleted_at.is_(None), Task.status == "overdue"), Task).scalar(),
    }

    summary = _summary_month(db, base, company_id=cid_for_helpers)
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
            db, months=settings.chart_months, base_currency=base, company_id=company_id, include_all=scope_all
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
            "top_categories": _category_tops(db, base, company_id=cid_for_helpers),
            "top_parties": _party_rows(db, base, company_id=cid_for_helpers)[:6],
            "transactions": _recent_transactions(db, limit=10, company_id=cid_for_helpers),
            "notes": _recent_notes(db, limit=10, company_id=cid_for_helpers),
            "overdue_tasks": [t for t in _recent_tasks(db, limit=100, company_id=cid_for_helpers) if t["status"] == "overdue"],
        },
    )


EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _sanitize_excel_cell_main(value: str | None) -> str:
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


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
        cell.alignment = Alignment(horizontal="center", readingOrder=2)
    try:
        ws.sheet_view.rightToLeft = True
    except Exception:
        pass
    for r in rows:
        local_dt = to_local_naive(r.created_at)
        ws.append(
            [
                local_dt.strftime("%Y-%m-%d %H:%M") if local_dt else "",
                "إيراد" if r.type == "income" else "مصروف",
                float(r.amount) if r.amount is not None else 0,
                _sanitize_excel_cell_main(r.currency or ""),
                _sanitize_excel_cell_main(r.person or ""),
                _sanitize_excel_cell_main(r.category or ""),
                _sanitize_excel_cell_main(r.description or ""),
            ]
        )
    widths = [18, 10, 12, 10, 20, 14, 34]
    for i, width in enumerate(widths, start=1):
        # إصلاح chr(64+i): يفشل بعد Z (26). استخدم get_column_letter الآمن.
        try:
            from openpyxl.utils import get_column_letter

            col_letter = get_column_letter(i)
        except Exception:
            col_letter = chr(64 + i) if i <= 26 else f"A{chr(64 + i - 26)}"
        ws.column_dimensions[col_letter].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = border
        row[2].number_format = "#,##0.00"
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


@app.get("/dashboard/transactions", response_class=HTMLResponse)
def dashboard_transactions(
    request: Request,
    period: str = "all_time",
    q: str = "",
    ttype: str = "",
    page: int = 1,
    page_size: int = 50,
    export: str = "",
    company_id: int | None = None,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """جدول المعاملات: فلاتر (فترة/بحث/نوع) + ترقيم صفحات + تصدير Excel.

    عزل المستأجرين: إن حُدد company_id يُفلتر عليه؛ اللوحة أداة مشغّل فقط.
    """
    # دعم company_id عبر query param (قد يأتي كـ string)
    scope_all = False
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    cid_for_helpers = _ALL_COMPANIES if scope_all else company_id
    base_path = "/dashboard/transactions"
    params: dict = {}
    if period != "all_time":
        params["period"] = period
    if q:
        params["q"] = q
    if ttype:
        params["ttype"] = ttype
    if scope_all:
        params["company_id"] = "__all__"
    elif company_id is not None:
        params["company_id"] = str(company_id)
    else:
        # default NULL scope — keep explicit __none__ in links
        params["company_id"] = "__none__"
    filters = [Transaction.deleted_at.is_(None)]
    if scope_all:
        pass
    elif company_id is None:
        filters.append(Transaction.company_id.is_(None))
    else:
        filters.append(Transaction.company_id == company_id)
    start = _period_start(period)
    if start:
        filters.append(func.coalesce(Transaction.occurred_at, Transaction.created_at) >= start)
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
def dashboard_tasks(
    request: Request,
    status: str = "",
    q: str = "",
    page: int = 1,
    page_size: int = 50,
    company_id: int | None = None,
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

    # company_id filter
    scope_all = False
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    base_path = "/dashboard/tasks"
    params: dict = {}
    if status:
        params["status"] = status
    if q:
        params["q"] = q
    if scope_all:
        params["company_id"] = "__all__"
    elif company_id is not None:
        params["company_id"] = str(company_id)
    else:
        # default NULL scope — keep explicit __none__ in links
        params["company_id"] = "__none__"
    filters = [Task.deleted_at.is_(None)]
    if scope_all:
        pass
    elif company_id is None:
        filters.append(Task.company_id.is_(None))
    else:
        filters.append(Task.company_id == company_id)
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
        base_q.order_by(Task.due_date.is_(None).asc(), Task.due_date.asc())
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
def dashboard_notes(
    request: Request,
    ntype: str = "",
    q: str = "",
    page: int = 1,
    page_size: int = 50,
    company_id: int | None = None,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """جدول الطلبيات والملاحظات: تصفية بالنوع وبالشخص + ترقيم صفحات."""
    scope_all = False
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    base_path = "/dashboard/notes"
    params: dict = {}
    if ntype:
        params["ntype"] = ntype
    if q:
        params["q"] = q
    if scope_all:
        params["company_id"] = "__all__"
    elif company_id is not None:
        params["company_id"] = str(company_id)
    else:
        # default NULL scope — keep explicit __none__ in links
        params["company_id"] = "__none__"
    filters = [Note.deleted_at.is_(None)]
    if scope_all:
        pass
    elif company_id is None:
        filters.append(Note.company_id.is_(None))
    else:
        filters.append(Note.company_id == company_id)
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
def dashboard_customers(
    request: Request,
    company_id: int | None = None,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """سجلّ الأطراف (عملاء/موردون): أسماء مجمّعة من المعاملات والطلبيات والمهام.

    عزل المستأجرين: اللوحة الآن تدعم فلتر company_id — بدون فلتر تُعرض كل
    الشركات لكن مع فصل حسب الشركة (لا دمج عابر لأسماء متطابقة).
    """
    scope_all = False
    # دعم فلتر company_id عبر query param + استخدام عملة الشركة
    try:
        q_cid_early = request.query_params.get("company_id")
        if q_cid_early is not None and str(q_cid_early).strip():
            val = str(q_cid_early).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    if company_id is not None:
        comp = db.query(Company).filter(Company.id == company_id).first()
        if comp and comp.base_currency:
            base = comp.base_currency.upper()
        else:
            base = (settings.base_currency or "ILS").upper()
    else:
        base = (settings.base_currency or "ILS").upper()
    # دعم فلتر company_id عبر query param (إعادة لضمان التزامن)
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    csrf_token = _csrf_token_or_new(request)
    rows = _party_rows(db, base, company_id=_ALL_COMPANIES if scope_all else company_id)
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
    """دمج اسمين لنفس الطرف: استبدال source بـ target في كل السجلات.

    عزل المستأجرين: الدمج معزول حسب الشركة — يُمرر company_id من النموذج
    (مأخوذ من صف الطرف في _party_rows) لمنع الدمج العابر.
    """
    from app.database.crud import merge_person

    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw)
    source = (parsed.get("source") or [""])[0].strip()
    target = (parsed.get("target") or [""])[0].strip()
    # تمييز "بلا شركة" (__none__) عن "الكل" (عدم إرسال الحقل)
    # القالب الآن يرسل دائمًا company_id: رقم أو "__none__" للـ NULL
    has_company_field = "company_id" in parsed
    company_id_raw = (parsed.get("company_id") or [""])[0].strip()
    company_id = None
    # sentinel للـ "الكل" — نستخدمه عند عدم وجود الحقل أصلاً (طلبات قديمة/خارجية)
    _ALL_SENTINEL = object()
    if not has_company_field:
        company_id = _ALL_SENTINEL  # كل الشركات (سلوك قديم)
    elif company_id_raw == "__none__":
        company_id = None  # بلا شركة فقط (NULL)
    elif company_id_raw == "__all__" or company_id_raw == "":
        company_id = _ALL_SENTINEL
    elif company_id_raw:
        try:
            company_id = int(company_id_raw)
        except ValueError:
            company_id = _ALL_SENTINEL
    else:
        company_id = _ALL_SENTINEL
    if source and target:
        # مرر company_id كما هو: None يعني بلا شركة، _ALL_SENTINEL يعني الكل
        if company_id is _ALL_SENTINEL:
            await asyncio.to_thread(merge_person, db, source, target)
        else:
            await asyncio.to_thread(merge_person, db, source, target, company_id=company_id)
    redirect_url = "/dashboard/customers?merged=1"
    if company_id is not None and company_id is not _ALL_SENTINEL:
        redirect_url += f"&company_id={company_id}"
    elif company_id is None and has_company_field:
        redirect_url += "&company_id=__none__"
    return RedirectResponse(url=redirect_url, status_code=303)


@app.get("/dashboard/export", response_class=Response)
def dashboard_export(
    request: Request,
    fmt: str = "xlsx",
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """تقرير شامل لكل المستخدمين بتصدير Excel أو PDF (أزرار اللوحة الرئيسية)."""
    scope_all = False
    company_id = None
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    if fmt == "pdf":
        from bot.exporters import generate_export_pdf

        buf = generate_export_pdf(db, None, company_id=company_id, scope_all=scope_all)
        media_type = "application/pdf"
        ext = "pdf"
    else:
        from bot.exporters import generate_export_excel

        buf = generate_export_excel(db, None, company_id=company_id, scope_all=scope_all)
        media_type = EXCEL_MIME
        ext = "xlsx"
    filename = f"report_{datetime.now().strftime('%Y%m%d')}.{ext}"
    return Response(
        content=buf.getvalue(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- الميزانيات (لوحة التحكم + JSON) ----------


def _budget_rows(db: Session, company_id: int | None | object = None) -> list[dict]:
    from app.database.crud import budget_monthly_reset, budget_usage
    from app.exchange import CURRENCY_NAMES

    rows = []
    q = db.query(Budget).order_by(Budget.created_at.asc())
    if company_id is _ALL_COMPANIES:
        pass
    elif company_id is None:
        q = q.filter(Budget.company_id.is_(None))
    else:
        q = q.filter(Budget.company_id == company_id)
    budgets = q.all()
    for b in budgets:
        budget_monthly_reset(db, b)
        usage = budget_usage(db, b)
        if b.scope == "currency":
            target = CURRENCY_NAMES.get(b.currency, b.currency)
        elif b.scope == "category":
            target = f"تصنيف {b.category or '—'}"
        elif b.scope == "person":
            target = f"الشخص {b.person or '—'}"
        else:
            target = b.person or b.currency or "—"
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
def dashboard_budgets(
    request: Request,
    company_id: int | None = None,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """عرض الميزانيات الشهرية وحالة كل منها."""
    scope_all = False
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    budgets = _budget_rows(db, company_id=_ALL_COMPANIES if scope_all else company_id)
    return templates.TemplateResponse(
        request,
        "budgets.html",
        {"active": "budgets", "budgets": budgets},
    )


@app.get("/dashboard/company", response_class=HTMLResponse)
def dashboard_company(
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
    try:
        cid = int(str((parsed.get("company_id") or ["0"])[0] or "0").strip())
    except (TypeError, ValueError):
        cid = 0
    name = (parsed.get("name") or [""])[0].strip()
    description = (parsed.get("description") or [""])[0].strip()
    # للوحة التحكم: نعتبر المالك هو الفاعل (أول عضو owner)
    comp = db.query(Company).filter(Company.id == cid).first()
    if comp and name:
        from app.database.crud.company import update_company

        # استخدم owner كممثل — لفّ العمل المتزامن خارج حلقة الأحداث
        await asyncio.to_thread(update_company, db, comp.owner_telegram_user_id, cid, name, description)
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

            await asyncio.to_thread(revoke_invite, db, comp.owner_telegram_user_id, invite_id)
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
    try:
        cid = int(str((parsed.get("company_id") or ["0"])[0] or "0").strip())
    except (TypeError, ValueError):
        cid = 0
    target = int((parsed.get("target_id") or ["0"])[0] or 0)
    comp = db.query(Company).filter(Company.id == cid).first()
    if comp and target:
        from app.database.crud.company import transfer_company_ownership

        await asyncio.to_thread(transfer_company_ownership, db, comp.owner_telegram_user_id, target)
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
    try:
        cid = int(str((parsed.get("company_id") or ["0"])[0] or "0").strip())
    except (TypeError, ValueError):
        cid = 0
    confirm = (parsed.get("confirm") or [""])[0]
    if confirm != "DELETE":
        return RedirectResponse(url="/dashboard/company", status_code=303)
    comp = db.query(Company).filter(Company.id == cid).first()
    if comp:
        from app.database.crud.company import delete_company

        await asyncio.to_thread(delete_company, db, comp.owner_telegram_user_id, cid, True)
    return RedirectResponse(url="/dashboard/company", status_code=303)


@app.get("/api/budgets")
def api_budgets(
    request: Request,
    company_id: int | None = None,
    db: Session = Depends(get_db), _auth: None = Depends(require_dashboard_auth)
):
    """الميزانيات الشهرية بصيغة JSON."""
    scope_all = False
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    return _budget_rows(db, company_id=_ALL_COMPANIES if scope_all else company_id)


# ---------- APIs للبيانات (JSON) ----------


@app.get("/api/transactions")
def api_transactions(
    request: Request,
    period: str = "all_time",
    company_id: int | None = None,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_dashboard_auth),
):
    """معاملات مالية بصيغة JSON (أحدث 500)."""
    scope_all = False
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    q = db.query(Transaction).filter(Transaction.deleted_at.is_(None))
    if scope_all:
        pass
    elif company_id is None:
        q = q.filter(Transaction.company_id.is_(None))
    else:
        q = q.filter(Transaction.company_id == company_id)
    start = _period_start(period)
    if start:
        q = q.filter(func.coalesce(Transaction.occurred_at, Transaction.created_at) >= start)
    rows = q.order_by(func.coalesce(Transaction.occurred_at, Transaction.created_at).desc()).limit(500).all()
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
def api_tasks(
    request: Request,
    company_id: int | None = None,
    db: Session = Depends(get_db), _auth: None = Depends(require_dashboard_auth)
):
    """المهام بصيغة JSON."""
    scope_all = False
    try:
        q_cid = request.query_params.get("company_id")
        if q_cid is not None and str(q_cid).strip():
            val = str(q_cid).strip()
            if val == "__all__":
                scope_all = True
                company_id = None
            elif val == "__none__":
                company_id = None
            else:
                company_id = int(val)
    except Exception:
        pass
    # إن حُدد company_id فلتره؛ وإلا نفس السلوك (NULL scope افتراضيًا)
    if scope_all:
        rows = db.query(Task).filter(Task.deleted_at.is_(None)).order_by(Task.due_date.is_(None).asc(), Task.due_date.asc()).limit(500).all()
    elif company_id is not None:
        rows = db.query(Task).filter(Task.deleted_at.is_(None), Task.company_id == company_id).order_by(Task.due_date.is_(None).asc(), Task.due_date.asc()).limit(500).all()
    else:
        rows = db.query(Task).filter(Task.deleted_at.is_(None), Task.company_id.is_(None)).order_by(Task.due_date.is_(None).asc(), Task.due_date.asc()).limit(500).all()
    out = []
    for r in rows:
        due_str = to_local_naive(r.due_date).strftime("%Y-%m-%d %H:%M") if r.due_date else ""
        out.append({"id": r.id, "description": r.description or "", "person": r.person or "", "due_date": due_str, "status": r.status})
    return out
