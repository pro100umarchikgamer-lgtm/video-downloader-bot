"""Compatibility counters and dashboard aggregates.

New download completion accounting is transactional in
``app.services.jobs.finish_job`` and includes both user and group counters.
The individual increment helpers remain for compatibility with integrations
from the previous release; the application itself uses ``global_totals``.
"""

from __future__ import annotations

from app.database import get_connection


def increment_download_total(telegram_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET downloads_total = downloads_total + 1 "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )
        conn.commit()


def increment_download_success(telegram_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET downloads_success = downloads_success + 1 "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )
        conn.commit()


def increment_download_failed(telegram_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET downloads_failed = downloads_failed + 1 "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )
        conn.commit()


def increment_cache_hit(telegram_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET cache_hits = cache_hits + 1 "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )
        conn.commit()


def increment_cache_miss(telegram_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET cache_misses = cache_misses + 1 "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )
        conn.commit()


def global_totals() -> dict:
    """
    Суммы по всем пользователям — то, что показывает Dashboard.
    Hit rate вычисляется здесь же из hits/misses, отдельного
    хранимого счётчика hit_rate нет (п.15 требования).
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(downloads_total), 0) AS total,
                COALESCE(SUM(downloads_success), 0) AS success,
                COALESCE(SUM(downloads_failed), 0) AS failed,
                COALESCE(SUM(cache_hits), 0) AS hits,
                COALESCE(SUM(cache_misses), 0) AS misses
            FROM users
            """
        ).fetchone()

    hits = row["hits"]
    misses = row["misses"]
    total_cache_lookups = hits + misses
    hit_rate = (hits / total_cache_lookups) if total_cache_lookups else None

    return {
        "total": row["total"],
        "success": row["success"],
        "failed": row["failed"],
        "hits": hits,
        "misses": misses,
        "hit_rate": hit_rate,
    }
