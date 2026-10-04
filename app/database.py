"""SQLite connection and ordered, non-destructive schema migrations."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)
_db_path = Path("./data/bot.db")


def set_database_path(path: str | Path) -> None:
    global _db_path
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)


def get_database_path() -> Path:
    return _db_path


def get_connection() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migration_001_legacy_core(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS video_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id TEXT NOT NULL,
            source TEXT,
            original_url TEXT,
            quality INTEGER,
            filesize INTEGER,
            cache_type TEXT NOT NULL DEFAULT 'small',
            telegram_chat_id INTEGER,
            telegram_message_id INTEGER,
            telegram_file_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(content_id, quality)
        );
        CREATE INDEX IF NOT EXISTS idx_video_cache_content_id ON video_cache(content_id);
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK(role IN ('owner','admin','moderator','broadcaster')),
            added_by INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            language_code TEXT,
            first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            downloads_total INTEGER NOT NULL DEFAULT 0,
            downloads_success INTEGER NOT NULL DEFAULT 0,
            downloads_failed INTEGER NOT NULL DEFAULT 0,
            cache_hits INTEGER NOT NULL DEFAULT 0,
            cache_misses INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            title TEXT,
            username TEXT,
            first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            downloads_total INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS admin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_admin_actions_created ON admin_actions(created_at DESC);
        """
    )
    _add_column(conn, "users", "cache_misses INTEGER NOT NULL DEFAULT 0")


def _migration_002_settings_jobs(conn: sqlite3.Connection) -> None:
    for definition in (
        "locale TEXT NOT NULL DEFAULT 'ru'",
        "locale_manual INTEGER NOT NULL DEFAULT 0",
        "default_quality TEXT NOT NULL DEFAULT 'auto'",
        "quality_manual INTEGER NOT NULL DEFAULT 0",
    ):
        _add_column(conn, "users", definition)
    for definition in (
        "locale TEXT NOT NULL DEFAULT 'ru'",
        "locale_manual INTEGER NOT NULL DEFAULT 0",
        "default_quality TEXT NOT NULL DEFAULT 'auto'",
        "quality_manual INTEGER NOT NULL DEFAULT 0",
        "downloads_success INTEGER NOT NULL DEFAULT 0",
        "downloads_failed INTEGER NOT NULL DEFAULT 0",
    ):
        _add_column(conn, "groups", definition)
    conn.execute(
        """UPDATE users SET locale=CASE
             WHEN lower(substr(language_code,1,2))='kk' THEN 'kk'
             WHEN lower(substr(language_code,1,2))='uz' THEN 'uz_latn'
             WHEN lower(substr(language_code,1,2))='en' THEN 'en'
             ELSE 'ru' END
           WHERE locale_manual=0"""
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_by INTEGER,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS download_jobs (
            id TEXT PRIMARY KEY,
            requester_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            requested_quality TEXT NOT NULL,
            effective_quality INTEGER,
            status TEXT NOT NULL DEFAULT 'queued',
            error_category TEXT,
            source TEXT,
            content_id TEXT,
            title TEXT,
            duration REAL,
            filesize INTEGER,
            cache_hit INTEGER NOT NULL DEFAULT 0,
            queue_wait_ms INTEGER,
            metadata_ms INTEGER,
            download_ms INTEGER,
            processing_ms INTEGER,
            upload_ms INTEGER,
            total_ms INTEGER,
            retry_of TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON download_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON download_jobs(requester_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_content_quality ON download_jobs(content_id, effective_quality);
        CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen DESC);
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        CREATE INDEX IF NOT EXISTS idx_groups_last_seen ON groups(last_seen DESC);
        CREATE INDEX IF NOT EXISTS idx_groups_username ON groups(username);
        """
    )


def _migration_003_cache_storage(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cache_storages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storage_type TEXT NOT NULL,
            telegram_chat_id INTEGER NOT NULL UNIQUE,
            title TEXT,
            role TEXT NOT NULL DEFAULT 'primary' CHECK(role IN ('primary','backup')),
            enabled INTEGER NOT NULL DEFAULT 1,
            healthy INTEGER NOT NULL DEFAULT 0,
            bot_permissions TEXT,
            last_success_at TEXT,
            last_error TEXT,
            last_error_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_storages_type_enabled
            ON cache_storages(storage_type, enabled, role);
        CREATE TABLE IF NOT EXISTS cache_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id TEXT NOT NULL,
            quality INTEGER NOT NULL,
            source TEXT,
            original_url TEXT,
            title TEXT,
            duration REAL,
            filesize INTEGER,
            storage_id INTEGER REFERENCES cache_storages(id) ON DELETE SET NULL,
            telegram_chat_id INTEGER,
            telegram_message_id INTEGER,
            telegram_file_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(content_id, quality, telegram_file_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cache_exact
            ON cache_entries(content_id, quality, last_used DESC);
        """
    )
    conn.execute(
        """INSERT OR IGNORE INTO cache_entries (
            content_id, quality, source, original_url, filesize,
            telegram_chat_id, telegram_message_id, telegram_file_id,
            created_at, last_used)
        SELECT content_id, COALESCE(quality, 0), source, original_url, filesize,
               telegram_chat_id, telegram_message_id, telegram_file_id,
               created_at, last_used
        FROM video_cache
        WHERE telegram_file_id IS NOT NULL AND COALESCE(quality, 0) > 0"""
    )


def _migration_004_delivery_events(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media_deliveries (
            token TEXT PRIMARY KEY,
            job_id TEXT,
            requester_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            content_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            quality INTEGER NOT NULL,
            telegram_file_id TEXT,
            local_path TEXT,
            duration REAL,
            note_status TEXT NOT NULL DEFAULT 'ready',
            note_file_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deliveries_expiry ON media_deliveries(expires_at);
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            actor_id INTEGER,
            target TEXT,
            level TEXT NOT NULL DEFAULT 'info',
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_event_log_created ON event_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_event_log_type ON event_log(event_type, created_at DESC);
        CREATE TABLE IF NOT EXISTS operational_alerts (
            alert_key TEXT PRIMARY KEY,
            last_sent_at TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1
        );
        """
    )


def _migration_005_broadcast_buttons(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inline_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            url TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'downloads',
            position INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_inline_buttons_scope ON inline_buttons(scope, position);
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL,
            target_filter TEXT NOT NULL,
            message_payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            total INTEGER NOT NULL DEFAULT 0,
            sent INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS broadcast_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id),
            target_chat_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_broadcast_targets_broadcast
            ON broadcast_targets(broadcast_id, status);
        """
    )
    _add_column(conn, "inline_buttons", "system_key TEXT")
    conn.execute(
        """UPDATE inline_buttons SET system_key='add_to_group'
           WHERE system_key IS NULL AND text='➕ Добавить бота в группу'"""
    )


def _migration_006_metadata_and_flags(conn: sqlite3.Connection) -> None:
    _add_column(conn, "users", "quality_manual INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "groups", "quality_manual INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "inline_buttons", "system_key TEXT")
    _add_column(conn, "download_jobs", "available_qualities TEXT")
    conn.execute(
        """UPDATE inline_buttons SET system_key='add_to_group'
           WHERE system_key IS NULL AND text='➕ Добавить бота в группу'"""
    )


MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "legacy core tables", _migration_001_legacy_core),
    (2, "user settings and download jobs", _migration_002_settings_jobs),
    (3, "multi-copy cache and storage manager", _migration_003_cache_storage),
    (4, "media deliveries and event log", _migration_004_delivery_events),
    (5, "broadcast and inline button tables", _migration_005_broadcast_buttons),
    (6, "metadata and explicit setting flags", _migration_006_metadata_and_flags),
)


