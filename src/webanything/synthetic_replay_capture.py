"""Freeze a sanitized full-capture directory into a replay manifest."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .release import atomic_json
from .synthetic_replay import SyntheticReplayContractError, sha256_file


SENSITIVE_NAMES = {
    "auth", "authorization", "cookie", "cookies", "credential", "credentials",
    "storage-state", "storage_state", "token", "tokens", "user-data", "userdata",
}
KIND_BY_SUFFIX = {
    ".html": "dom", ".htm": "dom", ".xhtml": "dom", ".css": "css",
    ".woff": "font", ".woff2": "font", ".ttf": "font", ".otf": "font",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".avif": "image",
    ".gif": "image", ".svg": "icon", ".ico": "icon",
    ".webm": "media", ".mp4": "media", ".ogg": "media", ".mp3": "media", ".wav": "media",
    ".json": "metadata", ".txt": "metadata",
}
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SyntheticReplayCaptureError(ValueError):
    """A prepared capture cannot be frozen safely."""


def trusted_dom_topology_script(topology: list[dict[str, str]]) -> str:
    normalized: list[dict[str, str]] = []
    for item in topology:
        if not isinstance(item, dict):
            raise SyntheticReplayCaptureError("DOM topology entries must be objects")
        node_id = item.get("node_id")
        parent_node_id = item.get("parent_node_id")
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
            raise SyntheticReplayCaptureError("DOM topology node_id is invalid")
        if not isinstance(parent_node_id, str) or not NODE_ID_RE.fullmatch(parent_node_id):
            raise SyntheticReplayCaptureError("DOM topology parent_node_id is invalid")
        normalized.append({"node_id": node_id, "parent_node_id": parent_node_id})
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (
        '<script data-webanything-dom-topology="v1">(()=>{const topology='
        + payload
        + ";for(const item of topology){const node=document.querySelector(`[data-webanything-node-id=\"${item.node_id}\"]`);"
        + "const parent=document.querySelector(`[data-webanything-node-id=\"${item.parent_node_id}\"]`);"
        + "if(node&&parent&&node!==parent)parent.appendChild(node);}"
        + 'document.documentElement.setAttribute("data-webanything-topology-restored","v1");})();</script>'
    )


def _without_trusted_dom_topology_script(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        complete = match.group(0)
        body = match.group(1)
        prefix = "(()=>{const topology="
        suffix = (
            ";for(const item of topology){const node=document.querySelector(`[data-webanything-node-id=\"${item.node_id}\"]`);"
            "const parent=document.querySelector(`[data-webanything-node-id=\"${item.parent_node_id}\"]`);"
            "if(node&&parent&&node!==parent)parent.appendChild(node);}"
            'document.documentElement.setAttribute("data-webanything-topology-restored","v1");})();'
        )
        if not body.startswith(prefix) or not body.endswith(suffix):
            return complete
        try:
            topology = json.loads(body[len(prefix) : -len(suffix)])
            expected = trusted_dom_topology_script(topology)
        except (ValueError, TypeError, SyntheticReplayCaptureError):
            return complete
        return "" if complete == expected else complete

    return re.sub(
        r'<script data-webanything-dom-topology="v1">(.*?)</script>',
        replace,
        text,
        flags=re.DOTALL,
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SyntheticReplayCaptureError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyntheticReplayCaptureError(f"{label} must be a JSON object")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def freeze_replay_capture(
    *,
    bundle_root: Path,
    capture_dir: Path,
    source_origin: str,
    allowed_source_origins: set[str] | None = None,
    routes_path: Path,
    privacy_report_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Inventory a capture after privacy cleanup; never acquire source data here."""
    bundle_root = bundle_root.resolve()
    capture_dir = capture_dir.resolve()
    output_path = output_path.resolve()
    if not bundle_root.is_dir() or not capture_dir.is_dir() or not _inside(capture_dir, bundle_root):
        raise SyntheticReplayCaptureError("capture directory must exist inside bundle_root")
    if capture_dir.relative_to(bundle_root).parts[:1] != ("capture",):
        raise SyntheticReplayCaptureError("capture directory must live under bundle_root/capture/")
    if not _inside(output_path, capture_dir):
        raise SyntheticReplayCaptureError("capture manifest output must live inside capture_dir")
    parsed = urlsplit(source_origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SyntheticReplayCaptureError("source_origin must be a credential-free HTTP(S) origin")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if source_origin.rstrip("/") != origin:
        raise SyntheticReplayCaptureError("source_origin must not contain a path, query, or fragment")
    allowed_origins = {origin}
    for raw_origin in allowed_source_origins or set():
        parsed_allowed = urlsplit(raw_origin)
        allowed_origin = f"{parsed_allowed.scheme}://{parsed_allowed.netloc}"
        if (
            parsed_allowed.scheme not in {"http", "https"}
            or not parsed_allowed.hostname
            or parsed_allowed.username
            or parsed_allowed.password
            or raw_origin.rstrip("/") != allowed_origin
        ):
            raise SyntheticReplayCaptureError("allowed_source_origins must contain credential-free origins")
        allowed_origins.add(allowed_origin)
    routes_payload = _load_object(routes_path.resolve(), "route map")
    routes = routes_payload.get("routes")
    if not isinstance(routes, list) or not routes:
        raise SyntheticReplayCaptureError("route map must contain routes")
    normalized_routes: list[dict[str, str]] = []
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            raise SyntheticReplayCaptureError(f"routes[{index}] must be an object")
        source_url = str(route.get("source_url") or "")
        parsed_route = urlsplit(source_url)
        if f"{parsed_route.scheme}://{parsed_route.netloc}" not in allowed_origins:
            raise SyntheticReplayCaptureError(f"routes[{index}].source_url must use an allowed source origin")
        local_path = route.get("local_path")
        if not isinstance(local_path, str) or not local_path.startswith("/") or local_path.startswith("//"):
            raise SyntheticReplayCaptureError(f"routes[{index}].local_path must be local")
        normalized_routes.append({"source_url": source_url, "local_path": local_path})
    privacy = _load_object(privacy_report_path.resolve(), "privacy report")
    if (
        privacy.get("schema") != "webanything-replay-privacy-report-v1"
        or privacy.get("status") != "passed"
        or privacy.get("source_credentials_removed") is not True
        or not isinstance(privacy.get("private_fields_removed"), int)
        or privacy["private_fields_removed"] < 0
    ):
        raise SyntheticReplayCaptureError("privacy report is not a passing replay privacy report")
    files: list[dict[str, Any]] = []
    for path in sorted(capture_dir.rglob("*")):
        if path == output_path or not path.is_file():
            continue
        if path.is_symlink():
            raise SyntheticReplayCaptureError(f"capture symlinks are forbidden: {path}")
        relative = path.relative_to(bundle_root).as_posix()
        folded_parts = {
            value
            for part in path.relative_to(capture_dir).parts
            for value in (part.casefold(), Path(part).stem.casefold())
        }
        if folded_parts.intersection(SENSITIVE_NAMES):
            raise SyntheticReplayCaptureError(f"sensitive filename is forbidden in capture: {relative}")
        suffix = path.suffix.casefold()
        if suffix in {".js", ".mjs", ".cjs"}:
            raise SyntheticReplayCaptureError(f"upstream JavaScript is forbidden in capture: {relative}")
        kind = KIND_BY_SUFFIX.get(suffix)
        if kind is None:
            raise SyntheticReplayCaptureError(f"unsupported capture file type: {relative}")
        if kind in {"dom", "icon"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            inspected = _without_trusted_dom_topology_script(text)
            if re.search(r"<script\b|\son[a-z]+\s*=|javascript\s*:", inspected, flags=re.IGNORECASE):
                raise SyntheticReplayCaptureError(f"executable upstream markup is forbidden in capture: {relative}")
        elif kind == "css":
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"@import\s+(?:url\()?['\"]?https?://|url\(\s*['\"]?https?://", text, flags=re.IGNORECASE):
                raise SyntheticReplayCaptureError(f"external CSS dependency is forbidden in capture: {relative}")
        files.append({"path": relative, "sha256": sha256_file(path), "kind": kind, "bytes": path.stat().st_size})
    if not files:
        raise SyntheticReplayCaptureError("capture directory is empty")
    manifest = {
        "schema": "webanything-replay-capture-manifest-v1",
        "source_origin": origin,
        "allowed_source_origins": sorted(allowed_origins - {origin}),
        "external_javascript_execution": False,
        "source_auth_persisted": False,
        "privacy": {
            "status": "passed",
            "source_credentials_removed": True,
            "private_fields_removed": privacy["private_fields_removed"],
            "report_sha256": sha256_file(privacy_report_path.resolve()),
        },
        "routes": normalized_routes,
        "files": files,
    }
    atomic_json(output_path, manifest)
    return manifest
