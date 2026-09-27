from __future__ import annotations

import hashlib
from urllib.parse import urlparse

import yt_dlp


def get_content_id(url: str) -> str:
    try:
        with yt_dlp.YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
            }
        ) as ydl:
            data = ydl.extract_info(url, download=False)

        source = (
            data.get("extractor_key")
            or data.get("extractor")
            or urlparse(url).netloc
        )

        content_id = data.get("id")

        if content_id:
            return f"{source.lower()}_{content_id}"

    except Exception:
        pass

    parsed = urlparse(url)
    normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    if parsed.query:
        normalized += f"?{parsed.query}"

    return f"{parsed.netloc.lower()}_{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
