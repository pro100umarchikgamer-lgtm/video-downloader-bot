from __future__ import annotations

import asyncio
import copy
import json
import logging
import subprocess
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yt_dlp

from app.errors import DownloadCancelled, UserError
from app.security import UnsafeURLError, is_obviously_private_url, validate_public_url

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DownloadResult:
    path: str
    title: str
    quality: int
    filesize: int
    duration: float | None
    source: str
    elapsed_ms: int
    processing_ms: int


class DownloadError(Exception):
    pass


def build_format_selector(info: dict, quality: int) -> str:
    dimension = "width" if (info.get("width") or 0) < (info.get("height") or 0) else "height"
    upper_bounds = {360: 479, 480: 719, 720: 1079, 1080: 1439, 1440: 2159, 2160: 2160}
    upper = upper_bounds.get(quality, quality)
    # Prefer H.264 only inside the requested quality tier. A lower H.264
    # stream must not beat an available target-tier VP9/AV1 stream.
    return (
        f"bestvideo[{dimension}>={quality}][{dimension}<={upper}][vcodec^=avc1]+bestaudio/"
        f"bestvideo[{dimension}>={quality}][{dimension}<={upper}]+bestaudio/"
        f"bestvideo[{dimension}<={upper}]+bestaudio/"
        f"best[{dimension}<={upper}]/"
        "best"
    )


def classify_extractor_error(exc: Exception) -> UserError:
    text = str(exc).lower()
    if any(part in text for part in ("unsupported url", "no suitable extractor", "not a valid url")):
        return UserError("unsupported", str(exc), False)
    if any(part in text for part in ("private video", "login required", "sign in", "cookies", "members-only", "age-restricted")):
        return UserError("private_media", str(exc), False)
    if "requested format is not available" in text:
        return UserError("quality_unavailable", str(exc), False)
    if any(part in text for part in ("video unavailable", "not available", "removed", "deleted")):
        return UserError("unavailable", str(exc), False)
    if any(part in text for part in ("file is larger", "max-filesize", "too large")):
        return UserError("too_large", str(exc), False)
    return UserError("temporary_error", str(exc), True)


