from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UserError(Exception):
    category: str
    detail: str = ""
    retryable: bool = False

    def __str__(self) -> str:
        return self.detail or self.category


class DownloadCancelled(Exception):
    pass
