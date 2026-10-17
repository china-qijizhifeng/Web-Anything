"""Bounded Responses transport probe; never prints credentials or response bodies."""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


def probe(base_url: str, api_key: str, model: str, timeout: float = 30) -> dict:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        return {"status": "invalid_url"}
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/responses", "", ""))
    body = json.dumps({"model": model, "input": "Reply OK.", "max_output_tokens": 16,
                       "stream": True}).encode()
    request = Request(endpoint, data=body, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        "Accept": "text/event-stream"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            # HTTP 200 plus a real SSE event proves the HTTP stream works.
            for _ in range(20):
                line = response.readline(4096)
                if line.startswith(b"data: "):
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "error" or event.get("error"):
                        return {"status": "stream_error", "http_status": response.status}
                    if event.get("type", "").startswith("response."):
                        return {"status": "passed", "http_status": response.status,
                                "transport": "http_sse"}
                if not line:
                    break
            return {"status": "invalid_stream", "http_status": response.status}
    except HTTPError as error:
        return {"status": "http_error", "http_status": error.code}
    except (URLError, TimeoutError, OSError):
        return {"status": "connection_error"}


def main() -> int:
    result = probe(os.environ["WEBANYTHING_RESPONSES_BASE_URL"],
                   os.environ["WEBANYTHING_RESPONSES_API_KEY"], sys.argv[1])
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
