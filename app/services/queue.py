"""Fair in-process queue behind a replaceable job-queue boundary."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QueueItem:
    job_id: str
    user_id: int
    chat_id: int
    url: str
    quality: str
    locale: str
    created_monotonic: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    retry_of: str | None = None
    status_message_id: int | None = None
    request_message_id: int | None = None
    output_mode: str = "video"


class QueueStopped(Exception):
    pass


class DownloadQueue:
    def __init__(self) -> None:
        self.runtime: RuntimeConfig | None = None
        self._queues: dict[int, deque[QueueItem]] = {}
        self._rotation: deque[int] = deque()
        self._user_counts: dict[int, int] = {}
        self._last_message: dict[int, float] = {}
        self._active: dict[str, QueueItem] = {}
        self._queued_by_id: dict[str, QueueItem] = {}
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        self._semaphore = asyncio.Semaphore(1)
        self._stopping = False

    def configure(self, runtime: RuntimeConfig) -> None:
        self.runtime = runtime
        self._semaphore = asyncio.Semaphore(int(runtime.get("max_concurrent_downloads")))

    def _setting(self, key: str):
        if self.runtime is None:
            raise RuntimeError("Download queue is not configured")
        return self.runtime.get(key)

    async def check_message_allowed(self, user_id: int) -> tuple[bool, int]:
        now = time.monotonic()
        interval = float(self._setting("min_message_interval"))
        async with self._lock:
            if len(self._last_message) > 10_000:
                cutoff = now - max(60.0, interval * 2)
                self._last_message = {uid: seen for uid, seen in self._last_message.items() if seen >= cutoff}
            last = self._last_message.get(user_id)
            if last is not None and now - last < interval:
                return False, max(1, int(interval - (now - last) + 0.999))
            self._last_message[user_id] = now
        return True, 0

    async def can_add(self, user_id: int, count: int) -> tuple[bool, int]:
        max_batch = int(self._setting("max_batch_size"))
        max_user = int(self._setting("max_user_queue"))
        if count <= 0:
            return False, 0
        if count > max_batch:
            return False, max_batch
        async with self._lock:
            current = self._user_counts.get(user_id, 0)
            available = max_user - current
            if count > available:
                return False, max(0, available)
            self._user_counts[user_id] = current + count
        return True, 0

    async def release_reserved(self, user_id: int, count: int = 1) -> None:
        async with self._lock:
            current = self._user_counts.get(user_id, 0)
            remaining = max(0, current - count)
            if remaining:
                self._user_counts[user_id] = remaining
            else:
                self._user_counts.pop(user_id, None)

    async def put(self, item: QueueItem) -> bool:
        async with self._condition:
            if self._stopping:
                return False
            # Same user's accidental duplicate is coalesced before it occupies
            # another queue slot. Cross-user heavy work is coalesced later by
            # content_id/effective quality.
            for existing in (*self._queued_by_id.values(), *self._active.values()):
                if existing.user_id == item.user_id and existing.url == item.url and existing.quality == item.quality:
                    return False
            user_queue = self._queues.setdefault(item.user_id, deque())
            was_empty = not user_queue
            user_queue.append(item)
            self._queued_by_id[item.job_id] = item
            if was_empty:
                self._rotation.append(item.user_id)
            self._condition.notify(1)
            return True

    async def get(self) -> QueueItem:
        async with self._condition:
            while not self._rotation:
                if self._stopping:
                    raise QueueStopped()
                await self._condition.wait()
            user_id = self._rotation.popleft()
            user_queue = self._queues[user_id]
            item = user_queue.popleft()
            self._queued_by_id.pop(item.job_id, None)
            if user_queue:
                self._rotation.append(user_id)
            else:
                self._queues.pop(user_id, None)
            self._active[item.job_id] = item
            return item

    async def cancel(self, job_id: str, requester_id: int) -> str:
        async with self._condition:
            item = self._queued_by_id.get(job_id)
            if item is not None:
                if item.user_id != requester_id:
                    return "forbidden"
                queue = self._queues.get(item.user_id)
                if queue:
                    self._queues[item.user_id] = deque(x for x in queue if x.job_id != job_id)
                    if not self._queues[item.user_id]:
                        self._queues.pop(item.user_id, None)
                        self._rotation = deque(uid for uid in self._rotation if uid != item.user_id)
                self._queued_by_id.pop(job_id, None)
                item.cancel_event.set()
                current = self._user_counts.get(item.user_id, 0)
                if current <= 1:
                    self._user_counts.pop(item.user_id, None)
                else:
                    self._user_counts[item.user_id] = current - 1
                return "queued"
            item = self._active.get(job_id)
            if item is not None:
                if item.user_id != requester_id:
                    return "forbidden"
                item.cancel_event.set()
                return "active"
            return "missing"

    async def run(self, handler: Callable[[QueueItem], Awaitable[None]]) -> None:
        while True:
            try:
                item = await self.get()
            except QueueStopped:
                return
            try:
                async with self._semaphore:
                    await handler(item)
            except asyncio.CancelledError:
                item.cancel_event.set()
                raise
            except Exception:
                logger.exception("Unhandled queue handler exception job_id=%s", item.job_id)
            finally:
                async with self._lock:
                    self._active.pop(item.job_id, None)
                await self.release_reserved(item.user_id)

    async def stop(self, *, cancel_active: bool = False) -> None:
        async with self._condition:
            self._stopping = True
            if cancel_active:
                for item in self._active.values():
                    item.cancel_event.set()
            self._condition.notify_all()

    @property
    def queued_count(self) -> int:
        return len(self._queued_by_id)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def user_count(self, user_id: int) -> int:
        return self._user_counts.get(user_id, 0)


download_queue = DownloadQueue()