def migrate() -> list[int]:
    applied: list[int] = []
    with get_connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        current = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, name, migration in MIGRATIONS:
            if version in current:
                continue
            try:
                migration(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                    (version, name),
                )
                conn.commit()
                applied.append(version)
                logger.info("Applied database migration %s: %s", version, name)
            except Exception:
                conn.rollback()
                logger.exception("Database migration %s failed", version)
                raise
    return applied


def init_db() -> list[int]:
    return migrate()


def log_event(
    event_type: str,
    *, actor_id: int | None = None,
    target: str | None = None,
    level: str = "info",
    metadata: dict | None = None,
) -> None:
    safe_metadata = json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO event_log(event_type,actor_id,target,level,metadata) VALUES (?,?,?,?,?)",
            (event_type, actor_id, target, level, safe_metadata),
        )
        conn.commit()


# Compatibility wrappers; canonical cache operations live in
# app.services.storage.
def get_cached_video(content_id: str, quality: int):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM cache_entries WHERE content_id=? AND quality=? ORDER BY last_used DESC LIMIT 1",
            (content_id, quality),
        ).fetchone()


def save_cached_video(
    content_id: str,
    source: str,
    original_url: str,
    quality: int,
    filesize: int,
    cache_type: str,
    telegram_chat_id: int,
    telegram_message_id: int,
    telegram_file_id: str,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO video_cache
               (content_id,source,original_url,quality,filesize,cache_type,
                telegram_chat_id,telegram_message_id,telegram_file_id,last_used)
               VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (content_id, source, original_url, quality, filesize, cache_type,
             telegram_chat_id, telegram_message_id, telegram_file_id),
        )
        conn.commit()
