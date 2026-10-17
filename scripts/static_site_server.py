#!/usr/bin/env python3
"""Serve one synthetic static benchmark with SPA fallback and strict CSP."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


CSP = "; ".join((
    "default-src 'self'",
    "connect-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "object-src 'none'",
    "frame-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
))


def handler_for(root: Path, site_id: str) -> type[BaseHTTPRequestHandler]:
    root = root.resolve()

    class StaticBenchmarkHandler(BaseHTTPRequestHandler):
        server_version = "WebAnythingSynthetic/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}", flush=True)

        def do_HEAD(self) -> None:
            self._serve(send_body=False)

        def do_GET(self) -> None:
            self._serve(send_body=True)

        def do_POST(self) -> None:
            self.send_error(405, "synthetic static benchmarks are read-only HTTP environments")

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _serve(self, *, send_body: bool) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/_health":
                payload = json.dumps({"ok": True, "site": site_id, "runtime": "static"}, sort_keys=True).encode()
                self._headers(200, "application/json; charset=utf-8", len(payload))
                if send_body:
                    self.wfile.write(payload)
                return
            relative = unquote(parsed.path).lstrip("/")
            requested = (root / relative).resolve()
            try:
                requested.relative_to(root)
            except ValueError:
                self.send_error(404)
                return
            if parsed.path.endswith("/"):
                requested = requested / "index.html"
            if not requested.is_file():
                requested = root / "index.html"
            if not requested.is_file():
                self.send_error(404, "index.html is missing")
                return
            payload = requested.read_bytes()
            content_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self._headers(200, content_type, len(payload))
            if send_body:
                self.wfile.write(payload)

    return StaticBenchmarkHandler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"site root does not exist: {root}")
    server = ThreadingHTTPServer((args.host, args.port), handler_for(root, args.site_id))
    print(f"synthetic-site-ready site={args.site_id} port={server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
