"""Validated, persistent runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from app.config import BootstrapConfig
from app.database import get_connection, log_event

QUALITIES = ("auto", "360", "480", "720", "1080", "1440", "2160")


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    default: Any
    parser: Callable[[str], Any]
    validator: Callable[[Any], bool]
    label: str
    requires_restart: bool = False


def _bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected true/false")


def _quality(value: str) -> str:
    value = str(value).lower().removesuffix("p")
    if value not in QUALITIES:
        raise ValueError("unsupported quality")
    return value


class RuntimeConfig:
    def __init__(self, bootstrap: BootstrapConfig):
        self.bootstrap = bootstrap
        env = os.environ
        initial = lambda name, default: env.get(name, "").strip() or default
        integer = int
        number = float
        self.specs: dict[str, SettingSpec] = {
            "private_default_quality": SettingSpec("private_default_quality", initial("DEFAULT_QUALITY", "1080"), _quality, lambda v: v in QUALITIES, "Качество в ЛС"),
            "group_default_quality": SettingSpec("group_default_quality", initial("GROUP_DEFAULT_QUALITY", "1080"), _quality, lambda v: v in QUALITIES, "Качество в группах"),
            "max_concurrent_downloads": SettingSpec("max_concurrent_downloads", initial("MAX_CONCURRENT_DOWNLOADS", "2"), integer, lambda v: 1 <= v <= 64, "Параллельные загрузки", True),
            "queue_workers": SettingSpec("queue_workers", initial("QUEUE_WORKERS", "2"), integer, lambda v: 1 <= v <= 64, "Воркеры очереди", True),
            "max_batch_size": SettingSpec("max_batch_size", initial("MAX_BATCH_SIZE", "6"), integer, lambda v: 1 <= v <= 20, "Ссылок в сообщении"),
            "max_user_queue": SettingSpec("max_user_queue", initial("MAX_USER_QUEUE", "6"), integer, lambda v: 1 <= v <= 50, "Очередь пользователя"),
            "min_message_interval": SettingSpec("min_message_interval", initial("MIN_MESSAGE_INTERVAL", "9"), number, lambda v: 0 <= v <= 3600, "Интервал сообщений"),
            "small_tier_max_mb": SettingSpec("small_tier_max_mb", initial("SMALL_TIER_MAX_MB", "500"), integer, lambda v: 1 <= v <= 1900, "Граница SMALL, MB"),
            "medium_tier_max_mb": SettingSpec("medium_tier_max_mb", initial("MEDIUM_TIER_MAX_MB", "1024"), integer, lambda v: 2 <= v <= 1950, "Граница MEDIUM, MB"),
            "max_delivery_mb": SettingSpec("max_delivery_mb", initial("MAX_DELIVERY_MB", str(bootstrap.transport_limit_mb)), integer, lambda v: 1 <= v <= bootstrap.transport_limit_mb, "Лимит доставки, MB"),
            "video_note_enabled": SettingSpec("video_note_enabled", initial("VIDEO_NOTE_ENABLED", "true"), _bool, lambda v: isinstance(v, bool), "Кружочки"),
            "quality_selection_enabled": SettingSpec("quality_selection_enabled", initial("QUALITY_SELECTION_ENABLED", "true"), _bool, lambda v: isinstance(v, bool), "Выбор качества"),
            "maintenance_mode": SettingSpec("maintenance_mode", initial("MAINTENANCE_MODE", "false"), _bool, lambda v: isinstance(v, bool), "Обслуживание"),
            "temp_retention_minutes": SettingSpec("temp_retention_minutes", initial("TEMP_RETENTION_MINUTES", "60"), integer, lambda v: 5 <= v <= 10080, "Хранение temp, мин"),
            "video_note_source_ttl_minutes": SettingSpec("video_note_source_ttl_minutes", initial("VIDEO_NOTE_SOURCE_TTL_MINUTES", "30"), integer, lambda v: 5 <= v <= 1440, "Источник кружочка, мин"),
            "event_retention_days": SettingSpec("event_retention_days", initial("EVENT_RETENTION_DAYS", "90"), integer, lambda v: 7 <= v <= 3650, "Журнал, дней"),
            "download_history_retention_days": SettingSpec("download_history_retention_days", initial("DOWNLOAD_HISTORY_RETENTION_DAYS", "365"), integer, lambda v: 30 <= v <= 3650, "История загрузок, дней"),
            "disk_min_free_mb": SettingSpec("disk_min_free_mb", initial("DISK_MIN_FREE_MB", "1024"), integer, lambda v: 128 <= v <= 102400, "Резерв диска, MB"),
            "operational_alerts": SettingSpec("operational_alerts", initial("OPERATIONAL_ALERTS", "true"), _bool, lambda v: isinstance(v, bool), "Операционные alerts"),
        }
        self._cache: dict[str, Any] = {}

    def seed_defaults(self) -> None:
        with get_connection() as conn:
            for key, spec in self.specs.items():
                parsed = self._parse_and_validate(spec, str(spec.default))
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_settings(key,value) VALUES (?,?)",
                    (key, self._serialize(parsed)),
                )
            conn.execute(
                """UPDATE runtime_settings SET value=?,updated_at=CURRENT_TIMESTAMP
                   WHERE key='max_delivery_mb' AND CAST(value AS INTEGER)>?""",
                (str(self.bootstrap.transport_limit_mb), self.bootstrap.transport_limit_mb),
            )
            conn.commit()
        self.reload()
        self._validate_cross_fields(self._cache)

    def reload(self) -> None:
        with get_connection() as conn:
            rows = conn.execute("SELECT key,value FROM runtime_settings").fetchall()
        cache: dict[str, Any] = {}
        for row in rows:
            spec = self.specs.get(row["key"])
            if spec:
                cache[row["key"]] = self._parse_and_validate(spec, row["value"])
        self._cache = cache

    def get(self, key: str) -> Any:
        if key not in self.specs:
            raise KeyError(key)
        if key not in self._cache:
            self.reload()
        if key in self._cache:
            return self._cache[key]
        return self._parse_and_validate(self.specs[key], str(self.specs[key].default))

    def set(self, key: str, value: Any, *, actor_id: int | None = None) -> Any:
        spec = self.specs.get(key)
        if spec is None:
            raise KeyError(key)
        parsed = self._parse_and_validate(spec, str(value))
        candidate = dict(self._cache)
        candidate[key] = parsed
        self._validate_cross_fields(candidate)
        old = self.get(key)
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO runtime_settings(key,value,updated_by,updated_at)
                   VALUES (?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
                (key, self._serialize(parsed), actor_id),
            )
            conn.commit()
        self._cache[key] = parsed
        log_event("setting_changed", actor_id=actor_id, target=key, metadata={"old": old, "new": parsed, "requires_restart": spec.requires_restart})
        return parsed

    def all(self) -> list[tuple[SettingSpec, Any]]:
        return [(spec, self.get(key)) for key, spec in self.specs.items()]

    def _parse_and_validate(self, spec: SettingSpec, raw: str) -> Any:
        try:
            value = spec.parser(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid value for {spec.key}: {raw!r}") from exc
        if not spec.validator(value):
            raise ValueError(f"Value out of range for {spec.key}: {value!r}")
        return value

    @staticmethod
    def _serialize(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _validate_cross_fields(self, values: dict[str, Any]) -> None:
        small = int(values.get("small_tier_max_mb", 500))
        medium = int(values.get("medium_tier_max_mb", 1024))
        if small >= medium:
            raise ValueError("SMALL boundary must be lower than MEDIUM boundary")


_runtime: RuntimeConfig | None = None


def init_runtime_config(bootstrap: BootstrapConfig) -> RuntimeConfig:
    global _runtime
    _runtime = RuntimeConfig(bootstrap)
    _runtime.seed_defaults()
    return _runtime


def runtime_config() -> RuntimeConfig:
    if _runtime is None:
        raise RuntimeError("Runtime configuration is not initialized")
    return _runtime
