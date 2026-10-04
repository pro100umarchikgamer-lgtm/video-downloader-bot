from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "igshid",
    "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term",
}


def normalized_url(url: str) -> str:
    """Remove known tracking data without changing content-bearing params."""
    parsed = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    # Query ordering can be content-bearing for an arbitrary extractor. Do
    # not reorder it here: metadata-based extractor/id identity supersedes
    # this URL fallback after the single extract_info call.
    host = (parsed.hostname or "").lower()
    port = parsed.port
    normalized_host = f"[{host}]" if ":" in host else host
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(query, doseq=True), ""))


def get_content_id(url: str, info: Optional[dict] = None) -> str:
    if info:
        source = info.get("extractor_key") or info.get("extractor") or urlsplit(url).hostname or "url"
        media_id = info.get("id")
        if media_id:
            return f"{str(source).lower()}:{media_id}"
    canonical = normalized_url(url)
    host = (urlsplit(canonical).hostname or "url").lower()
    return f"{host}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
