from __future__ import annotations

from app.database import get_connection
from admin.counters import global_totals


def total_users() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def active_users(days: int) -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE last_seen>=datetime('now',?)", (f"-{days} days",)).fetchone()[0]


def new_users(days: int) -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE first_seen>=datetime('now',?)", (f"-{days} days",)).fetchone()[0]


def total_groups() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]


def download_stats() -> dict:
    return global_totals()


def cache_stats() -> dict:
    totals = global_totals()
    return {"hits": totals["hits"], "misses": totals["misses"], "hit_rate": totals["hit_rate"]}


def users_by_locale() -> list:
    with get_connection() as conn:
        return conn.execute(
            "SELECT locale,locale_manual,COUNT(*) count FROM users GROUP BY locale,locale_manual ORDER BY count DESC"
        ).fetchall()


def resolve_user_audience(audience: str, *, period_days: int | None = None, exclude_blocked: bool = True) -> list[int]:
    where: list[str] = ["blocked=0"] if exclude_blocked else []
    params: list = []
    if audience == "active":
        where.append("last_seen>=datetime('now',?)")
        params.append(f"-{period_days or 7} days")
    elif audience == "new":
        where.append("first_seen>=datetime('now',?)")
        params.append(f"-{period_days or 7} days")
    elif audience == "downloaded":
        where.append("downloads_total>0")
    elif audience == "top":
        query = "SELECT telegram_id FROM users" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY downloads_total DESC LIMIT ?"
        with get_connection() as conn:
            return [row[0] for row in conn.execute(query, (*params, period_days or 50)).fetchall()]
    elif audience != "all":
        raise ValueError("unknown audience")
    query = "SELECT telegram_id FROM users" + (" WHERE " + " AND ".join(where) if where else "")
    with get_connection() as conn:
        return [row[0] for row in conn.execute(query, params).fetchall()]


def resolve_group_audience(exclude_blocked: bool = True) -> list[int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT chat_id FROM groups" + (" WHERE blocked=0" if exclude_blocked else "")).fetchall()
    return [row[0] for row in rows]


def recent_events(limit: int = 5) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT event_type,target,created_at FROM event_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"action": row["event_type"], "target": row["target"], "created_at": row["created_at"]} for row in rows]
