"""Bounded work lanes with per-chat round-robin fairness, deduplication and overload shedding.

Two lanes are used: `text` (fast) and `media` (CPU/IO heavy), so a large GIF can never delay text.
Handlers only enqueue; they never await translation work (incoming updates are never blocked).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from translator_bot.metrics import Metrics

log = logging.getLogger(__name__)

JobFn = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class _Job:
    key: str | None
    fn: JobFn


class Lane:
    def __init__(self, name: str, *, workers: int, max_total: int, per_chat_max: int, job_timeout: float,
                 metrics: Metrics) -> None:
        self.name, self.workers, self.max_total, self.per_chat_max = name, workers, max_total, per_chat_max
        self.job_timeout = job_timeout
        self.metrics = metrics
        self._queues: OrderedDict[int, deque[_Job]] = OrderedDict()  # chat -> jobs; order = round robin
        self._keys: set[str] = set()
        self._size = 0
        self._cond = asyncio.Condition()
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self.inflight = 0

    @property
    def size(self) -> int:
        return self._size

    def start(self) -> None:
        for i in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker(), name=f"{self.name}-worker-{i}"))

    async def submit(self, chat_id: int, fn: JobFn, key: str | None = None) -> bool:
        """Enqueue a job. Returns False when shed (duplicate, lane full, chat over its share, closing)."""
        if self._closing:
            return False
        if key is not None and key in self._keys:
            self.metrics.inc(f"queue.{self.name}.duplicate")
            return False
        q = self._queues.get(chat_id)
        if self._size >= self.max_total or (q is not None and len(q) >= self.per_chat_max):
            self.metrics.inc(f"queue.{self.name}.shed")
            return False
        async with self._cond:
            if q is None:
                q = self._queues[chat_id] = deque()
            q.append(_Job(key, fn))
            if key is not None:
                self._keys.add(key)
            self._size += 1
            self.metrics.inc(f"queue.{self.name}.enqueued")
            self._cond.notify()
        return True

    async def _next(self) -> _Job | None:
        async with self._cond:
            while self._size == 0:
                if self._closing:
                    return None
                await self._cond.wait()
            chat_id, q = next(iter(self._queues.items()))
            job = q.popleft()
            self._size -= 1
            if q:
                self._queues.move_to_end(chat_id)  # round robin: this chat goes to the back
            else:
                del self._queues[chat_id]
            return job

    async def _worker(self) -> None:
        while True:
            job = await self._next()
            if job is None:
                return
            self.inflight += 1
            try:
                await asyncio.wait_for(job.fn(), timeout=self.job_timeout)
            except TimeoutError:
                self.metrics.inc(f"queue.{self.name}.timeout")
                log.warning("job timeout", extra={"lane": self.name})
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if self._closing or (task is not None and task.cancelling()):
                    raise  # the worker itself is being stopped
                # a job surfaced someone else's cancellation (e.g. a coalesced request whose owner timed out):
                # count it and keep serving — an escaping CancelledError used to kill this worker for good
                self.metrics.inc(f"queue.{self.name}.error")
                log.warning("job cancelled unexpectedly", extra={"lane": self.name})
            except Exception:
                self.metrics.inc(f"queue.{self.name}.error")
                log.exception("job failed", extra={"lane": self.name})
            finally:
                self.inflight -= 1
                if job.key is not None:
                    self._keys.discard(job.key)

    async def stop(self, drain_timeout: float = 10.0) -> None:
        """Graceful: stop accepting, let workers drain for up to drain_timeout, then cancel."""
        self._closing = True
        async with self._cond:
            self._cond.notify_all()
        if self._tasks:
            _done, pending = await asyncio.wait(self._tasks, timeout=drain_timeout)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()


@dataclass(slots=True)
class BurstItem:
    message_id: int
    text_html: str
    plain: str
    reply_to: object | None = None  # aiogram Message being replied to (first item's), optional


class BurstCoalescer:
    """Groups rapid consecutive text messages from one sender (same chat/topic) into one unit.

    A unit is flushed `window` seconds after its first message, or immediately when it reaches
    max_items / max_chars, or when a different kind of message from that sender arrives.
    window=0 disables coalescing (each message flushes immediately).
    """

    def __init__(self, flush: Callable[[tuple[int, int | None, int], list[BurstItem], object], Awaitable[None]],
                 *, window: float = 0.7, max_items: int = 5, max_chars: int = 1500) -> None:
        self._flush_cb = flush
        self.window, self.max_items, self.max_chars = window, max_items, max_chars
        self._pending: dict[tuple[int, int | None, int], tuple[list[BurstItem], object, asyncio.TimerHandle]] = {}

    async def add(self, key: tuple[int, int | None, int], item: BurstItem, meta: object) -> None:
        if self.window <= 0:
            await self._flush_cb(key, [item], meta)
            return
        entry = self._pending.get(key)
        if entry is None:
            loop = asyncio.get_running_loop()
            handle = loop.call_later(self.window, lambda: asyncio.ensure_future(self.flush(key)))
            self._pending[key] = ([item], meta, handle)
        else:
            items, _meta, handle = entry
            items.append(item)
            self._pending[key] = (items, meta, handle)  # latest meta (sender name etc.)
        items = self._pending[key][0]
        if len(items) >= self.max_items or sum(len(i.plain) for i in items) >= self.max_chars:
            await self.flush(key)

    def update_pending(self, key: tuple[int, int | None, int], message_id: int, text_html: str, plain: str) -> bool:
        """An edit arrived before the burst was flushed: replace the text in place."""
        entry = self._pending.get(key)
        if not entry:
            return False
        for it in entry[0]:
            if it.message_id == message_id:
                it.text_html, it.plain = text_html, plain
                return True
        return False

    async def flush(self, key: tuple[int, int | None, int]) -> None:
        entry = self._pending.pop(key, None)
        if entry is None:
            return
        items, meta, handle = entry
        if handle is not None:
            handle.cancel()
        await self._flush_cb(key, items, meta)

    async def flush_all(self) -> None:
        for key in list(self._pending):
            await self.flush(key)
