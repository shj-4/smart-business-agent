"""
FastAPI تطبيق منفصل عن البوت (bot.py).

هذا التطبيق يعمل كـ health/status API فقط، ويُشغَّل كخدمة مستقلة (SmartBotAPI)
على المنفذ 8000 عبر start_api.cmd / setup_services.ps1.

لا علاقة له بمنطق البوت (polling عبر bot.py) ولا يستقبل Updates من Telegram.
الغرض منه فقط: فحص صحة النظام (قاعدة البيانات) والتقارير التشغيلية الداخلية
لمراقبة الخدمات. إذا أُريد تحويل البوت لـ webhook mode لاحقًا، يُعتمد على هذا
التطبيق كـ entry point ويُضاف فيه endpoint لاستقبال Updates.
"""

from datetime import datetime, timezone

from fastapi import FastAPI, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import TELEGRAM_BOT_TOKEN
from app.database.db import get_db, engine
from app.database.models import Transaction, Task, Note

app = FastAPI(
    title="Smart Business Agent API",
    description=(
        "Health/status API منفصل عن bot.py (polling). "
        "لا يقبل Updates من Telegram حاليًا."
    ),
)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Smart Business Agent API (health check)",
        "note": "منفصل عن bot.py polling — راجع /health و /status للمزيد",
    }


@app.get("/health")
async def health(db: Session = Depends(get_db)):
    """فحص حقيقي: اتصال قاعدة البيانات + صحة الجداول الأساسية."""
    db_ok = False
    try:
        db.execute(func.count(Transaction.id))
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
        db.execute(func.count(Transaction.id))
    except Exception:
        db_ok = False

    now = datetime.now(timezone.utc)

    summary = {
        "status": "ok" if db_ok else "degraded",
        "checked_at": now.isoformat(),
        "database": {
            "connected": db_ok,
            "url": str(engine.url).replace("sqlite:///", "").split("\\")[-1],
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

    return summary
