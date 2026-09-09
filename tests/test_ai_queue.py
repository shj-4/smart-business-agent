"""
اختبارات طابور معالجة الـ AI (اختياري، AI_QUEUE_ENABLED).
"""

import asyncio

from app.ai_queue import AIServiceQueue


def test_disabled_runs_inline():
    q = AIServiceQueue()
    q.enabled = False
    q._queue = None

    async def run():
        return await q.submit(lambda: 42)

    assert asyncio.run(run()) == 42


def test_enabled_processes_and_returns_results():
    q = AIServiceQueue()
    q.enabled = True
    q._queue = None
    q.processed = 0
    q.failed = 0

    async def run():
        await q.ensure_started()
        try:
            results = await asyncio.gather(
                q.submit(lambda: "one"),
                q.submit(lambda: "two"),
                q.submit(lambda: "three"),
            )
            return results, q.processed
        finally:
            await q.stop()

    results, processed = asyncio.run(run())
    assert sorted(results) == ["one", "three", "two"]
    assert processed == 3


def test_exception_propagates_to_caller():
    q = AIServiceQueue()
    q.enabled = True
    q._queue = None
    q.processed = 0
    q.failed = 0

    def boom():
        raise ValueError("boom")

    async def run():
        await q.ensure_started()
        try:
            with __import__("pytest").raises(ValueError):
                await q.submit(boom)
            return q.failed, q.processed
        finally:
            await q.stop()

    failed, processed = asyncio.run(run())
    assert failed == 1
    assert processed == 1


def test_sync_side_effect_runs_in_thread():
    import threading

    q = AIServiceQueue()
    q.enabled = True
    q._queue = None
    q.processed = 0
    q.failed = 0
    seen = threading.Event()

    def work():
        seen.set()
        return "ok"

    async def run():
        await q.ensure_started()
        try:
            return await q.submit(work)
        finally:
            await q.stop()

    assert asyncio.run(run()) == "ok"
    assert seen.is_set()
