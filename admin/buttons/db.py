"""
Слой доступа к данным для Inline Button Manager.

Собственная таблица (inline_buttons), собственная init-функция.
scope определяет, где кнопка показывается — сейчас единственный scope
"downloads" (под видео, отправленным пользователю), но поле заложено
как расширяемое, не хардкожено в один-единственный вызов.

position — целое число, порядок по возрастанию. Перестановка (⬆️/⬇️)
меняется местами с соседней кнопкой, без пересчёта всех позиций.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from app.database import get_connection


def init_buttons_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inline_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                url TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'downloads',
                position INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_inline_buttons_scope
            ON inline_buttons(scope, position)
            """
        )
        conn.commit()


def create_button(text: str, url: str, scope: str = "downloads") -> int:
    with get_connection() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM inline_buttons WHERE scope = ?",
            (scope,),
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO inline_buttons (text, url, scope, position, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            (text, url, scope, max_pos + 1),
        )
        conn.commit()
        return cur.lastrowid


def get_button(button_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM inline_buttons WHERE id = ?", (button_id,)
        ).fetchone()


def list_buttons(scope: str = "downloads", enabled_only: bool = False) -> list[sqlite3.Row]:
    with get_connection() as conn:
        if enabled_only:
            return conn.execute(
                """
                SELECT * FROM inline_buttons
                WHERE scope = ? AND enabled = 1
                ORDER BY position
                """,
                (scope,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM inline_buttons WHERE scope = ? ORDER BY position",
            (scope,),
        ).fetchall()


def update_button(button_id: int, *, text: Optional[str] = None, url: Optional[str] = None) -> None:
    current = get_button(button_id)
    if current is None:
        return
    new_text = text if text is not None else current["text"]
    new_url = url if url is not None else current["url"]
    with get_connection() as conn:
        conn.execute(
            "UPDATE inline_buttons SET text = ?, url = ? WHERE id = ?",
            (new_text, new_url, button_id),
        )
        conn.commit()


def set_button_enabled(button_id: int, enabled: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE inline_buttons SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, button_id),
        )
        conn.commit()


def delete_button(button_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM inline_buttons WHERE id = ? AND system_key IS NULL",
            (button_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def move_button(button_id: int, direction: str) -> bool:
    """
    direction: "up" (меньше position, выше в списке) или "down".
    Меняет местами position с соседней кнопкой в том же scope.
    Возвращает False, если двигать некуда (уже крайняя).
    """
    assert direction in ("up", "down")
    current = get_button(button_id)
    if current is None:
        return False

    with get_connection() as conn:
        if direction == "up":
            neighbor = conn.execute(
                """
                SELECT * FROM inline_buttons
                WHERE scope = ? AND position < ?
                ORDER BY position DESC LIMIT 1
                """,
                (current["scope"], current["position"]),
            ).fetchone()
        else:
            neighbor = conn.execute(
                """
                SELECT * FROM inline_buttons
                WHERE scope = ? AND position > ?
                ORDER BY position ASC LIMIT 1
                """,
                (current["scope"], current["position"]),
            ).fetchone()

        if neighbor is None:
            return False

        conn.execute(
            "UPDATE inline_buttons SET position = ? WHERE id = ?",
            (neighbor["position"], current["id"]),
        )
        conn.execute(
            "UPDATE inline_buttons SET position = ? WHERE id = ?",
            (current["position"], neighbor["id"]),
        )
        conn.commit()
    return True
