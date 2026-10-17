"""Loopback HTTP helpers that never consume WebHarbor catalog ports."""

from __future__ import annotations

import errno
import http.client
import itertools
import threading
from urllib.parse import urlsplit


# Linux commonly assigns ephemeral client ports from 32768-60999. WebHarbor's
# stable 40000+ listeners overlap that range, so local probes use a source port
# above it to avoid leaving TIME_WAIT sockets on future site ports.
LOCAL_SOURCE_PORT_FIRST = 61000
LOCAL_SOURCE_PORT_LAST = 65535
LOCAL_SOURCE_ADDRESS = "127.0.0.2"
_source_port_counter = itertools.count()
_source_port_lock = threading.Lock()


def _next_source_port() -> int:
    with _source_port_lock:
        offset = next(_source_port_counter)
    size = LOCAL_SOURCE_PORT_LAST - LOCAL_SOURCE_PORT_FIRST + 1
    return LOCAL_SOURCE_PORT_FIRST + offset % size


def request_loopback_url(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 30.0,
    read_limit: int | None = None,
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError(f"not a loopback HTTP URL: {url}")
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=timeout,
        source_address=(LOCAL_SOURCE_ADDRESS, 0),
    )
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read() if read_limit is None else response.read(read_limit)
        return response.status, body
    except OSError as exc:
        if exc.errno not in {errno.EADDRINUSE, errno.EADDRNOTAVAIL}:
            raise
    finally:
        connection.close()

    # Non-Linux platforms may not expose the full 127/8 loopback range. Retain
    # the original dedicated-port strategy as a compatibility fallback.
    attempts = LOCAL_SOURCE_PORT_LAST - LOCAL_SOURCE_PORT_FIRST + 1
    for _ in range(attempts):
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            port,
            timeout=timeout,
            source_address=("127.0.0.1", _next_source_port()),
        )
        try:
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read() if read_limit is None else response.read(read_limit)
            return response.status, body
        except OSError as exc:
            if exc.errno not in {errno.EADDRINUSE, errno.EADDRNOTAVAIL}:
                raise
        finally:
            connection.close()
    raise OSError(errno.EADDRINUSE, "no dedicated loopback source port is available")
