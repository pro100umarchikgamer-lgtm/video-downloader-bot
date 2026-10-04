"""Shared persistence helpers for users, groups, admins and audit events."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from app.database import get_connection, log_event
from app.i18n import normalize_locale
from app.runtime_config import QUALITIES

ROLES = ("owner", "admin", "moderator", "broadcaster")


def init_admin_db() -> None:
    # Tables are managed by app.database.migrate(). Kept for compatibility.
    return None


def upsert_user(
    telegram_id: int,
    username: Optional[str],
    first_name: Optional[str],
    language_code: Optional[str],
) -> None:
    initial_locale = normalize_locale(language_code)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO users
               (telegram_id,username,first_name,language_code,locale,locale_manual)
               VALUES (?,?,?,?,?,0)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name,
                 language_code=excluded.language_code, last_seen=CURRENT_TIMESTAMP""",
            (telegram_id, username, first_name, language_code, initial_locale),
        )
        conn.commit()


def upsert_group(
    chat_id: int,
    chat_type: str,
    title: Optional[str],
    username: Optional[str],
    initial_language_code: Optional[str] = None,
) -> None:
    initial_locale = normalize_locale(initial_language_code)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO groups(chat_id,type,title,username,locale,locale_manual)
               VALUES (?,?,?,?,?,0)
               ON CONFLICT(chat_id) DO UPDATE SET
                 type=excluded.type,title=excluded.title,username=excluded.username,
                 last_seen=CURRENT_TIMESTAMP""",
            (chat_id, chat_type, title, username, initial_locale),
        )
        conn.commit()


def get_user(telegram_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()


def get_group(chat_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,)).fetchone()


def get_user_locale(telegram_id: int, hint: str | None = None) -> str:
    row = get_user(telegram_id)
    return row["locale"] if row else normalize_locale(hint)


def set_user_locale(telegram_id: int, locale: str) -> bool:
    if locale not in {"ru", "kk", "uz_latn", "uz_cyrl", "en"}:
        return False
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE users SET locale=?,locale_manual=1 WHERE telegram_id=?",
            (locale, telegram_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_user_quality(telegram_id: int, fallback: str = "auto") -> str:
    row = get_user(telegram_id)
    return row["default_quality"] if row and row["quality_manual"] and row["default_quality"] in QUALITIES else fallback


def set_user_quality(telegram_id: int, quality: str) -> bool:
    if quality not in QUALITIES:
        return False
    with get_connection() as conn:
        cur = conn.execute("UPDATE users SET default_quality=?,quality_manual=1 WHERE telegram_id=?", (quality, telegram_id))
        conn.commit()
        return cur.rowcount > 0


def get_group_locale(chat_id: int, fallback: str = "ru") -> str:
    row = get_group(chat_id)
    return row["locale"] if row else fallback


def set_group_locale(chat_id: int, locale: str) -> bool:
    if locale not in {"ru", "kk", "uz_latn", "uz_cyrl", "en"}:
        return False
    with get_connection() as conn:
        cur = conn.execute("UPDATE groups SET locale=?,locale_manual=1 WHERE chat_id=?", (locale, chat_id))
        conn.commit()
        return cur.rowcount > 0


def get_group_quality(chat_id: int, fallback: str = "auto") -> str:
    row = get_group(chat_id)
    return row["default_quality"] if row and row["quality_manual"] and row["default_quality"] in QUALITIES else fallback


def set_group_quality(chat_id: int, quality: str) -> bool:
    if quality not in QUALITIES:
        return False
    with get_connection() as conn:
        cur = conn.execute("UPDATE groups SET default_quality=?,quality_manual=1 WHERE chat_id=?", (quality, chat_id))
        conn.commit()
        return cur.rowcount > 0


def is_user_blocked(telegram_id: int) -> bool:
    row = get_user(telegram_id)
    return bool(row and row["blocked"])


def is_group_blocked(chat_id: int) -> bool:
    row = get_group(chat_id)
    return bool(row and row["blocked"])


def set_user_blocked(telegram_id: int, blocked: bool) -> bool:
    with get_connection() as conn:
        cur = conn.execute("UPDATE users SET blocked=? WHERE telegram_id=?", (int(blocked), telegram_id))
        conn.commit()
        return cur.rowcount > 0


def set_group_blocked(chat_id: int, blocked: bool) -> bool:
    with get_connection() as conn:
        cur = conn.execute("UPDATE groups SET blocked=? WHERE chat_id=?", (int(blocked), chat_id))
        conn.commit()
        return cur.rowcount > 0


def search_users(query: str, limit: int = 20) -> list[sqlite3.Row]:
    query = query.strip().lstrip("@")
    with get_connection() as conn:
        if query.lstrip("-").isdigit():
            return conn.execute("SELECT * FROM users WHERE telegram_id=? LIMIT ?", (int(query), limit)).fetchall()
        return conn.execute(
            """SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ?
               ORDER BY last_seen DESC LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()


