from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yt_dlp

from app.services.formats import QUALITY_ORDER


@dataclass
class DownloadResult:
    path: str
    title: str
    quality: int
    filesize: int


class DownloadError(Exception):
    pass


class Downloader:
    def __init__(self):
        self.max_filesize = int(
            os.getenv("MAX_FILESIZE_MB", "45")
        ) * 1024 * 1024

    async def download(
        self,
        url: str,
        quality: str | int = "auto",
        max_filesize: Optional[int] = None,
    ) -> DownloadResult:
        return await asyncio.to_thread(
            self._download_sync,
            url,
            quality,
            max_filesize or self.max_filesize,
        )

    def _download_sync(
        self,
        url: str,
        quality: str | int,
        max_filesize: int,
    ) -> DownloadResult:

        workdir = Path(
            tempfile.mkdtemp(prefix="video_", dir="./data")
        )

        try:
            with yt_dlp.YoutubeDL({
                "quiet": True,
                "no_warnings": False,
                "noplaylist": True,
            }) as ydl:
                info = ydl.extract_info(url, download=False)

            title = info.get("title") or "video"

            if quality == "auto":
                qualities = QUALITY_ORDER
            else:
                qualities = [int(quality)]

            last_error = None

            for current_quality in qualities:
                try:
                    print(
                        f"[downloader] пробую {current_quality}p"
                    )

                    output_template = str(
                        workdir / "%(title).80s.%(ext)s"
                    )

                    ydl_opts = {
                        "outtmpl": output_template,
                        "noplaylist": True,
                        "quiet": True,
                        "merge_output_format": "mp4",
                        "format": (
                            f"bestvideo[height<={current_quality}]"
                            f"+bestaudio/"
                            f"best[height<={current_quality}]"
                        ),
                    }

                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])

                    files = [
                        p for p in workdir.iterdir()
                        if p.is_file()
                    ]

                    if not files:
                        raise DownloadError(
                            "yt-dlp не создал файл."
                        )

                    video_path = max(
                        files,
                        key=lambda p: p.stat().st_size
                    )

                    filesize = video_path.stat().st_size

                    print(
                        f"[downloader] {current_quality}p: "
                        f"{filesize / 1024 / 1024:.1f} MB"
                    )

                    if filesize <= max_filesize:
                        return DownloadResult(
                            path=str(video_path),
                            title=title,
                            quality=current_quality,
                            filesize=filesize,
                        )

                    print(
                        f"[downloader] файл слишком большой: "
                        f"{filesize / 1024 / 1024:.1f} MB"
                    )

                    video_path.unlink(missing_ok=True)

                except Exception as exc:
                    last_error = exc
                    print(
                        f"[downloader] {current_quality}p ошибка: "
                        f"{exc}"
                    )

                    for p in workdir.iterdir():
                        if p.is_file():
                            p.unlink(missing_ok=True)

            raise DownloadError(
                f"Не удалось получить файл меньше "
                f"{max_filesize / 1024 / 1024:.0f} MB. "
                f"Последняя ошибка: {last_error}"
            )

        finally:
            try:
                if workdir.exists() and not any(workdir.iterdir()):
                    workdir.rmdir()
            except OSError:
                pass

    @staticmethod
    def cleanup(path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
