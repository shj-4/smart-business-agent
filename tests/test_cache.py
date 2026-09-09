"""
اختبارات ذاكرة التخزين المؤقت (TTL cache) للاستعلامات المتكررة.
"""

import time

from app import cache
from app.cache import clear_all, get, get_or_set, set


def test_get_or_set_caches_and_reuses():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return {"total": 100}

    clear_all()
    first = get_or_set("k1", factory)
    second = get_or_set("k1", factory)
    assert first == {"total": 100}
    assert second == {"total": 100}
    assert calls["n"] == 1


def test_returns_independent_copies():
    clear_all()
    value = get_or_set("kc", lambda: {"total": 5})
    value["total"] = 999
    assert get("kc") == {"total": 5}


def test_ttl_expiry():
    clear_all()
    set("kexp", {"x": 1}, ttl_seconds=0.2)
    assert get("kexp") != cache._UNSET
    time.sleep(0.35)
    assert get("kexp") is cache._UNSET


def test_clear_scoped_namespace():
    clear_all()
    set("run_query:111:total_expenses:this_month:None", {"a": 1})
    set("run_query:222:total_expenses:this_month:None", {"b": 2})
    set("admin_stats", {"c": 3})
    cache.clear("run_query:111")
    assert get("run_query:111:total_expenses:this_month:None") is cache._UNSET
    assert get("run_query:222:total_expenses:this_month:None") == {"b": 2}
    assert get("admin_stats") == {"c": 3}


def test_none_values_not_cached():
    clear_all()
    got = get_or_set("knone", lambda: None)
    assert got is None
    assert get("knone") is cache._UNSET
