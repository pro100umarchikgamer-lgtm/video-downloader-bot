"""In-process coalescing of identical expensive media preparation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar

from app.errors import DownloadCancelled

T = TypeVar("T")


@dataclass
class _Flight(Generic[T]):
    task: asyncio.Task[T]
    shared_cancel: asyncio.Event
    cleanup: Callable[[T], None]
    subscribers: int = 0


class InflightCoordinator(Generic[T]):
    def __init__(self) -> None:
        self._flights: dict[str, _Flight[T]] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(
        self,
        key: str,
        factory: Callable[[asyncio.Event], Awaitable[T]],
        subscriber_cancel: asyncio.Event,
        cleanup: Callable[[T], None],
    ):
        async with self._lock:
            flight = self._flights.get(key)
            if flight is None or flight.task.done() and flight.task.cancelled():
                shared_cancel = asyncio.Event()
                flight = _Flight(asyncio.create_task(factory(shared_cancel)), shared_cancel, cleanup)
                self._flights[key] = flight
            flight.subscribers += 1

        cancel_waiter = asyncio.create_task(subscriber_cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {flight.task, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_waiter in done and subscriber_cancel.is_set() and not flight.task.done():
                raise DownloadCancelled()
            result = await asyncio.shield(flight.task)
            if subscriber_cancel.is_set():
                raise DownloadCancelled()
            yield result
        finally:
            cancel_waiter.cancel()
            await asyncio.gather(cancel_waiter, return_exceptions=True)
            await self._release(key, flight)

    async def _release(self, key: str, flight: _Flight[T]) -> None:
        cleanup_result: T | None = None
        async with self._lock:
            flight.subscribers = max(0, flight.subscribers - 1)
            if flight.subscribers == 0:
                if not flight.task.done():
                    flight.shared_cancel.set()
                    flight.task.add_done_callback(
                        lambda task: self._on_flight_done(key, flight, task)
                    )
                else:
                    if self._flights.get(key) is flight:
                        self._flights.pop(key, None)
                    if not flight.task.cancelled() and flight.task.exception() is None:
                        cleanup_result = flight.task.result()
        if cleanup_result is not None:
            flight.cleanup(cleanup_result)

    def _on_flight_done(
        self,
        key: str,
        flight: _Flight[T],
        task: asyncio.Task[T],
    ) -> None:
        # Retrieve producer failures immediately.  The asynchronous cleanup
        # task may not get a scheduling turn during process shutdown, and an
        # unretrieved producer exception would otherwise generate a noisy
        # "Task exception was never retrieved" warning.
        if not task.cancelled():
            task.exception()
        asyncio.create_task(self._cleanup_finished(key, flight))

    async def _cleanup_finished(self, key: str, flight: _Flight[T]) -> None:
        cleanup_result: T | None = None
        async with self._lock:
            if flight.subscribers != 0:
                return
            if self._flights.get(key) is flight:
                self._flights.pop(key, None)
            if not flight.task.cancelled() and flight.task.exception() is None:
                cleanup_result = flight.task.result()
        if cleanup_result is not None:
            flight.cleanup(cleanup_result)

    @property
    def active_count(self) -> int:
        return sum(1 for flight in self._flights.values() if not flight.task.done())
