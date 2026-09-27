from __future__ import annotations

from typing import Optional


QUALITY_ORDER = [1440, 1080, 720, 480, 360]


def format_size(size: Optional[int]) -> str:
    if not size:
        return "неизвестно"

    value = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024

    return "неизвестно"


def estimate_format_size(fmt: dict) -> Optional[int]:
    """
    Возвращает известный размер конкретного формата.
    Для видео-only форматов размер может быть приблизительным.
    """
    size = fmt.get("filesize") or fmt.get("filesize_approx")

    if size:
        return int(size)

    return None


def choose_quality(
    formats: list[dict],
    max_size: int,
    preferred_quality: int = 1440,
) -> tuple[int, Optional[int]]:
    """
    Выбирает максимальное доступное качество,
    которое укладывается в max_size.

    Если размер неизвестен, качество не отбрасываем:
    yt-dlp всё равно сможет попробовать его скачать.
    """

    available = {}

    for fmt in formats:
        height = fmt.get("height")

        if not height:
            continue

        height = int(height)

        if height not in QUALITY_ORDER:
            continue

        size = estimate_format_size(fmt)

        # Для одного разрешения сохраняем самый маленький
        # известный вариант.
        if height not in available:
            available[height] = size
        elif size is not None:
            old_size = available[height]
            if old_size is None or size < old_size:
                available[height] = size

    qualities = [
        q for q in QUALITY_ORDER
        if q <= preferred_quality and q in available
    ]

    if not qualities:
        return 360, None

    for quality in qualities:
        size = available[quality]

        if size is None:
            return quality, None

        if size <= max_size:
            return quality, size

    # Даже если все известные варианты больше лимита,
    # берём самое низкое доступное качество.
    quality = min(qualities)
    return quality, available.get(quality)
