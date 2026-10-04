from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from app.database import get_connection
from app.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)


def cleanup_orphan_temp(temp_dir: str | Path, runtime: RuntimeConfig) -> int:
    root = Path(temp_dir)
    if not root.exists():
        return 0
    cutoff = time.time() - int(runtime.get("temp_retention_minutes")) * 60
    removed = 0
    for path in root.iterdir():
        if path.name == "video_notes":
            continue
        if not path.name.startswith(("video_", "note_")):
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except OSError:
            logger.warning("Could not clean orphan temp path: %s", path, exc_info=True)
    return removed


def apply_data_retention(runtime: RuntimeConfig) -> int:
    days = int(runtime.get("event_retention_days"))
    with get_connection() as conn:
        deleted = conn.execute(
            "DELETE FROM event_log WHERE created_at<datetime('now',?)",
            (f"-{days} days",),
        ).rowcount
        conn.execute(
            "DELETE FROM admin_actions WHERE created_at<datetime('now',?)",
            (f"-{days} days",),
        )
        history_days = int(runtime.get("download_history_retention_days"))
        conn.execute(
            """DELETE FROM download_jobs
               WHERE status IN ('success','failed','cancelled')
                 AND created_at<datetime('now',?)""",
            (f"-{history_days} days",),
        )
        conn.commit()
    return deleted
