#!/usr/bin/env python3
"""Single-port preview gateway for many local WebHarbor Flask sites.

E2B/Codex-style sandboxes are usually easier to inspect through one exposed
HTTP port than through hundreds of individual site ports. This gateway lists
live local WebHarbor listeners and proxies them under /p/<port>/.
"""

from __future__ import annotations

import argparse
import html
import http.client
import json
import mimetypes
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATEWAY_PORT = 8885
CATALOG_BASE_PORT = 40000
CONNECT_TIMEOUT_SECONDS = 0.25
PREVIEW_MANIFEST_PATH = ROOT / "preview_manifest.json"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

TEXTUAL_TYPES = (
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "application/xml",
    "image/svg+xml",
)

# Some captured pages contain thousands of hash-based inline-script entries in
# their CSP.  Forwarding a ~20 KB header through the E2B edge can yield a 502
# before the response reaches the browser.  Keep ordinary policies intact and
# only omit an oversized policy at the preview proxy boundary; the source site
# and its captured assets remain unchanged.
MAX_FORWARDED_CSP_BYTES = 12 * 1024


def load_catalog() -> dict[int, dict[str, str]]:
    path = ROOT / "site_catalog.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    sites = data.get("sites") or []
    out: dict[int, dict[str, str]] = {}
    for index, site in enumerate(sites):
        port = CATALOG_BASE_PORT + index
        out[port] = {
            "id": str(site.get("id") or f"port_{port}"),
            "name": str(site.get("display_name") or site.get("id") or f"Port {port}"),
            "tier": str(site.get("tier") or ""),
        }
    return out


