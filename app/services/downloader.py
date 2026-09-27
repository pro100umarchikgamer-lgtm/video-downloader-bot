from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yt_dlp


@dataclass
class VideoInfo:
    title: str
    duration: Optional[int]
    uploader: Optional[str]
    extractor: str
    webpage_url: str
    formats: list[dict]


@dataclass
class DownloadResult:
    path: Path
    title: str
    duration: Optional[int]
    width: Optional[int]
    height: Optional[int]
    filesize: Optional[int]


class DownloadError(Exception):
    pass


class Downloader:
    def __init__(self) -> None:
        self.temp_root = Path(
            os.getenv("DOWNLOAD_TEMP_DIR", "./data/tmp")
        )
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def _base_options(self) -> dict:
        return {
            "quiet": True,
            "no_warnings": False,
            "noplaylist": True,
            "restrictfilenames": True,
            "windowsfilenames": True,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 4,
        }

    async def get_info(self, url: str) -> VideoInfo:
        return await asyncio.to_thread(self._get_info_sync, url)

    def _get_info_sync(self, url: str) -> VideoInfo:
        options = self._base_options()
        options["skip_download"] = True

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise DownloadError(str(exc)) from exc

        formats = []

        for fmt in data.get("formats", []):
            height = fmt.get("height")
            if not height:
                continue

            formats.append(
                {
                    "format_id": fmt.get("format_id"),
                    "height": height,
                    "width": fmt.get("width"),
                    "ext": fmt.get("ext"),
                    "fps": fmt.get("fps"),
                    "filesize": fmt.get("filesize")
                    or fmt.get("filesize_approx"),
                    "vcodec": fmt.get("vcodec"),
                    "acodec": fmt.get("acodec"),
                }
            )

        return VideoInfo(
            title=data.get("title") or "Без названия",
            duration=data.get("duration"),
            uploader=data.get("uploader"),
            extractor=data.get("extractor_key")
            or data.get("extractor")
            or "Unknown",
            webpage_url=data.get("webpage_url") or url,
            formats=formats,
        )

    async def download(
        self,
        url: str,
        quality: str = "best",
        max_filesize: Optional[int] = None,
    ) -> DownloadResult:
        return await asyncio.to_thread(
            self._download_sync,
            url,
            quality,
            max_filesize,
        )

    def _download_sync(
        self,
        url: str,
        quality: str,
        max_filesize: Optional[int],
    ) -> DownloadResult:

        job_dir = Path(
            tempfile.mkdtemp(
                prefix="job_",
                dir=self.temp_root,
            )
        )

        output_template = str(job_dir / "%(title).120s.%(ext)s")

        if quality == "best":
            format_selector = (
                "bv*[ext=mp4]+ba[ext=m4a]/"
                "bv*+ba/b[ext=mp4]/b"
            )
        else:
            try:
                height = int(quality)
            except ValueError:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise DownloadError("Некорректное качество.")

            format_selector = (
                f"bv*[height<={height}][ext=mp4]+"
                f"ba[ext=m4a]/"
                f"bv*[height<={height}]+ba/"
                f"b[height<={height}]"
            )

        options = self._base_options()
        options.update(
            {
                "format": format_selector,
                "merge_output_format": "mp4",
                "outtmpl": output_template,
                "paths": {"home": str(job_dir)},
                "writethumbnail": False,
            }
        )

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(
                    url,
                    download=True,
                )

            files = [
                p
                for p in job_dir.iterdir()
                if p.is_file()
                and p.suffix.lower()
                in {".mp4", ".mkv", ".webm", ".mov", ".avi"}
            ]

            if not files:
                raise DownloadError(
                    "yt-dlp не создал видеофайл."
                )

            result_path = max(
                files,
                key=lambda p: p.stat().st_mtime,
            )

            filesize = result_path.stat().st_size

            if max_filesize and filesize > max_filesize:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise DownloadError(
                    "Файл превышает установленный лимит размера."
                )

            return DownloadResult(
                path=result_path,
                title=data.get("title") or result_path.stem,
                duration=data.get("duration"),
                width=data.get("width"),
                height=data.get("height"),
                filesize=filesize,
            )

        except DownloadError:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

        except Exception as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise DownloadError(str(exc)) from exc

    @staticmethod
    def cleanup(path: Path) -> None:
        job_dir = path.parent
        shutil.rmtree(job_dir, ignore_errors=True)
