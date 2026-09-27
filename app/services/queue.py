from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable


MIN_MESSAGE_INTERVAL = float(
    os.getenv("MIN_MESSAGE_INTERVAL", "9")
)

MAX_BATCH_SIZE = int(
    os.getenv("MAX_BATCH_SIZE", "6")
)

MAX_USER_QUEUE = int(
    os.getenv("MAX_USER_QUEUE", "6")
)

MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")
)


@dataclass
class QueueItem:
    user_id: int
    url: str


class DownloadQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._user_counts: dict[int, int] = {}
        self._last_message: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def check_message_allowed(
        self,
        user_id: int,
    ) -> tuple[bool, int]:
        """
        Проверяет задержку между отдельными сообщениями.

        Возвращает:
        (True, 0) если сообщение можно принять;
        (False, remaining_seconds) если ещё рано.
        """
        now = time.monotonic()

        async with self._lock:
            last = self._last_message.get(user_id)

            if last is not None:
                elapsed = now - last

                if elapsed < MIN_MESSAGE_INTERVAL:
                    remaining = int(
                        MIN_MESSAGE_INTERVAL - elapsed + 0.999
                    )
                    return False, remaining

            self._last_message[user_id] = now

        return True, 0

    async def can_add(
        self,
        user_id: int,
        count: int,
    ) -> tuple[bool, int]:
        """
        Проверяет, можно ли добавить count задач
        конкретному пользователю.
        """
        if count <= 0:
            return False, 0

        if count > MAX_BATCH_SIZE:
            return False, MAX_BATCH_SIZE

        async with self._lock:
            current = self._user_counts.get(user_id, 0)
            available = MAX_USER_QUEUE - current

            if count > available:
                return False, max(available, 0)

            self._user_counts[user_id] = current + count

        return True, 0

    async def release_user_task(
        self,
        user_id: int,
    ) -> None:
        async with self._lock:
            current = self._user_counts.get(user_id, 0)

            if current <= 1:
                self._user_counts.pop(user_id, None)
            else:
                self._user_counts[user_id] = current - 1

    async def put(
        self,
        item: QueueItem,
    ) -> None:
        await self._queue.put(item)

    async def get(self) -> QueueItem:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def run(
        self,
        handler: Callable[[QueueItem], Awaitable[None]],
    ) -> None:
        while True:
            item = await self.get()

            try:
                async with self._semaphore:
                    await handler(item)
            finally:
                await self.release_user_task(item.user_id)
                self.task_done()


download_queue = DownloadQueue()
