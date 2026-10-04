from __future__ import annotations

from pathlib import Path

import pytest

from app.config import BootstrapConfig
from app.database import migrate, set_database_path
from app.runtime_config import RuntimeConfig


def make_bootstrap(tmp_path: Path, *, local: bool = False) -> BootstrapConfig:
    data = tmp_path / "data"
    temp = data / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    return BootstrapConfig(
        bot_token="123456:TEST_TOKEN_NOT_SECRET",
        database_path=data / "bot.db",
        data_dir=data,
        temp_dir=temp,
        owner_ids=frozenset({1}),
        admin_group_id=None,
        telegram_api_base_url="http://telegram-bot-api:8081" if local else None,
        telegram_api_is_local=local,
        telegram_api_id=None,
        telegram_api_hash=None,
        log_level="INFO",
        app_version="test",
        legacy_cache_channels={},
    )


@pytest.fixture
def configured_db(tmp_path, monkeypatch):
    for name in (
        "DEFAULT_QUALITY", "GROUP_DEFAULT_QUALITY", "MAX_CONCURRENT_DOWNLOADS",
        "QUEUE_WORKERS", "MAX_BATCH_SIZE", "MAX_USER_QUEUE",
        "MIN_MESSAGE_INTERVAL", "SMALL_TIER_MAX_MB", "MEDIUM_TIER_MAX_MB",
        "MAX_DELIVERY_MB", "VIDEO_NOTE_ENABLED", "QUALITY_SELECTION_ENABLED",
        "MAINTENANCE_MODE", "TEMP_RETENTION_MINUTES",
        "VIDEO_NOTE_SOURCE_TTL_MINUTES", "EVENT_RETENTION_DAYS",
        "DOWNLOAD_HISTORY_RETENTION_DAYS", "DISK_MIN_FREE_MB",
        "OPERATIONAL_ALERTS",
    ):
        monkeypatch.delenv(name, raising=False)
    bootstrap = make_bootstrap(tmp_path)
    set_database_path(bootstrap.database_path)
    migrate()
    runtime = RuntimeConfig(bootstrap)
    runtime.seed_defaults()
    return bootstrap, runtime
