"""Persisted, idempotent Telegram video-note conversion."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from app.database import get_connection
from app.runtime_config import RuntimeConfig
from app.services.downloader import Downloader
from app.services.telegram import telegram_retry

logger = logging.getLogger(__name__)
VIDEO_NOTE_MAX_DURATION = 60.0
VIDEO_NOTE_SIZE = 640


def is_video_note_eligible(duration: float | int | None, enabled: bool = True) -> bool:
    if not enabled or duration is None:
        return False
    return 0 < float(duration) <= VIDEO_NOTE_MAX_DURATION


class VideoNoteService:
    def __init__(self, runtime: RuntimeConfig, downloader: Downloader, temp_dir: str | Path):
        self.runtime = runtime
        self.downloader = downloader
        self.root = Path(temp_dir) / "video_notes"
        self.sources = self.root / "sources"
        self.jobs = self.root / "jobs"
        self.sources.mkdir(parents=True, exist_ok=True)
        self.jobs.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(1)

    def create_delivery(
        self,
        *,
        job_id: str,
        requester_id: int,
        chat_id: int,
        content_id: str,
        source_url: str,
        quality: int,
        telegram_file_id: str | None,
        duration: float | None,
        source_path: str | None = None,
    ) -> str | None:
        if not is_video_note_eligible(duration, bool(self.runtime.get("video_note_enabled"))):
            return None
        token = secrets.token_urlsafe(9)
        local_path = None
        if source_path and Path(source_path).is_file():
            destination = self.sources / f"{token}{Path(source_path).suffix or '.mp4'}"
            try:
                os.link(source_path, destination)
            except OSError:
                shutil.copy2(source_path, destination)
            local_path = str(destination)
        ttl = int(self.runtime.get("video_note_source_ttl_minutes"))
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO media_deliveries
                   (token,job_id,requester_id,chat_id,content_id,source_url,quality,
                    telegram_file_id,local_path,duration,expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now',?))""",
                (token, job_id, requester_id, chat_id, content_id, source_url, quality,
                 telegram_file_id, local_path, duration, f"+{ttl} minutes"),
            )
            conn.commit()
        return token

    def get_delivery(self, token: str):
        with get_connection() as conn:
            return conn.execute(
                "SELECT * FROM media_deliveries WHERE token=? AND expires_at>CURRENT_TIMESTAMP",
                (token,),
            ).fetchone()

    def claim(self, token: str) -> str:
        """Return claimed, busy, done or missing atomically."""
        with get_connection() as conn:
            row = conn.execute("SELECT note_status,note_file_id FROM media_deliveries WHERE token=? AND expires_at>CURRENT_TIMESTAMP", (token,)).fetchone()
            if row is None:
                return "missing"
            if row["note_file_id"] or row["note_status"] == "done":
                return "done"
            if row["note_status"] == "processing":
                return "busy"
            cur = conn.execute(
                "UPDATE media_deliveries SET note_status='processing' WHERE token=? AND note_status IN ('ready','failed')",
                (token,),
            )
            conn.commit()
            return "claimed" if cur.rowcount else "busy"

    def recover_interrupted(self) -> int:
        """Release claims left by an unclean single-process shutdown.

        The current deployment model runs one bot process. A future
        multi-process deployment must replace this startup reset with leased
        claims owned by a worker identity.
        """
        with get_connection() as conn:
            changed = conn.execute(
                "UPDATE media_deliveries SET note_status='failed' WHERE note_status='processing'"
            ).rowcount
            conn.commit()
        return changed

    async def create_and_send(self, bot: Bot, token: str) -> str:
        row = self.get_delivery(token)
        if row is None:
            return "missing"
        async with self._semaphore:
            workdir = Path(tempfile.mkdtemp(prefix="note_", dir=self.jobs))
            source: Path | None = None
            downloaded_result = None
            try:
                if row["local_path"] and Path(row["local_path"]).is_file():
                    source = Path(row["local_path"])
                elif row["telegram_file_id"]:
                    source = workdir / "source.mp4"
                    try:
                        await bot.download(row["telegram_file_id"], destination=source)
                    except Exception:
                        source = None
                if source is None:
                    info = await self.downloader.extract_info(row["source_url"])
                    downloaded_result = await self.downloader.download(
                        row["source_url"], int(row["quality"]),
                        int(self.runtime.get("max_delivery_mb")) * 1024 * 1024,
                        info=info,
                    )
                    source = Path(downloaded_result.path)

                output = workdir / "note.mp4"
                await self._convert(source, output)
                message = await telegram_retry(
                    lambda: bot.send_video_note(
                        row["chat_id"],
                        video_note=FSInputFile(output),
                        duration=min(60, max(1, int(float(row["duration"] or 1)))),
                        length=VIDEO_NOTE_SIZE,
                    )
                )
                file_id = message.video_note.file_id if message.video_note else None
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE media_deliveries SET note_status='done',note_file_id=? WHERE token=?",
                        (file_id, token),
                    )
                    conn.commit()
                return "done"
            except asyncio.CancelledError:
                # A shutdown/cancel must not leave a permanently busy token.
                # The per-job work directory is removed by ``finally`` and a
                # later tap may safely claim the delivery again.
                with get_connection() as conn:
                    conn.execute("UPDATE media_deliveries SET note_status='failed' WHERE token=?", (token,))
                    conn.commit()
                raise
            except Exception:
                logger.exception("Video-note conversion failed token=%s", token)
                with get_connection() as conn:
                    conn.execute("UPDATE media_deliveries SET note_status='failed' WHERE token=?", (token,))
                    conn.commit()
                return "failed"
            finally:
                if downloaded_result is not None:
                    self.downloader.cleanup(downloaded_result.path)
                shutil.rmtree(workdir, ignore_errors=True)

    async def _convert(self, source: Path, output: Path) -> None:
        filter_graph = (
            "crop='min(iw,ih)':'min(iw,ih)',"
            f"scale={VIDEO_NOTE_SIZE}:{VIDEO_NOTE_SIZE}:flags=lanczos"
        )
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-t", "60", "-vf", filter_graph,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
            "-maxrate", "2M", "-bufsize", "4M",
            "-pix_fmt", "yuv420p", "-profile:v", "main",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            str(output),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            output.unlink(missing_ok=True)
            raise
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("video-note conversion timed out")
        if process.returncode != 0 or not output.is_file():
            raise RuntimeError((stderr or b"ffmpeg failed").decode(errors="replace")[-500:])

    def cleanup_expired(self) -> int:
        with get_connection() as conn:
            rows = conn.execute("SELECT local_path FROM media_deliveries WHERE expires_at<=CURRENT_TIMESTAMP").fetchall()
            deleted = conn.execute("DELETE FROM media_deliveries WHERE expires_at<=CURRENT_TIMESTAMP").rowcount
            conn.commit()
        for row in rows:
            if row["local_path"]:
                path = Path(row["local_path"])
                try:
                    if path.parent.resolve() == self.sources.resolve():
                        path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove expired video-note source: %s", path)
        cutoff = time.time() - int(self.runtime.get("temp_retention_minutes")) * 60
        for directory in self.jobs.glob("note_*"):
            try:
                if directory.stat().st_mtime < cutoff:
                    shutil.rmtree(directory)
            except OSError:
                logger.warning("Could not remove orphan video-note job: %s", directory)
        with get_connection() as conn:
            referenced = {Path(row[0]).resolve() for row in conn.execute("SELECT local_path FROM media_deliveries WHERE local_path IS NOT NULL")}
        for source in self.sources.iterdir():
            try:
                if source.resolve() not in referenced and source.stat().st_mtime < cutoff:
                    source.unlink()
            except OSError:
                logger.warning("Could not remove orphan video-note source: %s", source)
        return deleted
