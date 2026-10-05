"""Bootstrap configuration.

Only secrets and values needed before SQLite is available live here. Mutable
operational settings are stored in ``runtime_settings`` and are accessed via
``app.runtime_config.RuntimeConfig``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class ConfigurationError(RuntimeError):
    pass


def _csv_ints(value: str) -> frozenset[int]:
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as exc:
            raise ConfigurationError("ADMIN_IDS must contain integer Telegram IDs") from exc
    return frozenset(result)


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    bot_token: str
    database_path: Path
    data_dir: Path
    temp_dir: Path
    owner_ids: frozenset[int]
    admin_group_id: int | None
    telegram_api_base_url: str | None
    telegram_api_is_local: bool
    telegram_api_id: str | None
    telegram_api_hash: str | None
    log_level: str
    app_version: str
    legacy_cache_channels: dict[str, int]

    @property
    def transport_limit_mb(self) -> int:
        # Cloud Bot API accepts uploads up to 50 MB; Local Bot API up to
        # 2,000 MB. Headroom avoids claiming the exact protocol boundary.
        return 1950 if self.telegram_api_is_local else 49


def load_bootstrap_config(*, require_token: bool = True) -> BootstrapConfig:
    token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and not token:
        raise ConfigurationError("BOT_TOKEN is required")

    data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser()
    database_path = Path(os.getenv("DATABASE_PATH", str(data_dir / "bot.db"))).expanduser()
    temp_dir = Path(os.getenv("TEMP_DIR", str(data_dir / "tmp"))).expanduser()
    api_base = os.getenv("TELEGRAM_API_BASE_URL", "").strip().rstrip("/") or None
    local_flag = os.getenv("TELEGRAM_API_IS_LOCAL", "").strip().lower()
    is_local = bool(api_base) and local_flag not in {"0", "false", "no", "off"}

    channels: dict[str, int] = {}
    for tier, env_name in (
        ("small", "CACHE_SMALL"),
        ("medium", "CACHE_MEDIUM"),
        ("large", "CACHE_LARGE"),
        ("adult", "CACHE_ADULT"),
    ):
        value = _optional_int(env_name)
        if value is not None:
            channels[tier] = value

    database_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    return BootstrapConfig(
        bot_token=token,
        database_path=database_path,
        data_dir=data_dir,
        temp_dir=temp_dir,
        owner_ids=_csv_ints(os.getenv("ADMIN_IDS", "")),
        admin_group_id=_optional_int("ADMIN_GROUP_ID"),
        telegram_api_base_url=api_base,
        telegram_api_is_local=is_local,
        telegram_api_id=os.getenv("TELEGRAM_API_ID") or None,
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH") or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        app_version=os.getenv("APP_VERSION", "2.0.0"),
        legacy_cache_channels=channels,
    )


def validate() -> list[str]:
    """Compatibility helper used by older deployments/tests."""
    return [] if os.getenv("BOT_TOKEN", "").strip() else ["BOT_TOKEN"]