class Downloader:
    def __init__(self, temp_dir: str | Path = "./data/tmp"):
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def extract_info(self, url: str) -> dict:
        try:
            info = await asyncio.to_thread(self._extract_sync, url)
            return await self._filter_public_endpoints(info)
        except UserError:
            raise
        except Exception as exc:
            raise classify_extractor_error(exc) from exc

    @staticmethod
    async def _filter_public_endpoints(info: dict) -> dict:
        """Resolve each unique extracted media origin before downloading it.

        The submitted page URL is checked before queueing. Extractors may then
        return different CDN/manifest hosts, so those hosts need the same
        private-network check. Validation is grouped by origin to avoid one
        DNS lookup for every format.
        """
        formats = list(info.get("formats") or [])
        endpoint_urls = [str(fmt.get("url") or "") for fmt in formats]
        if not formats and info.get("url"):
            endpoint_urls.append(str(info["url"]))

        origins: dict[tuple, str] = {}
        for endpoint in endpoint_urls:
            if not endpoint:
                continue
            try:
                parsed = urlsplit(endpoint)
                key = (
                    parsed.scheme.lower(),
                    (parsed.hostname or "").lower(),
                    parsed.port,
                    bool(parsed.username or parsed.password),
                )
            except ValueError:
                key = ("malformed", endpoint, None, False)
            origins.setdefault(key, endpoint)

        semaphore = asyncio.Semaphore(16)

        async def check(key: tuple, endpoint: str) -> tuple[tuple, bool]:
            async with semaphore:
                try:
                    await validate_public_url(endpoint)
                    return key, True
                except UnsafeURLError:
                    return key, False

        checked = await asyncio.gather(*(check(key, endpoint) for key, endpoint in origins.items()))
        allowed = {key for key, safe in checked if safe}

        def endpoint_allowed(endpoint: str) -> bool:
            if not endpoint:
                return True
            try:
                parsed = urlsplit(endpoint)
                key = (
                    parsed.scheme.lower(),
                    (parsed.hostname or "").lower(),
                    parsed.port,
                    bool(parsed.username or parsed.password),
                )
            except ValueError:
                return False
            return key in allowed

        if formats:
            info["formats"] = [fmt for fmt in formats if endpoint_allowed(str(fmt.get("url") or ""))]
            if not info["formats"]:
                raise UserError("unsafe_link", "all media endpoints resolve to non-public addresses", False)
        elif info.get("url") and not endpoint_allowed(str(info["url"])):
            raise UserError("unsafe_link", "media endpoint resolves to a non-public address", False)
        return info

    @staticmethod
    def _extract_sync(url: str) -> dict:
        with yt_dlp.YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "extract_flat": False,
            }
        ) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise DownloadError("extractor returned no media metadata")
        # A playlist URL must never fan out into many jobs. If an extractor
        # still returns a playlist, use only its first concrete entry.
        if info.get("_type") in {"playlist", "multi_video"}:
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                raise DownloadError("playlist contains no downloadable media")
            info = entries[0]
        if is_obviously_private_url(str(info.get("url") or "")):
            raise UserError("unsafe_link", "extractor resolved to a private endpoint", False)
        if info.get("formats"):
            info["formats"] = [
                fmt for fmt in info["formats"]
                if not is_obviously_private_url(str(fmt.get("url") or ""))
            ]
            if not info["formats"]:
                raise UserError("unsafe_link", "all media endpoints are private", False)
        return info

    @staticmethod
    def _probe_duration(path: Path) -> float | None:
        try:
            completed = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "json", str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            value = json.loads(completed.stdout).get("format", {}).get("duration")
            duration = float(value) if value is not None else None
            return duration if duration and duration > 0 else None
        except Exception:
            logger.warning("Could not probe media duration for %s", path, exc_info=True)
            return None

    async def download(
        self,
        url: str,
        quality: int,
        max_filesize: int,
        *,
        info: Optional[dict] = None,
        cancel_event: asyncio.Event | None = None,
    ) -> DownloadResult:
        try:
            return await asyncio.to_thread(
                self._download_sync, url, quality, max_filesize, info, cancel_event
            )
        except DownloadCancelled:
            raise
        except UserError:
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled() from exc
            raise classify_extractor_error(exc) from exc

    def _download_sync(
        self,
        url: str,
        quality: int,
        max_filesize: int,
        info: Optional[dict],
        cancel_event: asyncio.Event | None,
    ) -> DownloadResult:
        workdir = Path(tempfile.mkdtemp(prefix="video_", dir=self.temp_dir))
        started = time.monotonic()
        media_finished: float | None = None
        postprocess_started: float | None = None
        postprocess_finished: float | None = None

        def progress_hook(data: dict) -> None:
            nonlocal media_finished
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            if data.get("status") == "finished":
                media_finished = time.monotonic()

        def postprocessor_hook(data: dict) -> None:
            nonlocal postprocess_started, postprocess_finished
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            if data.get("status") == "started" and postprocess_started is None:
                postprocess_started = time.monotonic()
            elif data.get("status") == "finished":
                postprocess_finished = time.monotonic()

        try:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            if info is None:
                info = self._extract_sync(url)
            title = str(info.get("title") or "Video")
            source = str(info.get("extractor_key") or info.get("extractor") or "unknown")
            duration = info.get("duration")
            output_template = str(workdir / "media.%(ext)s")
            selector = build_format_selector(info, quality)
            opts = {
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "merge_output_format": "mp4",
                "format": selector,
                "max_filesize": max_filesize,
                "progress_hooks": [progress_hook],
                "postprocessor_hooks": [postprocessor_hook],
                "overwrites": True,
                "continuedl": False,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Reuse the already extracted metadata and avoid a second
                # extractor request. yt-dlp still performs media HTTP requests.
                prepared_info = copy.deepcopy(info)
                for selected_key in (
                    "requested_downloads", "requested_formats", "format",
                    "format_id", "format_note", "ext", "filesize",
                    "filesize_approx", "url", "manifest_url",
                ):
                    prepared_info.pop(selected_key, None)
                prepared_info.setdefault("webpage_url", url)
                ydl.process_ie_result(prepared_info, download=True)
            files = [
                path for path in workdir.iterdir()
                if path.is_file() and not path.name.endswith((".part", ".ytdl"))
            ]
            if not files:
                raise DownloadError("downloader produced no file")
            video_path = max(files, key=lambda path: path.stat().st_size)
            filesize = video_path.stat().st_size
            if filesize > max_filesize:
                raise UserError("too_large", f"result is {filesize} bytes", False)
            try:
                normalized_duration = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                normalized_duration = None
            if normalized_duration is None or normalized_duration <= 0:
                normalized_duration = self._probe_duration(video_path)
            return DownloadResult(
                path=str(video_path),
                title=title,
                quality=quality,
                filesize=filesize,
                duration=normalized_duration,
                source=source,
                elapsed_ms=round(((media_finished or time.monotonic()) - started) * 1000),
                processing_ms=round(((postprocess_finished or postprocess_started or 0) - (postprocess_started or postprocess_finished or 0)) * 1000) if postprocess_started else 0,
            )
        except DownloadCancelled:
            self.cleanup_dir(workdir)
            raise
        except Exception:
            self.cleanup_dir(workdir)
            raise

    def cleanup(self, path: str | Path) -> None:
        file_path = Path(path)
        try:
            file_path.unlink(missing_ok=True)
            self.cleanup_dir(file_path.parent)
        except OSError:
            logger.exception("Temporary media cleanup failed: %s", file_path)

    def cleanup_dir(self, path: str | Path) -> None:
        directory = Path(path)
        try:
            resolved = directory.resolve()
            temp_root = self.temp_dir.resolve()
            if resolved.parent != temp_root or not resolved.name.startswith(("video_", "note_")):
                return
            shutil.rmtree(resolved, ignore_errors=False)
        except FileNotFoundError:
            return
        except OSError:
            logger.exception("Temporary directory cleanup failed: %s", directory)
