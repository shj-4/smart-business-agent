"""
مراقب uptime بسيطة: يفحص /health و /status بشكل دوري ويسجل النتائج.

يمكن تشغيله يدويًا أو كمهمة مجدولة:
  python scripts/uptime_check.py                    # فحص لمرة واحدة
  python scripts/uptime_check.py --loop --interval 60  # فحص كل 60 ثانية

النتائج تُسجَّل في logs/uptime.log (JSON Lines) وتظهر في stdout.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LOG_FILE = os.path.join(ROOT, "logs", "uptime.log")
API_BASE = "http://127.0.0.1:8000"

def _dashboard_auth_header() -> str | None:
    import base64
    import os
    user = os.environ.get("DASHBOARD_USERNAME")
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if not user or not pw:
        # try credentials file
        cred = os.path.join(ROOT, "data", "dashboard_credentials.txt")
        if os.path.exists(cred):
            try:
                for line in open(cred, encoding="utf-8"):
                    if line.startswith("DASHBOARD_USERNAME="):
                        user = line.strip().split("=", 1)[1]
                    elif line.startswith("DASHBOARD_PASSWORD="):
                        pw = line.strip().split("=", 1)[1]
            except Exception:
                pass
    if not user or not pw:
        return None
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return f"Basic {token}"


def _log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)


def _check_endpoint(path: str, timeout: int = 5) -> dict:
    """يفحص endpoint واحد ويعيد الحالة."""
    url = f"{API_BASE}{path}"
    start = time.monotonic()
    try:
        req = urllib.request.Request(url)
        auth = _dashboard_auth_header()
        if auth:
            req.add_header("Authorization", auth)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            return {
                "path": path,
                "status": "ok",
                "http_status": resp.status,
                "response_ms": elapsed_ms,
                "data": data,
            }
    except urllib.error.HTTPError as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "path": path,
            "status": "error",
            "http_status": exc.code,
            "response_ms": elapsed_ms,
            "error": str(exc),
        }
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "path": path,
            "status": "error",
            "http_status": None,
            "response_ms": elapsed_ms,
            "error": str(exc),
        }


def _write_log(entry: dict):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def check_once() -> bool:
    """يفحص /health و /status، يسجّل ويعرض النتائج. يعيد True إذا كل شي OK."""
    timestamp = datetime.now(UTC).isoformat()

    health = _check_endpoint("/health")
    status = _check_endpoint("/status")

    all_ok = (
        health["status"] == "ok"
        and (health.get("data") or {}).get("status") == "healthy"
        and (status.get("data") or {}).get("status") == "ok"
    )

    entry = {
        "timestamp": timestamp,
        "healthy": all_ok,
        "health": health,
        "status": status,
    }
    _write_log(entry)

    symbol = "OK " if all_ok else "FAIL"
    _log(
        f"{symbol} API {'healthy' if all_ok else 'UNHEALTHY'} "
        f"| health={health['response_ms']}ms "
        f"| status={status['response_ms']}ms"
    )
    if not all_ok:
        _log(f"  health: {health}")
        _log(f"  status: {status}")

    return all_ok


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="فحص uptime للخادم")
    parser.add_argument("--loop", action="store_true", help="تشغيل مستمر")
    parser.add_argument("--interval", type=int, default=60, help="الفاصل بالثانية (60 افتراضي)")
    args = parser.parse_args()

    if args.loop:
        _log(f"بدء المراقبة (كل {args.interval} ثانية) — اضغط Ctrl+C للإيقاف")
        while True:
            try:
                check_once()
                time.sleep(args.interval)
            except KeyboardInterrupt:
                _log("إيقاف المراقبة.")
                break
    else:
        ok = check_once()
        sys.exit(0 if ok else 1)
