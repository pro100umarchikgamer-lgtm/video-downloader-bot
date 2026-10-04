from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

T = TypeVar("T")


async def telegram_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    max_retry_after: int = 30,
) -> T:
    attempt = 0
    while True:
        try:
            return await operation()
        except TelegramRetryAfter as exc:
            attempt += 1
            if attempt >= max_attempts or exc.retry_after > max_retry_after:
                raise
            await asyncio.sleep(max(0.1, float(exc.retry_after)))
        except TelegramNetworkError:
            attempt += 1
            if attempt >= max_attempts:
                raise
            await asyncio.sleep(float(attempt))
