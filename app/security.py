"""URL checks that prevent obvious SSRF use of the downloader."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURLError(ValueError):
    pass


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_obviously_private_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if not url:
            return False
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return True
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
            return True
        try:
            return not _is_public_ip(host)
        except ValueError:
            return False
    except ValueError:
        return True


async def validate_public_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError("malformed URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURLError("only public HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeURLError("credentials in URLs are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeURLError("local hosts are not allowed")
    try:
        public_literal = _is_public_ip(host)
    except ValueError:
        # A hostname (rather than an IP literal) is resolved below.
        pass
    else:
        if not public_literal:
            raise UnsafeURLError("private network address")
        return url

    loop = asyncio.get_running_loop()
    try:
        records = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM),
        )
    except socket.gaierror as exc:
        raise UnsafeURLError("host cannot be resolved") from exc
    addresses = {record[4][0].split("%")[0] for record in records}
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeURLError("host resolves to a non-public address")
    return url
