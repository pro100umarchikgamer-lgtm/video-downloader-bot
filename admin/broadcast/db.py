"""
Слой доступа к данным для системы рассылок.

Собственные таблицы (broadcasts, broadcast_targets), собственная
init-функция. Использует тот же SQLite-файл, что и app/database.py
и admin/db.py, но ничего в них не меняет.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from app.database import get_connection

STATUSES = ("draft", "preparing", "running", "paused", "completed", "cancelled", "failed")
TARGET_STATUSES = ("pending", "sent", "failed", "blocked")


def init_broadcast_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                target_filter TEXT NOT NULL,
                message_payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft' CHECK(
                    status IN ('draft','preparing','running','paused',
                               'completed','cancelled','failed')
                ),
                total INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id),
                target_chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(
                    status IN ('pending','sent','failed','blocked')
                ),
                error TEXT,
                sent_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_broadcast_targets_broadcast
            ON broadcast_targets(broadcast_id, status)
            """
        )
        conn.commit()


# ---------------------------------------------------------------------
# broadcasts
# ---------------------------------------------------------------------

def create_broadcast(
    creator_id: int,
    target_filter: dict,
    message_payload: dict,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO broadcasts (creator_id, target_filter, message_payload, status)
            VALUES (?, ?, ?, 'draft')
            """,
            (creator_id, json.dumps(target_filter), json.dumps(message_payload)),
        )
        conn.commit()
        return cur.lastrowid


def get_broadcast(broadcast_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)
        ).fetchone()


def set_broadcast_status(broadcast_id: int, status: str) -> None:
    assert status in STATUSES
    extra = ""
    if status == "running":
        extra = ", started_at = COALESCE(started_at, CURRENT_TIMESTAMP)"
    elif status in ("completed", "cancelled", "failed"):
        extra = ", finished_at = CURRENT_TIMESTAMP"

    with get_connection() as conn:
        conn.execute(
            f"UPDATE broadcasts SET status = ? {extra} WHERE id = ?",
            (status, broadcast_id),
        )
        conn.commit()


def set_broadcast_total(broadcast_id: int, total: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE broadcasts SET total = ? WHERE id = ?",
            (total, broadcast_id),
        )
        conn.commit()


def increment_broadcast_counter(broadcast_id: int, field: str) -> None:
    assert field in ("sent", "failed", "blocked")
    with get_connection() as conn:
        conn.execute(
            f"UPDATE broadcasts SET {field} = {field} + 1 WHERE id = ?",
            (broadcast_id,),
        )
        conn.commit()


def list_broadcasts(status: Optional[str] = None, limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
    with get_connection() as conn:
        if status:
            return conn.execute(
                """
                SELECT * FROM broadcasts WHERE status = ?
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            ).fetchall()
        return conn.execute(
            """
            SELECT * FROM broadcasts
            ORDER BY created_at DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


def count_broadcasts(status: Optional[str] = None) -> int:
    with get_connection() as conn:
        if status:
            return conn.execute(
                "SELECT COUNT(*) FROM broadcasts WHERE status = ?", (status,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0]


# ---------------------------------------------------------------------
# broadcast_targets
# ---------------------------------------------------------------------

def add_targets(broadcast_id: int, chat_ids: list[int]) -> None:
    """
    Один executemany — не N отдельных INSERT. Статус по умолчанию
    'pending' для всех.
    """
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO broadcast_targets (broadcast_id, target_chat_id, status)
            VALUES (?, ?, 'pending')
            """,
            [(broadcast_id, chat_id) for chat_id in chat_ids],
        )
        conn.commit()


def get_pending_targets(broadcast_id: int, limit: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM broadcast_targets
            WHERE broadcast_id = ? AND status = 'pending'
            ORDER BY id
            LIMIT ?
            """,
            (broadcast_id, limit),
        ).fetchall()


def get_failed_targets(broadcast_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM broadcast_targets
            WHERE broadcast_id = ? AND status = 'failed'
            ORDER BY id
            """,
            (broadcast_id,),
        ).fetchall()


def set_target_status(target_id: int, status: str, error: Optional[str] = None) -> None:
    assert status in TARGET_STATUSES
    sent_at = "CURRENT_TIMESTAMP" if status == "sent" else "sent_at"
    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE broadcast_targets
            SET status = ?, error = ?, sent_at = {sent_at}
            WHERE id = ?
            """,
            (status, error, target_id),
        )
        conn.commit()


def reset_failed_targets_to_pending(broadcast_id: int) -> int:
    """
    Используется "Повторить ошибки": переводит failed -> pending для
    конкретной рассылки, не трогая sent/blocked. Возвращает число строк.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE broadcast_targets
            SET status = 'pending', error = NULL
            WHERE broadcast_id = ? AND status = 'failed'
            """,
            (broadcast_id,),
        )
        if cur.rowcount:
            conn.execute(
                "UPDATE broadcasts SET failed=MAX(0,failed-?) WHERE id=?",
                (cur.rowcount, broadcast_id),
            )
        conn.commit()
        return cur.rowcount


def count_targets_by_status(broadcast_id: int) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) as cnt FROM broadcast_targets
            WHERE broadcast_id = ?
            GROUP BY status
            """,
            (broadcast_id,),
        ).fetchall()
    result = {s: 0 for s in TARGET_STATUSES}
    for row in rows:
        result[row["status"]] = row["cnt"]
    return result


def has_pending_targets(broadcast_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM broadcast_targets
            WHERE broadcast_id = ? AND status = 'pending'
            LIMIT 1
            """,
            (broadcast_id,),
        ).fetchone()
    return row is not None
