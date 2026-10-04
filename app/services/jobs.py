from __future__ import annotations

import uuid
from typing import Any

from app.database import get_connection

ALLOWED_UPDATE_FIELDS = {
    "effective_quality", "status", "error_category", "source", "content_id",
    "title", "duration", "filesize", "cache_hit", "queue_wait_ms",
    "metadata_ms", "download_ms", "processing_ms", "upload_ms", "total_ms",
    "started_at", "finished_at",
    "available_qualities",
}


def create_job(
    requester_id: int,
    chat_id: int,
    url: str,
    requested_quality: str,
    *,
    retry_of: str | None = None,
    job_id: str | None = None,
) -> str:
    job_id = job_id or uuid.uuid4().hex
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO download_jobs
               (id,requester_id,chat_id,url,requested_quality,retry_of)
               VALUES (?,?,?,?,?,?)""",
            (job_id, requester_id, chat_id, url, requested_quality, retry_of),
        )
        conn.commit()
    return job_id


def get_job(job_id: str):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone()


def update_job(job_id: str, **values: Any) -> None:
    if not values:
        return
    unknown = set(values) - ALLOWED_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"Unsupported job fields: {sorted(unknown)}")
    assignments = ",".join(f"{field}=?" for field in values)
    with get_connection() as conn:
        conn.execute(f"UPDATE download_jobs SET {assignments} WHERE id=?", (*values.values(), job_id))
        conn.commit()


def start_job(job_id: str, queue_wait_ms: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE download_jobs SET status='active',started_at=CURRENT_TIMESTAMP,queue_wait_ms=? WHERE id=?",
            (queue_wait_ms, job_id),
        )
        conn.commit()


def finish_job(job_id: str, status: str, *, error_category: str | None = None, total_ms: int | None = None) -> None:
    with get_connection() as conn:
        row = conn.execute("SELECT requester_id,chat_id,status FROM download_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        # A race between cancel and completion is resolved once: any terminal
        # state remains terminal.
        if row["status"] in {"success", "failed", "cancelled"}:
            return
        conn.execute(
            """UPDATE download_jobs SET status=?,error_category=?,total_ms=?,
               finished_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, error_category, total_ms, job_id),
        )
        conn.execute("UPDATE users SET downloads_total=downloads_total+1 WHERE telegram_id=?", (row["requester_id"],))
        if status == "success":
            conn.execute("UPDATE users SET downloads_success=downloads_success+1 WHERE telegram_id=?", (row["requester_id"],))
        elif status == "failed":
            conn.execute("UPDATE users SET downloads_failed=downloads_failed+1 WHERE telegram_id=?", (row["requester_id"],))
        if row["chat_id"] < 0:
            conn.execute("UPDATE groups SET downloads_total=downloads_total+1 WHERE chat_id=?", (row["chat_id"],))
            if status == "success":
                conn.execute("UPDATE groups SET downloads_success=downloads_success+1 WHERE chat_id=?", (row["chat_id"],))
            elif status == "failed":
                conn.execute("UPDATE groups SET downloads_failed=downloads_failed+1 WHERE chat_id=?", (row["chat_id"],))
        conn.commit()


def increment_cache_counter(user_id: int, hit: bool) -> None:
    field = "cache_hits" if hit else "cache_misses"
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {field}={field}+1 WHERE telegram_id=?", (user_id,))
        conn.commit()


def job_stats(period_days: int | None = None) -> dict:
    where = ""
    params: tuple = ()
    if period_days is not None:
        where = "WHERE created_at>=datetime('now',?)"
        params = (f"-{period_days} days",)
    with get_connection() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) total,
                SUM(status='queued') queued,SUM(status='active') active,
                SUM(status='success') success,SUM(status='failed') failed,
                SUM(status='cancelled') cancelled,
                AVG(CASE WHEN status='success' THEN total_ms END) avg_ms,
                AVG(CASE WHEN status='success' THEN queue_wait_ms END) avg_queue_ms,
                AVG(CASE WHEN status='success' THEN metadata_ms END) avg_metadata_ms,
                AVG(CASE WHEN status='success' THEN download_ms END) avg_download_ms,
                AVG(CASE WHEN status='success' THEN processing_ms END) avg_processing_ms,
                AVG(CASE WHEN status='success' THEN upload_ms END) avg_upload_ms,
                SUM(cache_hit=1) cache_hits,
                SUM(content_id IS NOT NULL) cache_lookups
                FROM download_jobs {where}""",
            params,
        ).fetchone()
    return {key: row[key] or 0 for key in row.keys()}


def distribution(field: str, period_days: int | None = None, limit: int = 10) -> list:
    if field not in {"source", "requested_quality", "effective_quality", "error_category"}:
        raise ValueError("invalid distribution field")
    where = f"WHERE {field} IS NOT NULL"
    params: list = []
    if period_days is not None:
        where += " AND created_at>=datetime('now',?)"
        params.append(f"-{period_days} days")
    params.append(limit)
    with get_connection() as conn:
        return conn.execute(
            f"SELECT {field} value,COUNT(*) count FROM download_jobs {where} GROUP BY {field} ORDER BY count DESC LIMIT ?",
            params,
        ).fetchall()


def active_jobs(limit: int = 20) -> list:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM download_jobs WHERE status IN ('queued','active') ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()


def recover_interrupted_jobs() -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE download_jobs SET status='failed',error_category='interrupted',
               finished_at=CURRENT_TIMESTAMP
               WHERE status IN ('queued','active')"""
        )
        conn.commit()
        return cur.rowcount