def search_users_by_username(username: str, limit: int = 20) -> list[sqlite3.Row]:
    return search_users(username, limit)


def search_groups(query: str, limit: int = 20) -> list[sqlite3.Row]:
    query = query.strip().lstrip("@")
    with get_connection() as conn:
        if query.lstrip("-").isdigit():
            return conn.execute("SELECT * FROM groups WHERE chat_id=? LIMIT ?", (int(query), limit)).fetchall()
        return conn.execute(
            """SELECT * FROM groups WHERE title LIKE ? OR username LIKE ?
               ORDER BY last_seen DESC LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()


def list_users(limit: int, offset: int, *, blocked: bool | None = None) -> list[sqlite3.Row]:
    where = "" if blocked is None else "WHERE blocked=?"
    params: tuple = (limit, offset) if blocked is None else (int(blocked), limit, offset)
    with get_connection() as conn:
        return conn.execute(f"SELECT * FROM users {where} ORDER BY last_seen DESC LIMIT ? OFFSET ?", params).fetchall()


def list_groups(limit: int, offset: int, *, blocked: bool | None = None) -> list[sqlite3.Row]:
    where = "" if blocked is None else "WHERE blocked=?"
    params: tuple = (limit, offset) if blocked is None else (int(blocked), limit, offset)
    with get_connection() as conn:
        return conn.execute(f"SELECT * FROM groups {where} ORDER BY last_seen DESC LIMIT ? OFFSET ?", params).fetchall()


def count_users(*, blocked: bool | None = None) -> int:
    with get_connection() as conn:
        if blocked is None:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM users WHERE blocked=?", (int(blocked),)).fetchone()[0]


def count_groups(*, blocked: bool | None = None) -> int:
    with get_connection() as conn:
        if blocked is None:
            return conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM groups WHERE blocked=?", (int(blocked),)).fetchone()[0]


def get_admin_role_from_table(telegram_id: int) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT role FROM admins WHERE telegram_id=? AND enabled=1", (telegram_id,)).fetchone()
        return row["role"] if row else None


def get_admin(telegram_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM admins WHERE telegram_id=?", (telegram_id,)).fetchone()


def list_admins() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM admins ORDER BY created_at").fetchall()


def add_admin(telegram_id: int, role: str, added_by: int) -> None:
    if role not in ROLES:
        raise ValueError("invalid role")
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO admins(telegram_id,role,added_by,enabled) VALUES (?,?,?,1)
               ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role,enabled=1""",
            (telegram_id, role, added_by),
        )
        conn.commit()


def set_admin_role(telegram_id: int, role: str) -> bool:
    if role not in ROLES:
        raise ValueError("invalid role")
    with get_connection() as conn:
        cur = conn.execute("UPDATE admins SET role=? WHERE telegram_id=?", (role, telegram_id))
        conn.commit()
        return cur.rowcount > 0


def set_admin_enabled(telegram_id: int, enabled: bool) -> bool:
    with get_connection() as conn:
        cur = conn.execute("UPDATE admins SET enabled=? WHERE telegram_id=?", (int(enabled), telegram_id))
        conn.commit()
        return cur.rowcount > 0


def delete_admin(telegram_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM admins WHERE telegram_id=?", (telegram_id,))
        conn.commit()
        return cur.rowcount > 0


def log_action(admin_id: int, action: str, target: Optional[str] = None, metadata: Optional[str] = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO admin_actions(admin_id,action,target,metadata) VALUES (?,?,?,?)",
            (admin_id, action, target, metadata),
        )
        conn.commit()
    parsed = None
    if metadata:
        try:
            parsed = json.loads(metadata)
        except (TypeError, json.JSONDecodeError):
            parsed = {"detail": str(metadata)[:500]}
    log_event(action, actor_id=admin_id, target=target, metadata=parsed)


def list_actions(limit: int, offset: int, event_type: str | None = None) -> list[sqlite3.Row]:
    with get_connection() as conn:
        if event_type:
            return conn.execute(
                "SELECT * FROM event_log WHERE event_type=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (event_type, limit, offset),
            ).fetchall()
        return conn.execute("SELECT * FROM event_log ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
