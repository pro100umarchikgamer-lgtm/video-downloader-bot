from __future__ import annotations

from typing import Optional

QUALITY_ORDER = [2160, 1440, 1080, 720, 480, 360]


def format_size(size: Optional[int]) -> str:
    if not size:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def estimate_format_size(fmt: dict) -> Optional[int]:
    value = fmt.get("filesize") or fmt.get("filesize_approx")
    return int(value) if value else None


def available_quality_sizes(formats: list[dict]) -> dict[int, int | None]:
    """Return canonical source heights and a conservative size estimate.

    For separate streams, the smallest known audio estimate is added. Unknown
    estimates remain eligible; the final downloaded size is still enforced.
    """
    audio_sizes = [
        estimate_format_size(fmt)
        for fmt in formats
        if fmt.get("vcodec") in (None, "none") and fmt.get("acodec") not in (None, "none")
    ]
    known_audio = [size for size in audio_sizes if size is not None]
    audio_size = min(known_audio) if known_audio else None
    available: dict[int, int | None] = {}
    for fmt in formats:
        if fmt.get("vcodec") in (None, "none"):
            continue
        try:
            height = int(fmt.get("height") or 0)
            width = int(fmt.get("width") or 0)
        except (TypeError, ValueError):
            continue
        dimension = min(height, width) if width and height else height
        canonical = next((quality for quality in QUALITY_ORDER if dimension >= quality), None)
        if canonical is None:
            continue
        size = estimate_format_size(fmt)
        if fmt.get("acodec") in (None, "none"):
            size = size + audio_size if size is not None and audio_size is not None else None
        old = available.get(canonical)
        if canonical not in available or (size is not None and (old is None or size < old)):
            available[canonical] = size
    return available


def choose_quality(
    formats: list[dict],
    max_size: int,
    preferred_quality: str | int = "auto",
) -> tuple[int, Optional[int]]:
    available = available_quality_sizes(formats)
    if not available:
        # Some extractors expose no detailed format list. yt-dlp can still
        # resolve a generic best format, represented by a conservative 360 key.
        return 360, None

    requested = str(preferred_quality).lower().removesuffix("p")
    if requested == "auto":
        candidates = [q for q in QUALITY_ORDER if q in available]
    else:
        target = int(requested)
        lower = sorted((q for q in available if q <= target), reverse=True)
        higher = sorted(q for q in available if q > target)
        candidates = lower + higher

    for quality in candidates:
        size = available[quality]
        if size is None or size <= max_size:
            return quality, size

    # No known estimate fits. Returning the smallest lets the caller report a
    # truthful transport-size error without downloading a much larger variant.
    smallest = min(available)
    return smallest, available[smallest]


def available_qualities(formats: list[dict]) -> list[int]:
    available = available_quality_sizes(formats)
    return [quality for quality in reversed(QUALITY_ORDER) if quality in available]
