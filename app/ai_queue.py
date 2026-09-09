"""
طابور معالجة غير متزامن خلف تحليل الـ AI (Gemini).

مفعّل افتراضيًا (AI_QUEUE_ENABLED=true) كإعداد الإنتاج المُوصى به: تُنفَّذ
مهام التعرف/التحليل بحدّ توافق (concurrency) وبسعة طابور محدودة (maxsize)
بدل إطلاق كل طلب مباشرةً على Gemini دفعة واحدة وقت الذروة. فوق سعة الطابور
تنشأ "ضغط رجعي" (backpressure): المتصل ينتظر حتى يتسع مساحة — الطريقة لا
تُسقط رسائل ولا تُكرّرها.

الوحدة المعالَجة عمل متزامن (function تُنفَّذ في خيط عبر asyncio.to_thread) كي
تظل حلقة الأحداث مستجيبة حتى مع استدعاءات Gemini المتزامنة.

عند إيقافه (AI_QUEUE_ENABLED=false) يُنفَّذ العمل مباشرةً — ولحماية حلقة
الأحداث يُغلَّف تحليل النصوص في bot/conversation عبر asyncio.to_thread على
أي حال (انظر fresh_entry / collect_reply).
"""

import asyncio
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class AIServiceQueue:
    def __init__(self) -> None:
        self.enabled = bool(settings.ai_queue_enabled)
        self.maxsize = max(1, int(settings.ai_queue_maxsize or 64))
        self.concurrency = max(1, int(settings.ai_queue_concurrency or 2))
        self._queue: asyncio.Queue | None = None
        self._workers: list[asyncio.Task] = []
        self.processed = 0
        self.failed = 0

    async def ensure_started(self) -> None:
        """ينشئ الطابور والعمال داخل حلقة الأحداث الجارية (يُستدعى عند تشغيل البوت)."""
        if not self.enabled or self._queue is not None:
            return
        self._queue = asyncio.Queue(maxsize=self.maxsize)
        loop = asyncio.get_running_loop()
        self._workers = [loop.create_task(self._worker(i)) for i in range(self.concurrency)]
        logger.info("AI queue started: %d workers, max %d", self.concurrency, self.maxsize)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers = []
        self._queue = None

    @property
    def pending(self) -> int:
        if self._queue is None or self._queue.empty():
            return 0
        return self._queue.qsize()

    async def submit(self, work) -> Any:
        """ينفّذ عملًا متزامنًا (fn) — عبر الطابور إن كان مفعّلًا وإلا مباشرةً."""
        if not self.enabled or self._queue is None:
            return work()
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put((work, fut))  # ينتظر تلقائيًا عندما يمتلئ الطابور
        return await fut

    async def _worker(self, idx: int) -> None:
        while True:
            work, fut = await self._queue.get()
            try:
                result = await asyncio.to_thread(work)
                if not fut.done():
                    fut.set_result(result)
            except Exception as exc:
                self.failed += 1
                logger.exception("AI queue worker %d error", idx)
                if not fut.done():
                    fut.set_exception(exc)
            finally:
                self._queue.task_done()
                self.processed += 1


aiq = AIServiceQueue()
