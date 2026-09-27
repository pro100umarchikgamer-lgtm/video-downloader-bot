import os
import sqlite3
from pathlib import Path
from typing import Optional


DB_PATH = Path(os.getenv("DATABASE_PATH", "./data/bot.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id TEXT NOT NULL,
                source TEXT,
                original_url TEXT,
                quality INTEGER,
                filesize INTEGER,
                cache_type TEXT NOT NULL,
                telegram_chat_id INTEGER,
                telegram_message_id INTEGER,
                telegram_file_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(content_id, quality)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_video_cache_content_id
            ON video_cache(content_id)
            """
        )
        conn.commit()


def get_cached_video(
    content_id: str,
    quality: int,
) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM video_cache
            WHERE content_id = ? AND quality = ?
            LIMIT 1
            """,
            (content_id, quality),
        ).fetchone()

        if row:
            conn.execute(
                """
                UPDATE video_cache
                SET last_used = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (row["id"],),
            )
            conn.commit()

        return row


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
            """
            INSERT INTO video_cache (
                content_id,
                source,
                original_url,
                quality,
                filesize,
                cache_type,
                telegram_chat_id,
                telegram_message_id,
                telegram_file_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_id, quality)
            DO UPDATE SET
                filesize = excluded.filesize,
                cache_type = excluded.cache_type,
                telegram_chat_id = excluded.telegram_chat_id,
                telegram_message_id = excluded.telegram_message_id,
                telegram_file_id = excluded.telegram_file_id,
                last_used = CURRENT_TIMESTAMP
            """,
            (
                content_id,
                source,
                original_url,
                quality,
                filesize,
                cache_type,
                telegram_chat_id,
                telegram_message_id,
                telegram_file_id,
            ),
        )
        conn.commit()
