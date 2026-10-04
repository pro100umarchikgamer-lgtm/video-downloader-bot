from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendVideo

from app.database import get_connection
from app.services.downloader import DownloadResult
from app.services.storage import CacheReferenceFailure, StorageService, classify_cache_delivery_error


def _insert_cache(content: str, quality: int, file_id: str, storage_id: int | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO cache_entries
               (content_id,quality,telegram_file_id,storage_id,filesize)
               VALUES (?,?,?,?,?)""",
            (content, quality, file_id, storage_id, 100),
        )
        conn.commit()
        return cur.lastrowid


def test_cache_lookup_is_exact_quality(configured_db):
    _, runtime = configured_db
    service = StorageService(runtime)
    _insert_cache("youtube:x", 720, "F720")
    _insert_cache("youtube:x", 1080, "F1080")
    assert [row["telegram_file_id"] for row in service.cache_entries("youtube:x", 1080)] == ["F1080"]
    assert service.cache_entries("youtube:x", 1440) == []


def test_permanent_stale_entry_is_deleted_not_accumulated(configured_db):
    _, runtime = configured_db
    service = StorageService(runtime)
    entry_id = _insert_cache("youtube:x", 1080, "DEAD")
    service.delete_permanently_stale(entry_id, reason="wrong file identifier")
    assert service.cache_entries("youtube:x", 1080) == []
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_log WHERE event_type='cache_stale_deleted'").fetchone()[0] == 1


def test_cache_error_classification_temporary_vs_permanent():
    method = SendVideo(chat_id=1, video="x")
    permanent = TelegramBadRequest(method=method, message="Bad Request: wrong file identifier/HTTP URL specified")
    network = TelegramNetworkError(method=method, message="connection reset")
    flood = TelegramRetryAfter(method=method, message="retry", retry_after=1)
    ambiguous = TelegramBadRequest(method=method, message="failed to get HTTP URL content")
    assert classify_cache_delivery_error(permanent) is CacheReferenceFailure.PERMANENT
    assert classify_cache_delivery_error(network) is CacheReferenceFailure.TEMPORARY
    assert classify_cache_delivery_error(flood) is CacheReferenceFailure.TEMPORARY
    assert classify_cache_delivery_error(ambiguous) is CacheReferenceFailure.TEMPORARY


def test_tier_boundaries_are_runtime_configurable(configured_db):
    _, runtime = configured_db
    service = StorageService(runtime)
    assert service.tier_for_size(499 * 1024 * 1024) == "small"
    assert service.tier_for_size(500 * 1024 * 1024) == "medium"
    assert service.tier_for_size(1024 * 1024 * 1024) == "medium"
    assert service.tier_for_size(1025 * 1024 * 1024) == "large"
    runtime.set("small_tier_max_mb", 400)
    assert service.tier_for_size(450 * 1024 * 1024) == "medium"


class FakeStorageBot:
    def __init__(self, failures: set[int]):
        self.failures = failures
        self.calls: list[int] = []

    async def send_video(self, chat_id, **kwargs):
        self.calls.append(chat_id)
        if chat_id in self.failures:
            raise RuntimeError("storage unavailable")
        return SimpleNamespace(
            message_id=77,
            video=SimpleNamespace(file_id=f"FILE:{chat_id}"),
        )


@pytest.mark.asyncio
async def test_primary_to_backup_failover(configured_db, tmp_path):
    _, runtime = configured_db
    service = StorageService(runtime)
    primary = service.add_storage("small", -100, "primary", "primary", enabled=True, healthy=True)
    backup = service.add_storage("small", -200, "backup", "backup", enabled=True, healthy=True)
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    result = DownloadResult(str(media), "Title", 1080, media.stat().st_size, 10, "Youtube", 20, 0)
    bot = FakeStorageBot({-100})
    stored = await service.store(bot, result, content_id="youtube:x", original_url="https://example.com/x")
    assert bot.calls == [-100, -200]
    assert stored and stored.storage_id == backup
    assert service.get_storage(primary)["healthy"] == 0
    assert service.get_storage(backup)["healthy"] == 1
    assert service.cache_entries("youtube:x", 1080)[0]["telegram_file_id"] == "FILE:-200"
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_log WHERE event_type='storage_failover'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_all_storage_failure_does_not_raise(configured_db, tmp_path):
    _, runtime = configured_db
    service = StorageService(runtime)
    service.add_storage("small", -100, "primary", "primary", enabled=True, healthy=True)
    service.add_storage("small", -200, "backup", "backup", enabled=True, healthy=True)
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    result = DownloadResult(str(media), "Title", 720, media.stat().st_size, 10, "Youtube", 20, 0)
    assert await service.store(FakeStorageBot({-100, -200}), result, content_id="youtube:y", original_url="https://example.com/y") is None
    assert service.cache_entries("youtube:y", 720) == []


def test_storage_replacement_preserves_historical_cache(configured_db):
    _, runtime = configured_db
    service = StorageService(runtime)
    old_id = service.add_storage("small", -100, "old", "primary", enabled=True, healthy=True)
    entry_id = _insert_cache("youtube:x", 720, "OLD_VALID", old_id)
    service.add_storage("small", -200, "new", "primary", enabled=True, healthy=True)
    service.set_enabled(old_id, False)
    assert service.cache_entries("youtube:x", 720)[0]["telegram_file_id"] == "OLD_VALID"
    service.delete_storage_config(old_id)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM cache_entries WHERE id=?", (entry_id,)).fetchone()
    assert row is not None and row["storage_id"] is None