def load_preview_manifest() -> dict[int, dict[str, str]]:
    """Load explicit trace/demo labels shipped with an E2B preview package."""
    if not PREVIEW_MANIFEST_PATH.is_file():
        return {}
    try:
        payload = json.loads(PREVIEW_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows: dict[int, dict[str, str]] = {}
    for item in payload.get("sites", []):
        try:
            port = int(item["port"])
        except (KeyError, TypeError, ValueError):
            continue
        rows[port] = {
            "id": str(item.get("id") or f"port_{port}"),
            "name": str(item.get("name") or item.get("display_name") or f"Port {port}"),
            "tier": str(item.get("tier") or "demo"),
            "description": str(item.get("description") or ""),
            "runtime": str(item.get("runtime") or "flask"),
        }
    return rows


CATALOG = load_catalog()
PREVIEW_MANIFEST = load_preview_manifest()

def candidate_ports() -> list[int]:
    ports = set(CATALOG)
    ports.update(PREVIEW_MANIFEST)
    ports.discard(DEFAULT_GATEWAY_PORT)
    return sorted(ports)


def can_connect(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def site_meta(port: int) -> dict[str, str]:
    # A deployed gallery manifest is the source of truth for its own ports.
    # Historical labels remain a fallback for old sandboxes that predate the
    # manifest, but must not override a newer site entry.
    meta = PREVIEW_MANIFEST.get(port) or CATALOG.get(port)
    if meta:
        return meta
    return {"id": f"port_{port}", "name": f"Port {port}", "tier": "local_listener"}


def live_sites() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for port in candidate_ports():
        if not can_connect(port):
            continue
        meta = site_meta(port)
        rows.append(
            {
                "port": port,
                "id": meta["id"],
                "name": meta["name"],
                "tier": meta["tier"],
                "description": meta.get("description", ""),
                "url": f"/p/{port}/",
            }
        )
    rows.sort(key=lambda item: (str(item["tier"]), int(item["port"])))
    return rows


def allowed_proxy_port(port: int) -> bool:
    return port in candidate_ports() and can_connect(port)


def html_index() -> bytes:
    rows = live_sites()
    row_html = "\n".join(
        "<tr>"
        f"<td>{int(row['port'])}</td>"
        f"<td><a href=\"{html.escape(str(row['url']))}\">{html.escape(str(row['name']))}</a></td>"
        f"<td>{html.escape(str(row['id']))}</td>"
        f"<td><span class=\"pill\">{html.escape(str(row['tier']))}</span></td>"
        f"<td>{html.escape(str(row.get('description') or ''))}</td>"
        "</tr>"
        for row in rows
    )
    body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WebHarbor Preview Gateway</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #101828; background: #f8fafc; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 48px 28px 64px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; letter-spacing: .01em; }}
    .note {{ max-width: 900px; color: #475467; line-height: 1.5; margin-bottom: 28px; }}
    table {{ border-collapse: collapse; width: 100%; background: white; border: 1px solid #e4e7ec; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #eaecf0; text-align: left; font-size: 14px; }}
    th {{ background: #f2f4f7; color: #344054; }}
    a {{ color: #175cd3; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .pill {{ display: inline-block; padding: 3px 8px; border-radius: 999px; background: #eef4ff; color: #3538cd; font-size: 12px; }}
  </style>
</head>
<body>
  <main><h1>WebHarbor Preview Gallery</h1>
  <p class="note">
    One externally exposed port can inspect many local mirror sites. Open a row
    below; the gateway proxies that site under <code>/p/&lt;port&gt;/</code>.
    Live sites detected: <span class="pill">{len(rows)}</span>.
  </p>
  <table>
    <thead><tr><th>Port</th><th>Site</th><th>ID</th><th>Tier</th><th>Notes</th></tr></thead>
    <tbody>{row_html}</tbody>
  </table>
</main>
</body>
</html>
"""
    return body.encode("utf-8")


def decode_text(body: bytes, content_type: str) -> str | None:
    if not any(content_type.lower().startswith(item) for item in TEXTUAL_TYPES):
        return None
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.I)
    if match:
        charset = match.group(1).strip("\"'")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def rewrite_text(text: str, port: int, content_type: str, upstream_path: str = "/") -> str:
    prefix = f"/p/{port}"
    if content_type.lower().startswith("text/html"):
        # Some captured pages retain placeholder resource hints whose href is
        # only "#" (for example a stylesheet or an image preload). Under a
        # proxy <base>, Chromium resolves that fragment to the HTML document
        # and then reports an HTML-as-resource MIME failure. These are no-op
        # upstream placeholders, so omit them at the preview boundary instead
        # of changing the frozen site candidate.
        text = re.sub(
            r"""(?is)<link\b(?=[^>]*\bhref=(?:"#"|'#'|#))[^>]*>""",
            "",
            text,
        )
        # Some replay runtimes normalize the same placeholder image href to
        # the current document path before the gateway sees it. A preload that
        # targets the document itself is never an image resource, so drop that
        # normalized form as well.
        raw_document_path = urlsplit(upstream_path).path or "/"
        document_path = re.escape(raw_document_path)
        # Flask's ProxyFix-aware replay apps may honor X-Forwarded-Prefix and
        # emit the already-prefixed form before the gateway performs its own
        # URL rewrite. Match both forms so the placeholder cannot survive as
        # an HTML-as-image request.
        proxied_document_path = re.escape(prefix + raw_document_path)
        text = re.sub(
            rf"""(?is)<link\b(?=[^>]*\brel=(?:"preload"|'preload'|preload))(?=[^>]*\bas=(?:"image"|'image'|image))(?=[^>]*\bhref=(?:"(?:{document_path}|{proxied_document_path})"|'(?:{document_path}|{proxied_document_path})'))[^>]*>""",
            "",
            text,
        )
        if "<head" in text.lower() and "<base " not in text.lower():
            text = re.sub(
                r"(?i)<head([^>]*)>",
                lambda match: f"<head{match.group(1)}><base href=\"{prefix}/\">",
                text,
                count=1,
            )
        text = re.sub(
            r"""(?i)\b(href|src|action|poster)=("|')/(?!/|p/\d+/)([^"']*)\2""",
            lambda match: f"{match.group(1)}={match.group(2)}{prefix}/{match.group(3)}{match.group(2)}",
            text,
        )
        text = re.sub(
            r"""(?i)\b(srcset)=("|')([^"']*)\2""",
            lambda match: f"{match.group(1)}={match.group(2)}{rewrite_srcset(match.group(3), prefix)}{match.group(2)}",
            text,
        )
    if content_type.lower().startswith(("text/css", "text/html", "image/svg+xml")):
        text = re.sub(
            r"url\((['\"]?)/(?!/|p/\d+/)([^)'\"\s]+)(['\"]?)\)",
            lambda match: f"url({match.group(1)}{prefix}/{match.group(2)}{match.group(3)})",
            text,
        )
    if "javascript" in content_type.lower() and site_meta(port).get("runtime") != "static":
        text = re.sub(
            r"""(["'`])/(?!/|p/\d+/)([^"'`\s<>]+)\1""",
            lambda match: f"{match.group(1)}{prefix}/{match.group(2)}{match.group(1)}",
            text,
        )
    return text


def rewrite_srcset(value: str, prefix: str) -> str:
    parts: list[str] = []
    for item in value.split(","):
        stripped = item.strip()
        if stripped.startswith("/") and not stripped.startswith("//") and not stripped.startswith(f"{prefix}/"):
            stripped = prefix + stripped
        parts.append(stripped)
    return ", ".join(parts)


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "WebHarborPreviewGateway/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - - [{time.strftime('%d/%b/%Y %H:%M:%S')}] {fmt % args}")

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_POST(self) -> None:
        self.handle_request(send_body=True)

    def do_PUT(self) -> None:
        self.handle_request(send_body=True)

    def do_PATCH(self) -> None:
        self.handle_request(send_body=True)

    def do_DELETE(self) -> None:
        self.handle_request(send_body=True)

    def handle_request(self, *, send_body: bool) -> None:
        parsed = urlsplit(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.send_bytes(200, html_index(), "text/html; charset=utf-8", send_body)
            return
        if parsed.path == "/__health":
            payload = json.dumps({"ok": True, "live_sites": len(live_sites())}, sort_keys=True).encode("utf-8")
            self.send_bytes(200, payload, "application/json", send_body)
            return
        if parsed.path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon", send_body)
            return

        match = re.match(r"^/p/(\d+)(?:/(.*))?$", parsed.path)
        if not match:
            self.send_error(404, "Use /p/<port>/ to open a local mirror")
            return
        port = int(match.group(1))
        rest = match.group(2)
        if rest is None:
            self.send_response(302)
            self.send_header("Location", f"/p/{port}/")
            self.end_headers()
            return
        if not allowed_proxy_port(port):
            self.send_error(502, f"Port {port} is not a live allowed preview port")
            return
        upstream_path = "/" + rest
        if parsed.query:
            upstream_path += "?" + parsed.query
        self.proxy_to_port(port, upstream_path, send_body=send_body)

    def proxy_to_port(self, port: int, upstream_path: str, *, send_body: bool) -> None:
        body = self.read_request_body()
        headers = self.forward_headers(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request(self.command, upstream_path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
        except OSError as exc:
            self.send_error(502, f"Upstream port {port} failed: {exc}")
            return
        finally:
            connection.close()

        content_type = response.getheader("Content-Type") or mimetypes.guess_type(upstream_path)[0] or "application/octet-stream"
        text = decode_text(response_body, content_type)
        if text is not None:
            rewritten = rewrite_text(text, port, content_type, upstream_path)
            response_body = rewritten.encode("utf-8")
            if "charset=" not in content_type.lower():
                content_type = content_type.split(";", 1)[0] + "; charset=utf-8"

        self.send_response(response.status, response.reason)
        dropped_oversized_csp = False
        for key, value in response.getheaders():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"content-length", "content-encoding", "content-type"}:
                continue
            if lower == "content-security-policy" and len(value.encode("utf-8")) > MAX_FORWARDED_CSP_BYTES:
                dropped_oversized_csp = True
                continue
            if lower == "location":
                value = self.rewrite_location(value, port)
            if lower == "set-cookie":
                value = re.sub(r"(?i)\bpath=/($|;)", f"Path=/p/{port}/\\1", value)
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("X-WebHarbor-Gateway-Port", str(port))
        if dropped_oversized_csp:
            self.send_header("X-WebHarbor-CSP-Proxy-Policy", "oversized-header-dropped")
        self.end_headers()
        if send_body and response_body:
            self.wfile.write(response_body)

    def read_request_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return None
        return self.rfile.read(length)

    def forward_headers(self, port: int) -> dict[str, str]:
        headers: dict[str, str] = {
            "Host": f"127.0.0.1:{port}",
            "Accept-Encoding": "identity",
            "X-Forwarded-Host": self.headers.get("Host", ""),
            "X-Forwarded-Prefix": f"/p/{port}",
        }
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"host", "accept-encoding", "content-length"}:
                continue
            headers[key] = value
        return headers

    def rewrite_location(self, value: str, port: int) -> str:
        split = urlsplit(value)
        prefix = f"/p/{port}"
        if not split.scheme and (value == prefix or value.startswith(prefix + "/")):
            return value
        if not split.scheme and value.startswith("/"):
            return f"{prefix}{value}"
        if split.scheme in {"http", "https"} and split.hostname in {"127.0.0.1", "localhost"}:
            path = split.path or "/"
            query = f"?{split.query}" if split.query else ""
            return f"/p/{split.port or port}{path}{query}"
        return value

    def send_bytes(self, status: int, body: bytes, content_type: str, send_body: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_GATEWAY_PORT)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    print(f"WebHarbor preview gateway listening on http://{args.host}:{args.port}/", flush=True)
    print(f"Detected {len(live_sites())} live preview ports", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
