"""Telegram-backed media cache with exact-quality lookup and failover."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile

from app.database import get_connection, log_event
from app.runtime_config import RuntimeConfig
from app.services.downloader import DownloadResult
from app.services.telegram import telegram_retry

logger = logging.getLogger(__name__)


class CacheReferenceFailure(Enum):
    PERMANENT = "permanent"
    TEMPORARY = "temporary"
    RECIPIENT = "recipient"


PERMANENT_REFERENCE_MARKERS = (
    "wrong file identifier",
    "file_id_invalid",
    "invalid file_id",
    "wrong remote file identifier specified",
    "media not found",
)


def classify_cache_delivery_error(exc: Exception) -> CacheReferenceFailure:
    if isinstance(exc, TelegramForbiddenError):
        return CacheReferenceFailure.RECIPIENT
    if isinstance(exc, (TelegramRetryAfter, TelegramNetworkError, TelegramServerError)):
        return CacheReferenceFailure.TEMPORARY
    if isinstance(exc, TelegramBadRequest):
        message = str(exc).lower()
        if any(marker in message for marker in PERMANENT_REFERENCE_MARKERS):
            return CacheReferenceFailure.PERMANENT
    return CacheReferenceFailure.TEMPORARY


@dataclass(slots=True)
class CacheWriteResult:
    file_id: str
    entry_id: int
    storage_id: int


class StorageService:
    def __init__(self, runtime: RuntimeConfig):
        self.runtime = runtime
        self.alert_service = None

    def seed_legacy_channels(self, channels: dict[str, int]) -> None:
        with get_connection() as conn:
            for storage_type, chat_id in channels.items():
                conn.execute(
                    """INSERT OR IGNORE INTO cache_storages
                       (storage_type,telegram_chat_id,title,role,enabled,healthy)
                       VALUES (?,?,?,'primary',1,0)""",
                    (storage_type, chat_id, "Imported from environment"),
                )
            conn.commit()

    def tier_for_size(self, filesize: int) -> str:
        mb = filesize / 1024 / 1024
        if mb < int(self.runtime.get("small_tier_max_mb")):
            return "small"
        if mb <= int(self.runtime.get("medium_tier_max_mb")):
            return "medium"
        return "large"

    def cache_entries(self, content_id: str, quality: int) -> list:
        with get_connection() as conn:
            return conn.execute(
                """SELECT ce.*,cs.enabled AS storage_enabled,cs.healthy AS storage_healthy
                   FROM cache_entries ce
                   LEFT JOIN cache_storages cs ON cs.id=ce.storage_id
                   WHERE ce.content_id=? AND ce.quality=?
                   ORDER BY COALESCE(cs.healthy,1) DESC,ce.last_used DESC""",
                (content_id, quality),
            ).fetchall()

    def touch_entry(self, entry_id: int) -> None:
        with get_connection() as conn:
            conn.execute("UPDATE cache_entries SET last_used=CURRENT_TIMESTAMP WHERE id=?", (entry_id,))
            conn.commit()

    def delete_permanently_stale(self, entry_id: int, *, reason: str) -> None:
        with get_connection() as conn:
            row = conn.execute("SELECT content_id,quality FROM cache_entries WHERE id=?", (entry_id,)).fetchone()
            conn.execute("DELETE FROM cache_entries WHERE id=?", (entry_id,))
            conn.commit()
        if row:
            log_event("cache_stale_deleted", target=f"{row['content_id']}:{row['quality']}", level="warning", metadata={"reason": reason[:200]})

    def list_storages(self, *, storage_type: str | None = None, enabled_only: bool = False) -> list:
        where: list[str] = []
        params: list = []
        if storage_type:
            where.append("storage_type=?")
            params.append(storage_type)
        if enabled_only:
            where.append("enabled=1")
        clause = " WHERE " + " AND ".join(where) if where else ""
        with get_connection() as conn:
            return conn.execute(
                f"""SELECT cs.*,
                    (SELECT COUNT(*) FROM cache_entries ce WHERE ce.storage_id=cs.id) AS entry_count,
                    (SELECT COALESCE(SUM(filesize),0) FROM cache_entries ce WHERE ce.storage_id=cs.id) AS total_bytes
                    FROM cache_storages cs{clause}
                    ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END,id""",
                params,
            ).fetchall()

    def get_storage(self, storage_id: int):
        with get_connection() as conn:
            return conn.execute(
                """SELECT cs.*,
                    (SELECT COUNT(*) FROM cache_entries ce WHERE ce.storage_id=cs.id) AS entry_count,
                    (SELECT COALESCE(SUM(filesize),0) FROM cache_entries ce WHERE ce.storage_id=cs.id) AS total_bytes
                   FROM cache_storages cs WHERE cs.id=?""",
                (storage_id,),
            ).fetchone()

    def add_storage(self, storage_type: str, chat_id: int, title: str, role: str, *, enabled: bool, healthy: bool, permissions: dict | None = None) -> int:
        if storage_type not in {"small", "medium", "large", "adult"} or role not in {"primary", "backup"}:
            raise ValueError("invalid storage type or role")
        with get_connection() as conn:
            if role == "primary":
                conn.execute(
                    "UPDATE cache_storages SET role='backup',updated_at=CURRENT_TIMESTAMP WHERE storage_type=? AND role='primary' AND telegram_chat_id<>?",
                    (storage_type, chat_id),
                )
            cur = conn.execute(
                """INSERT INTO cache_storages
                   (storage_type,telegram_chat_id,title,role,enabled,healthy,bot_permissions)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(telegram_chat_id) DO UPDATE SET
                     storage_type=excluded.storage_type,title=excluded.title,role=excluded.role,
                     enabled=excluded.enabled,healthy=excluded.healthy,
                     bot_permissions=excluded.bot_permissions,updated_at=CURRENT_TIMESTAMP""",
                (storage_type, chat_id, title, role, int(enabled), int(healthy), json.dumps(permissions or {})),
            )
            conn.commit()
            row = conn.execute("SELECT id FROM cache_storages WHERE telegram_chat_id=?", (chat_id,)).fetchone()
            return int(row["id"] if row else cur.lastrowid)

    def set_enabled(self, storage_id: int, enabled: bool) -> bool:
        with get_connection() as conn:
            cur = conn.execute("UPDATE cache_storages SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(enabled), storage_id))
            conn.commit()
            return cur.rowcount > 0

    def set_role(self, storage_id: int, role: str) -> bool:
        if role not in {"primary", "backup"}:
            raise ValueError("invalid role")
        with get_connection() as conn:
            row = conn.execute("SELECT storage_type FROM cache_storages WHERE id=?", (storage_id,)).fetchone()
            if row is None:
                return False
            if role == "primary":
                conn.execute(
                    "UPDATE cache_storages SET role='backup',updated_at=CURRENT_TIMESTAMP WHERE storage_type=? AND role='primary' AND id<>?",
                    (row["storage_type"], storage_id),
                )
            cur = conn.execute("UPDATE cache_storages SET role=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (role, storage_id))
            conn.commit()
            return cur.rowcount > 0

    def delete_storage_config(self, storage_id: int) -> bool:
        # Historical cache rows retain chat/message/file IDs; storage_id becomes
        # NULL through the FK so still-valid Telegram file IDs remain reusable.
        with get_connection() as conn:
            cur = conn.execute("DELETE FROM cache_storages WHERE id=?", (storage_id,))
            conn.commit()
            return cur.rowcount > 0

    def mark_storage_error(self, storage_id: int, error: Exception | str) -> None:
        detail = str(error)[:500]
        with get_connection() as conn:
            conn.execute(
                """UPDATE cache_storages SET healthy=0,last_error=?,last_error_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (detail, storage_id),
            )
            conn.commit()
        log_event("storage_failure", target=str(storage_id), level="error", metadata={"error": detail})

    def mark_storage_success(self, storage_id: int) -> None:
        with get_connection() as conn:
            old = conn.execute("SELECT healthy FROM cache_storages WHERE id=?", (storage_id,)).fetchone()
            conn.execute(
                """UPDATE cache_storages SET healthy=1,last_success_at=CURRENT_TIMESTAMP,
                   last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (storage_id,),
            )
            conn.commit()
        if old and not old["healthy"]:
            log_event("storage_recovered", target=str(storage_id))

    async def store(
        self,
        bot: Bot,
        result: DownloadResult,
        *,
        content_id: str,
        original_url: str,
    ) -> CacheWriteResult | None:
        tier = self.tier_for_size(result.filesize)
        storages = self.list_storages(storage_type=tier, enabled_only=True)
        for index, storage in enumerate(storages):
            try:
                message = await telegram_retry(
                    lambda storage=storage: bot.send_video(
                        storage["telegram_chat_id"],
                        video=FSInputFile(result.path),
                        caption=f"{result.title[:700]}\n{result.quality}p",
                        supports_streaming=True,
                    ),
                    max_attempts=2,
                )
                if not message.video:
                    raise RuntimeError("Telegram returned no video object")
                with get_connection() as conn:
                    cur = conn.execute(
                        """INSERT INTO cache_entries
                           (content_id,quality,source,original_url,title,duration,filesize,
                            storage_id,telegram_chat_id,telegram_message_id,telegram_file_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(content_id,quality,telegram_file_id) DO UPDATE SET
                             storage_id=excluded.storage_id,telegram_chat_id=excluded.telegram_chat_id,
                             telegram_message_id=excluded.telegram_message_id,last_used=CURRENT_TIMESTAMP""",
                        (content_id, result.quality, result.source, original_url, result.title,
                         result.duration, result.filesize, storage["id"], storage["telegram_chat_id"],
                         message.message_id, message.video.file_id),
                    )
                    conn.commit()
                    entry = conn.execute(
                        "SELECT id FROM cache_entries WHERE content_id=? AND quality=? AND telegram_file_id=?",
                        (content_id, result.quality, message.video.file_id),
                    ).fetchone()
                self.mark_storage_success(storage["id"])
                if index > 0:
                    log_event("storage_failover", target=str(storage["id"]), level="warning", metadata={"tier": tier})
                return CacheWriteResult(message.video.file_id, int(entry["id"] if entry else cur.lastrowid), storage["id"])
            except Exception as exc:
                logger.warning("Cache storage write failed storage_id=%s: %s", storage["id"], exc)
                self.mark_storage_error(storage["id"], exc)
                if self.alert_service is not None:
                    await self.alert_service.notify(
                        f"storage:{storage['id']}",
                        f"Хранилище #{storage['id']} недоступно; включён failover.",
                    )
                continue
        if storages and self.alert_service is not None:
            await self.alert_service.notify(
                f"storage-all:{tier}",
                f"Все хранилища уровня {tier.upper()} недоступны. Доставка пользователю продолжается напрямую, если возможна.",
            )
        return None

    def statistics(self) -> dict:
        with get_connection() as conn:
            totals = conn.execute("SELECT COUNT(*) entries,COALESCE(SUM(filesize),0) bytes FROM cache_entries").fetchone()
            stale = 0  # Permanently stale rows are deleted, not retained.
            failures = conn.execute("SELECT COUNT(*) FROM event_log WHERE event_type='storage_failure'").fetchone()[0]
            failovers = conn.execute("SELECT COUNT(*) FROM event_log WHERE event_type='storage_failover'").fetchone()[0]
        return {"entries": totals["entries"], "bytes": totals["bytes"], "stale": stale, "failures": failures, "failovers": failovers}
