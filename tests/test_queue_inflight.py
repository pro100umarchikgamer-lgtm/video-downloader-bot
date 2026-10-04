from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.errors import DownloadCancelled
from app.services.inflight import InflightCoordinator
from app.services.queue import DownloadQueue, QueueItem


def item(job: str, user: int, url: str | None = None) -> QueueItem:
    return QueueItem(job, user, user, url or f"https://example.com/{job}", "auto", "en")


@pytest.mark.asyncio
async def test_round_robin_queue_fairness(configured_db):
    _, runtime = configured_db
    queue = DownloadQueue()
    queue.configure(runtime)
    assert (await queue.can_add(1, 2))[0]
    assert (await queue.can_add(2, 1))[0]
    await queue.put(item("a", 1))
    await queue.put(item("b", 1))
    await queue.put(item("c", 2))
    assert (await queue.get()).job_id == "a"
    assert (await queue.get()).job_id == "c"
    assert (await queue.get()).job_id == "b"


@pytest.mark.asyncio
async def test_queue_counters_release_on_cancel_and_failure(configured_db):
    _, runtime = configured_db
    queue = DownloadQueue()
    queue.configure(runtime)
    assert (await queue.can_add(7, 1))[0]
    await queue.put(item("queued", 7))
    assert queue.user_count(7) == 1
    assert await queue.cancel("queued", 7) == "queued"
    assert queue.user_count(7) == 0 and queue.queued_count == 0

    assert (await queue.can_add(7, 2))[0]
    await queue.put(item("fail", 7))
    await queue.put(item("next", 7))
    seen: list[str] = []
    finished = asyncio.Event()

    async def handler(queue_item):
        seen.append(queue_item.job_id)
        if queue_item.job_id == "fail":
            raise RuntimeError("one bad job")
        finished.set()

    task = asyncio.create_task(queue.run(handler))
    await asyncio.wait_for(finished.wait(), 2)
    await queue.stop()
    await asyncio.wait_for(task, 2)
    assert seen == ["fail", "next"]
    assert queue.user_count(7) == 0


@pytest.mark.asyncio
async def test_rate_limit_is_temporary_and_configurable(configured_db):
    _, runtime = configured_db
    runtime.set("min_message_interval", 0.05)
    queue = DownloadQueue()
    queue.configure(runtime)
    assert await queue.check_message_allowed(1) == (True, 0)
    allowed, remaining = await queue.check_message_allowed(1)
    assert not allowed and remaining >= 1
    await asyncio.sleep(0.06)
    assert await queue.check_message_allowed(1) == (True, 0)


@dataclass
class Artifact:
    value: int


@pytest.mark.asyncio
async def test_inflight_deduplicates_and_one_cancel_does_not_break_other():
    coordinator = InflightCoordinator[Artifact]()
    gate = asyncio.Event()
    first_cancel = asyncio.Event()
    second_cancel = asyncio.Event()
    calls = 0
    cleaned: list[int] = []

    async def factory(shared_cancel):
        nonlocal calls
        calls += 1
        await gate.wait()
        assert not shared_cancel.is_set()
        return Artifact(42)

    async def consumer(cancel_event):
        async with coordinator.acquire("same:1080", factory, cancel_event, lambda value: cleaned.append(value.value)) as value:
            return value.value

    first = asyncio.create_task(consumer(first_cancel))
    second = asyncio.create_task(consumer(second_cancel))
    await asyncio.sleep(0)
    first_cancel.set()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(DownloadCancelled):
        await first
    assert await second == 42
    await asyncio.sleep(0)
    assert calls == 1
    assert cleaned == [42]


@pytest.mark.asyncio
async def test_last_subscriber_cancel_stops_shared_factory():
    coordinator = InflightCoordinator[Artifact]()
    cancel = asyncio.Event()
    factory_stopped = asyncio.Event()

    async def factory(shared_cancel):
        await shared_cancel.wait()
        factory_stopped.set()
        raise DownloadCancelled()

    async def consumer():
        async with coordinator.acquire("one", factory, cancel, lambda _: None):
            pass

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    cancel.set()
    with pytest.raises(DownloadCancelled):
        await task
    await asyncio.wait_for(factory_stopped.wait(), 1)
