from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.config import BootstrapConfig
from app.runtime_config import RuntimeConfig
from app.services.downloader import Downloader
from app.services.storage import StorageService
from app.services.video_note import VideoNoteService


@dataclass(slots=True)
class AppContext:
    bootstrap: BootstrapConfig
    runtime: RuntimeConfig
    downloader: Downloader
    storage: StorageService
    video_notes: VideoNoteService
    alerts: Any = None
    started_monotonic: float = 0.0
    polling: bool = False
    worker_states: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.started_monotonic:
            self.started_monotonic = time.monotonic()


_context: AppContext | None = None


def set_context(context: AppContext) -> None:
    global _context
    _context = context


def app_context() -> AppContext:
    if _context is None:
        raise RuntimeError("Application context is not initialized")
    return _context
