#!/usr/bin/env python3
"""Capture one pinned public benchmark row as an explicitly historical source."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import math
import re
import sys
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from PIL import Image

from historical_geometry import (
    HistoricalGeometryArchive,
    HistoricalGeometryError,
    load_geometry_for_row,
)
from page_content_policy import content_error_reason
from prerender_snapshot import static_structure


TOOL_VERSION = "webharbor-historical-benchmark-capture-v5"
DEFAULT_DATASET = "osunlp/Multimodal-Mind2Web"
DEFAULT_REVISION = "1b4c6a8cf9f77b7a5e0d641959935c80c4a05889"
DEFAULT_CONFIG = "default"
DEFAULT_WEBLINX_DATASET = "McGill-NLP/WebLINX-full"
DEFAULT_WEBLINX_REVISION = "36ef9f79b43df50e25b7f3b68e5c9f6ccf4160e8"
DEFAULT_WEBLINX_LICENSE = "CC BY-NC-SA 4.0"
DEFAULT_WEBUI_DOM_DATASET = "gbenson/webui-dom-snapshots"
DEFAULT_WEBUI_DOM_REVISION = "3dc9dea26bb353d59d0653a2a233ff80bf4e3d8b"
DEFAULT_WEBUI_DOM_LICENSE = "CC0-1.0"
DATASET_API = "https://huggingface.co/api/datasets/{dataset}"
DATASET_FILE = "https://huggingface.co/datasets/{dataset}/resolve/{revision}/{path}?download=true"
FILTER_API = "https://datasets-server.huggingface.co/filter"
ROWS_API = "https://datasets-server.huggingface.co/rows"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w-])")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")
LONG_NUMBER_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
SENSITIVE_INPUT_TYPES = {"email", "hidden", "password", "tel"}
SENSITIVE_NAME_TOKENS = {
    "address", "card", "cc", "credential", "cvv", "email", "password",
    "phone", "secret", "session", "ssn", "token",
}
SENSITIVE_ATTRIBUTE_TOKENS = {
    "auth", "authorization", "credential", "csrf", "nonce", "password",
    "secret", "session", "token",
}
SVG_GEOMETRY_ATTRIBUTES = {
    "cx", "cy", "d", "height", "offset", "pathlength", "points",
    "preserveaspectratio", "r", "rx", "ry", "stroke-dasharray",
    "stroke-dashoffset", "stroke-miterlimit", "stroke-width", "transform",
    "viewbox", "width", "x", "x1", "x2", "y", "y1", "y2",
}


class HistoricalCaptureError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def validate_site_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", value):
        raise HistoricalCaptureError("site must contain only lowercase letters, digits, and underscores")
    return value


def validate_public_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HistoricalCaptureError("represented upstream URL must be public HTTPS without credentials")
    return value


def validate_sha256(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise HistoricalCaptureError(f"{label} must be a lowercase SHA256")
    return value


def validate_repo_component(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise HistoricalCaptureError(f"{label} contains an unsafe repository path component")
    return value


def dataset_file_url(dataset: str, revision: str, path: str) -> str:
    components = path.split("/")
    if not components or any(not component or component in {".", ".."} for component in components):
        raise HistoricalCaptureError("dataset file path is unsafe")
    encoded_path = "/".join(quote(component, safe="._-") for component in components)
    return DATASET_FILE.format(
        dataset=quote(dataset, safe="/"),
        revision=quote(revision, safe=""),
        path=encoded_path,
    )


def fetch_bytes(
    url: str,
    *,
    timeout: float = 60,
    limit: int = MAX_RESPONSE_BYTES,
    attempts: int = 4,
) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = Request(
            url,
            headers={"User-Agent": f"WebHarbor/{TOOL_VERSION}"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise HistoricalCaptureError(f"response exceeds byte limit: {url}")
                return body, response.headers.get_content_type()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 4))
    raise HistoricalCaptureError(f"request failed after {attempts} attempts: {url}: {last_error}")


def fetch_json(url: str, *, timeout: float = 60) -> tuple[dict[str, Any], bytes]:
    body, _ = fetch_bytes(url, timeout=timeout)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HistoricalCaptureError(f"response is not valid JSON: {url}") from exc
    if not isinstance(value, dict):
        raise HistoricalCaptureError(f"JSON response must be an object: {url}")
    if value.get("error"):
        raise HistoricalCaptureError(f"dataset service error: {value['error']}")
    return value, body


def dataset_revision(dataset: str) -> str:
    value, _ = fetch_json(DATASET_API.format(dataset=quote(dataset, safe="/")), timeout=30)
    revision = value.get("sha")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise HistoricalCaptureError("dataset API did not return a full revision")
    return revision


def filter_url(dataset: str, config: str, split: str, website: str, length: int) -> str:
    where = f'"website"=\'{website}\''
    return FILTER_API + "?" + urlencode({
        "dataset": dataset,
        "config": config,
        "split": split,
        "where": where,
        "offset": 0,
        "length": length,
    })


def rows_url(dataset: str, config: str, split: str, row_index: int) -> str:
    return ROWS_API + "?" + urlencode({
        "dataset": dataset,
        "config": config,
        "split": split,
        "offset": row_index,
        "length": 1,
    })


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def stable_webui_row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    image = row.get("image")
    if not isinstance(image, dict):
        raise HistoricalCaptureError("WebUI DOM row lacks screenshot metadata")
    return {
        "capture_options": row.get("capture_options"),
        "displayed_url": row.get("displayed_url"),
        "image_height": image.get("height"),
        "image_width": image.get("width"),
        "num_frames": row.get("num_frames"),
        "requested_url": row.get("requested_url"),
        "source_image_ssim": row.get("source_image_ssim"),
        "source_index": row.get("source_index"),
        "source_key_name": row.get("source_key_name"),
    }


def retrieve_rows(args: argparse.Namespace) -> tuple[dict[str, Any], bytes, str, str]:
    """Fetch one bounded API response, preferring exact-row lookup when possible."""
    if args.row_index is not None:
        mode = "rows_exact"
        query_url = rows_url(args.dataset, args.config, args.split, args.row_index)
    else:
        mode = "filter_website"
        query_url = filter_url(
            args.dataset, args.config, args.split, args.website, args.limit
        )
    response, response_body = fetch_json(query_url, timeout=args.timeout)
    return response, response_body, query_url, mode


def select_row(
    response: dict[str, Any],
    *,
    row_index: int | None = None,
    annotation_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    rows = response.get("rows")
    if not isinstance(rows, list) or not rows:
        raise HistoricalCaptureError("dataset filter returned no rows")
    candidates: list[tuple[int, dict[str, Any]]] = []
    for wrapper in rows:
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("row"), dict):
            continue
        index = wrapper.get("row_idx")
        row = wrapper["row"]
        if not isinstance(index, int):
            continue
        if row_index is not None and index != row_index:
            continue
        if annotation_id is not None and row.get("annotation_id") != annotation_id:
            continue
        if row_index is None and str(row.get("target_action_index")) != "0":
            continue
        if not isinstance(row.get("raw_html"), str) or not row["raw_html"].strip():
            continue
        screenshot = row.get("screenshot")
        if not isinstance(screenshot, dict) or not isinstance(screenshot.get("src"), str):
            continue
        candidates.append((index, row))
    if not candidates:
        qualifier = f" row {row_index}" if row_index is not None else " initial-action"
        raise HistoricalCaptureError(f"dataset filter contains no usable{qualifier} row")
    return candidates[0]


def attr_is_sensitive(attrs: dict[str, str]) -> bool:
    if attrs.get("type", "").casefold() in SENSITIVE_INPUT_TYPES:
        return True
    haystack = " ".join((attrs.get("name", ""), attrs.get("id", ""))).casefold()
    tokens = set(re.split(r"[^a-z0-9]+", haystack))
    return bool(tokens & SENSITIVE_NAME_TOKENS)


def attribute_name_is_sensitive(value: str) -> bool:
    tokens = set(re.split(r"[^a-z0-9]+", value.casefold()))
    return bool(tokens & SENSITIVE_ATTRIBUTE_TOKENS)


class HistoricalHtmlSanitizer(HTMLParser):
    """Remove executable content and redact values that can contain benchmark PII."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, geometry: dict[str, dict[str, str]] | None = None) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.blocked_depth = 0
        self.geometry = geometry or {}
        self.redactions = {
            "scripts": 0,
            "sensitive_values": 0,
            "text_patterns": 0,
            "geometry_nodes": 0,
            "geometry_attributes": 0,
        }

    def redact_text(self, value: str) -> str:
        replaced = value
        for pattern in (EMAIL_RE, PHONE_RE, LONG_NUMBER_RE):
            replaced, count = pattern.subn("[redacted]", replaced)
            self.redactions["text_patterns"] += count
        return replaced

    def handle_decl(self, decl: str) -> None:
        if not self.blocked_depth:
            self.output.append(f"<!{decl}>")

    def handle_comment(self, data: str) -> None:
        return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "noscript", "template"}:
            self.blocked_depth += 1
            self.redactions["scripts"] += 1
            return
        if self.blocked_depth:
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        node_geometry = self.geometry.get(values.get("backend_node_id", ""))
        if node_geometry is None:
            node_geometry = self.geometry.get(values.get("data-webtasks-id", ""))
        if node_geometry and node_geometry.get("tag", tag) == tag:
            additions = [
                (key, value)
                for key, value in node_geometry.items()
                if key != "tag" and key not in values
            ]
            if additions:
                attrs = [*attrs, *additions]
                values.update(additions)
                self.redactions["geometry_nodes"] += 1
                self.redactions["geometry_attributes"] += len(additions)
        form_control = tag in {"input", "textarea"}
        sensitive = form_control and attr_is_sensitive(values)
        rendered: list[str] = []
        for key, value in attrs:
            key = key.casefold()
            if key.startswith("on"):
                continue
            should_clear = (
                form_control and key in {"value", "input_value", "data-initial-value"}
            ) or attribute_name_is_sensitive(key) or (
                sensitive and key == "placeholder"
            )
            if should_clear and value:
                value = ""
                self.redactions["sensitive_values"] += 1
            if value is None:
                rendered.append(key)
            else:
                rendered_value = (
                    value
                    if tag in {
                        "circle", "clippath", "ellipse", "g", "line", "path",
                        "polygon", "polyline", "rect", "stop", "svg", "use",
                    } and key in SVG_GEOMETRY_ATTRIBUTES
                    else self.redact_text(value)
                )
                rendered.append(f'{key}="{html.escape(rendered_value, quote=True)}"')
        suffix = (" " + " ".join(rendered)) if rendered else ""
        self.output.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "noscript", "template"}:
            if not self.blocked_depth:
                self.redactions["scripts"] += 1
            return
        self.handle_starttag(tag, attrs)
        if not self.blocked_depth and lowered not in self.VOID:
            self.output.append(f"</{lowered}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "noscript", "template"}:
            if self.blocked_depth:
                self.blocked_depth -= 1
            return
        if not self.blocked_depth and tag not in self.VOID:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.output.append(self.redact_text(data))

    def handle_entityref(self, name: str) -> None:
        if not self.blocked_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.blocked_depth:
            self.output.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self.output)


def sanitize_html(
    source: str,
    *,
    geometry: dict[str, dict[str, str]] | None = None,
) -> tuple[bytes, dict[str, int]]:
    parser = HistoricalHtmlSanitizer(geometry)
    parser.feed(source)
    parser.close()
    body = parser.result().encode("utf-8")
    if len(body) > MAX_HTML_BYTES:
        raise HistoricalCaptureError("sanitized historical HTML exceeds byte limit")
    reason = content_error_reason(body.decode("utf-8", errors="replace"))
    if reason:
        raise HistoricalCaptureError(f"historical row is an error page: {reason}")
    if not static_structure(body)["meaningful"]:
        raise HistoricalCaptureError("historical row lacks meaningful static structure")
    return body, parser.redactions


def screenshot_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        return image.size


def validate_screenshot_bytes(body: bytes, reported_type: str) -> tuple[int, int, str, str]:
    try:
        with Image.open(io.BytesIO(body)) as image:
            image.verify()
            image_format = (image.format or "").upper()
        with Image.open(io.BytesIO(body)) as image:
            width, height = image.size
    except (OSError, ValueError) as exc:
        raise HistoricalCaptureError("historical screenshot bytes are not a valid image") from exc
    format_types = {
        "JPEG": (".jpg", "image/jpeg"),
        "PNG": (".png", "image/png"),
        "WEBP": (".webp", "image/webp"),
    }
    if image_format not in format_types:
        raise HistoricalCaptureError(f"historical screenshot format is unsupported: {image_format}")
    suffix, detected_type = format_types[image_format]
    content_type = reported_type if reported_type.startswith("image/") else detected_type
    return width, height, suffix, content_type


def relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def verified_dataset_file(
    *,
    dataset: str,
    revision: str,
    path: str,
    expected_sha256: str,
    timeout: float,
    limit: int,
) -> tuple[bytes, str, str]:
    url = dataset_file_url(dataset, revision, path)
    body, content_type = fetch_bytes(url, timeout=timeout, limit=limit)
    observed = sha256_bytes(body)
    if observed != expected_sha256:
        raise HistoricalCaptureError(
            f"dataset file hash differs for {path}: expected {expected_sha256}, observed {observed}"
        )
    return body, content_type, url


def validate_weblinx_replay(
    body: bytes,
    *,
    page: str,
    screenshot: str,
    upstream_url: str,
    screenshot_size: tuple[int, int],
) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HistoricalCaptureError("WebLINX replay is not valid JSON") from exc
    events = value.get("data") if isinstance(value, dict) else None
    if not isinstance(events, list):
        raise HistoricalCaptureError("WebLINX replay does not contain an event list")
    matches: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("type") != "browser":
            continue
        state = event.get("state")
        if not isinstance(state, dict):
            continue
        if state.get("page") == page and state.get("screenshot") == screenshot:
            action = event.get("action")
            if isinstance(action, dict):
                matches.append((index, event, action))
    if len(matches) != 1:
        raise HistoricalCaptureError(
            "WebLINX replay must identify exactly one event for the selected page and screenshot"
        )
    index, event, action = matches[0]
    metadata = action.get("arguments", {}).get("metadata", {})
    if not isinstance(metadata, dict) or metadata.get("url") != upstream_url:
        raise HistoricalCaptureError("WebLINX selected page URL differs from represented upstream URL")
    prior_browser_events = [
        prior for prior in events[:index]
        if isinstance(prior, dict) and prior.get("type") == "browser"
    ]
    if any(
        prior.get("action", {}).get("intent") != "load"
        or prior.get("state", {}).get("page") is not None
        for prior in prior_browser_events
    ):
        raise HistoricalCaptureError(
            "WebLINX selected page is not the first public browser state before interaction"
        )
    intent = str(action.get("intent") or "").casefold()
    if intent in {"paste", "submit", "textinput", "type", "upload"}:
        raise HistoricalCaptureError("WebLINX selected page is attached to a sensitive input action")
    viewport_width = metadata.get("viewportWidth")
    viewport_height = metadata.get("viewportHeight")
    zoom = metadata.get("zoomLevel", 1)
    if not all(isinstance(item, (int, float)) for item in (viewport_width, viewport_height, zoom)):
        raise HistoricalCaptureError("WebLINX replay lacks numeric desktop viewport metadata")
    viewport_width = int(viewport_width)
    viewport_height = int(viewport_height)
    zoom = float(zoom)
    if viewport_width < 1024 or viewport_height < 600 or not 0.5 <= zoom <= 4:
        raise HistoricalCaptureError("WebLINX selected page is not a supported desktop observation")
    expected_width = viewport_width * zoom
    expected_height = viewport_height * zoom
    width, height = screenshot_size
    if abs(width - expected_width) > 2 or abs(height - expected_height) > 2:
        raise HistoricalCaptureError(
            "WebLINX screenshot dimensions do not match replay viewport and zoom metadata"
        )
    return {
        "action_intent": action.get("intent"),
        "event_id": action.get("event_id", event.get("event_id")),
        "event_index": index,
        "page": page,
        "screenshot": screenshot,
        "url": upstream_url,
        "viewport_height": viewport_height,
        "viewport_width": viewport_width,
        "zoom_level": zoom,
        "public_pre_authentication_state": True,
        "replay_retained": False,
    }


def weblinx_geometry(body: bytes, *, scale: float) -> dict[str, dict[str, str]]:
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HistoricalCaptureError("WebLINX bbox file is not valid JSON") from exc
    if not isinstance(value, dict) or not value:
        raise HistoricalCaptureError("WebLINX bbox file must be a non-empty object")
    output: dict[str, dict[str, str]] = {}
    for node_id, raw in value.items():
        if not isinstance(node_id, str) or not isinstance(raw, dict):
            raise HistoricalCaptureError("WebLINX bbox file contains an invalid node")
        coordinates = tuple(raw.get(name) for name in ("x", "y", "width", "height"))
        if not all(isinstance(item, (int, float)) and math.isfinite(item) for item in coordinates):
            raise HistoricalCaptureError(f"WebLINX bbox node {node_id!r} has invalid coordinates")
        x, y, width, height = (float(item) * scale for item in coordinates)
        if width <= 0 or height <= 0:
            raise HistoricalCaptureError(f"WebLINX bbox node {node_id!r} has non-positive dimensions")
        output[node_id] = {
            "bounding_box_rect": ",".join(f"{item:.6f}" for item in (x, y, width, height))
        }
    return output


def webui_snapshot_string(
    strings: list[Any],
    index: Any,
    label: str,
    *,
    allow_missing: bool = False,
) -> str:
    if index == -1 and allow_missing:
        return ""
    if not isinstance(index, int) or index < 0 or index >= len(strings):
        raise HistoricalCaptureError(f"WebUI DOM snapshot has invalid {label} string index")
    value = strings[index]
    if not isinstance(value, str):
        raise HistoricalCaptureError(f"WebUI DOM snapshot {label} is not a string")
    return value


def webui_sparse_values(
    value: Any,
    *,
    strings: list[Any],
    label: str,
) -> dict[int, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise HistoricalCaptureError(f"WebUI DOM snapshot {label} map is invalid")
    indexes = value.get("index", [])
    values = value.get("value", [])
    if not isinstance(indexes, list) or not isinstance(values, list) or len(indexes) != len(values):
        raise HistoricalCaptureError(f"WebUI DOM snapshot {label} map is inconsistent")
    output: dict[int, str] = {}
    for node_index, string_index in zip(indexes, values):
        if not isinstance(node_index, int) or node_index < 0:
            raise HistoricalCaptureError(f"WebUI DOM snapshot {label} node index is invalid")
        output[node_index] = webui_snapshot_string(
            strings, string_index, label, allow_missing=True
        )
    return output


def webui_utf16_fragment(value: str, start: Any, length: Any) -> str:
    if (
        not isinstance(start, int)
        or not isinstance(length, int)
        or start < 0
        or length < 0
    ):
        raise HistoricalCaptureError("WebUI DOM snapshot text box range is invalid")
    encoded = value.encode("utf-16-le")
    begin = start * 2
    end = (start + length) * 2
    if end > len(encoded):
        raise HistoricalCaptureError("WebUI DOM snapshot text box exceeds layout text")
    try:
        return encoded[begin:end].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise HistoricalCaptureError(
            "WebUI DOM snapshot text box splits a UTF-16 character"
        ) from exc


def webui_dom_snapshot_html(
    snapshot: Any,
    *,
    capture_url: str,
    computed_style_names: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(snapshot, dict):
        raise HistoricalCaptureError("WebUI DOM snapshot must be an object")
    strings = snapshot.get("strings")
    documents = snapshot.get("documents")
    if not isinstance(strings, list) or not isinstance(documents, list) or len(documents) != 1:
        raise HistoricalCaptureError("WebUI DOM snapshot must contain one document and a string table")
    document = documents[0]
    if not isinstance(document, dict):
        raise HistoricalCaptureError("WebUI DOM snapshot document is invalid")
    document_url = webui_snapshot_string(
        strings, document.get("documentURL"), "document URL"
    )
    if document_url != capture_url:
        raise HistoricalCaptureError(
            "WebUI DOM snapshot document URL differs from the selected capture URL"
        )
    nodes = document.get("nodes")
    layout = document.get("layout")
    if not isinstance(nodes, dict) or not isinstance(layout, dict):
        raise HistoricalCaptureError("WebUI DOM snapshot lacks nodes or layout")
    parent_indexes = nodes.get("parentIndex")
    node_types = nodes.get("nodeType")
    node_names = nodes.get("nodeName")
    node_values = nodes.get("nodeValue")
    backend_ids = nodes.get("backendNodeId")
    attributes = nodes.get("attributes")
    arrays = (parent_indexes, node_types, node_names, node_values, backend_ids, attributes)
    if not all(isinstance(value, list) for value in arrays):
        raise HistoricalCaptureError("WebUI DOM snapshot node arrays are invalid")
    node_count = len(parent_indexes)
    if node_count == 0 or any(len(value) != node_count for value in arrays[1:]):
        raise HistoricalCaptureError("WebUI DOM snapshot node arrays are empty or inconsistent")

    layout_indexes = layout.get("nodeIndex")
    layout_bounds = layout.get("bounds")
    layout_text = layout.get("text")
    layout_styles = layout.get("styles")
    if (
        not isinstance(layout_indexes, list)
        or not isinstance(layout_bounds, list)
        or not isinstance(layout_text, list)
        or not isinstance(layout_styles, list)
        or len(layout_indexes) != len(layout_bounds)
        or len(layout_indexes) != len(layout_text)
        or len(layout_indexes) != len(layout_styles)
    ):
        raise HistoricalCaptureError("WebUI DOM snapshot layout arrays are inconsistent")
    bounds_by_node: dict[int, tuple[float, float, float, float]] = {}
    for node_index, raw_bounds in zip(layout_indexes, layout_bounds):
        if (
            not isinstance(node_index, int)
            or not 0 <= node_index < node_count
            or not isinstance(raw_bounds, list)
            or len(raw_bounds) != 4
            or not all(isinstance(item, (int, float)) and math.isfinite(item) for item in raw_bounds)
        ):
            raise HistoricalCaptureError("WebUI DOM snapshot contains invalid layout bounds")
        bounds_by_node[node_index] = tuple(float(item) for item in raw_bounds)

    text_boxes = document.get("textBoxes")
    if not isinstance(text_boxes, dict):
        raise HistoricalCaptureError("WebUI DOM snapshot lacks text box geometry")
    text_box_layout_indexes = text_boxes.get("layoutIndex")
    text_box_bounds = text_boxes.get("bounds")
    text_box_starts = text_boxes.get("start")
    text_box_lengths = text_boxes.get("length")
    text_box_arrays = (
        text_box_layout_indexes,
        text_box_bounds,
        text_box_starts,
        text_box_lengths,
    )
    if (
        not all(isinstance(value, list) for value in text_box_arrays)
        or len({len(value) for value in text_box_arrays}) != 1
    ):
        raise HistoricalCaptureError("WebUI DOM snapshot text box arrays are inconsistent")
    text_fragments_by_node: dict[int, list[tuple[tuple[float, float, float, float], str]]] = {}
    for layout_index, raw_bounds, start, length in zip(*text_box_arrays):
        if (
            not isinstance(layout_index, int)
            or not 0 <= layout_index < len(layout_indexes)
            or not isinstance(raw_bounds, list)
            or len(raw_bounds) != 4
            or not all(
                isinstance(item, (int, float)) and math.isfinite(item)
                for item in raw_bounds
            )
        ):
            raise HistoricalCaptureError("WebUI DOM snapshot contains an invalid text box")
        x, y, width, height = (float(item) for item in raw_bounds)
        if width <= 0 or height <= 0:
            continue
        text_index = layout_text[layout_index]
        layout_value = webui_snapshot_string(
            strings, text_index, "layout text", allow_missing=True
        )
        fragment = webui_utf16_fragment(layout_value, start, length)
        if not fragment.strip():
            continue
        node_index = layout_indexes[layout_index]
        text_fragments_by_node.setdefault(node_index, []).append(
            ((x, y, width, height), fragment)
        )

    style_names = computed_style_names or []
    if not all(
        isinstance(name, str)
        and name in {"overflow-x", "overflow-y", "visibility"}
        for name in style_names
    ):
        raise HistoricalCaptureError("WebUI DOM snapshot computed style names are unsupported")
    computed_styles_by_node: dict[int, list[str]] = {}
    for layout_index, node_index in enumerate(layout_indexes):
        raw_styles = layout_styles[layout_index]
        if raw_styles == [] and style_names:
            continue
        if not isinstance(raw_styles, list) or len(raw_styles) != len(style_names):
            raise HistoricalCaptureError("WebUI DOM snapshot computed styles are inconsistent")
        declarations: list[str] = []
        for name, string_index in zip(style_names, raw_styles):
            value = webui_snapshot_string(
                strings, string_index, f"computed {name}", allow_missing=True
            ).casefold()
            if name == "visibility" and value in {"hidden", "collapse"}:
                declarations.append(f"{name}:{value}")
            elif name in {"overflow-x", "overflow-y"} and value in {
                "auto", "hidden", "scroll",
            }:
                declarations.append(f"{name}:{value}")
        if declarations:
            computed_styles_by_node[node_index] = declarations

    clickable = nodes.get("isClickable", {}).get("index", [])
    if not isinstance(clickable, list) or not all(
        isinstance(index, int) and 0 <= index < node_count for index in clickable
    ):
        raise HistoricalCaptureError("WebUI DOM snapshot clickable map is invalid")
    clickable_indexes = set(clickable)
    input_values = webui_sparse_values(
        nodes.get("inputValue"), strings=strings, label="input value"
    )
    checked_indexes = set(nodes.get("inputChecked", {}).get("index", []))
    selected_indexes = set(nodes.get("optionSelected", {}).get("index", []))
    if not all(
        isinstance(index, int) and 0 <= index < node_count
        for index in checked_indexes | selected_indexes
    ):
        raise HistoricalCaptureError("WebUI DOM snapshot control state map is invalid")

    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for index, parent in enumerate(parent_indexes):
        if parent == -1:
            roots.append(index)
        elif isinstance(parent, int) and 0 <= parent < node_count and parent != index:
            children.setdefault(parent, []).append(index)
        else:
            raise HistoricalCaptureError("WebUI DOM snapshot parent index is invalid")
    if not roots:
        raise HistoricalCaptureError("WebUI DOM snapshot has no root node")

    geometry_nodes = 0
    rendered_nodes = 0
    rendered_text_nodes = 0
    rendered_text_fragments = 0
    multi_fragment_text_nodes = sum(
        len(fragments) > 1 for fragments in text_fragments_by_node.values()
    )

    def render_text_fragments(index: int) -> str:
        nonlocal geometry_nodes, rendered_text_fragments, rendered_text_nodes
        fragments = text_fragments_by_node.get(index, [])
        if not fragments:
            return ""
        backend_id = backend_ids[index]
        backend_attr = (
            f' backend_node_id="{backend_id}"'
            if isinstance(backend_id, int) and backend_id >= 0
            else ""
        )
        output: list[str] = []
        for bounds, fragment in fragments:
            bounds_attr = ",".join(f"{item:.6f}" for item in bounds)
            output.append(
                f'<text{backend_attr} bounding_box_rect="{bounds_attr}">'
                f"{html.escape(fragment, quote=False)}</text>"
            )
        geometry_nodes += len(output)
        rendered_text_fragments += len(output)
        rendered_text_nodes += 1
        return "".join(output)

    def render(index: int, ancestry: set[int]) -> str:
        nonlocal geometry_nodes, rendered_nodes, rendered_text_fragments, rendered_text_nodes
        if index in ancestry:
            raise HistoricalCaptureError("WebUI DOM snapshot contains a parent cycle")
        ancestry = {*ancestry, index}
        node_type = node_types[index]
        name = webui_snapshot_string(strings, node_names[index], "node name")
        value = webui_snapshot_string(
            strings, node_values[index], "node value", allow_missing=True
        )
        if node_type in {9, 11}:
            return "".join(render(child, ancestry) for child in children.get(index, []))
        if node_type == 10:
            return "<!doctype html>"
        if node_type == 3:
            fragments = render_text_fragments(index)
            if fragments:
                return fragments
            bounds = bounds_by_node.get(index)
            if bounds is None or not value.strip():
                return ""
            backend_id = backend_ids[index]
            backend_attr = (
                f' backend_node_id="{backend_id}"'
                if isinstance(backend_id, int) and backend_id >= 0
                else ""
            )
            bounds_attr = ",".join(f"{item:.6f}" for item in bounds)
            geometry_nodes += 1
            rendered_text_nodes += 1
            rendered_text_fragments += 1
            return (
                f'<text{backend_attr} bounding_box_rect="{bounds_attr}">'
                f"{html.escape(value, quote=False)}</text>"
            )
        if node_type in {7, 8}:
            return ""
        if node_type != 1:
            return "".join(render(child, ancestry) for child in children.get(index, []))
        tag = name.casefold()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", tag):
            fragments = render_text_fragments(index)
            if fragments:
                return fragments
            return "".join(render(child, ancestry) for child in children.get(index, []))
        raw_attributes = attributes[index]
        if not isinstance(raw_attributes, list) or len(raw_attributes) % 2:
            raise HistoricalCaptureError("WebUI DOM snapshot element attributes are invalid")
        rendered_attributes: list[tuple[str, str | None]] = []
        existing: set[str] = set()
        for offset in range(0, len(raw_attributes), 2):
            key = webui_snapshot_string(strings, raw_attributes[offset], "attribute name").casefold()
            attr_value = webui_snapshot_string(
                strings,
                raw_attributes[offset + 1],
                "attribute value",
                allow_missing=True,
            )
            if not re.fullmatch(r"[a-z_:][a-z0-9_.:-]*", key) or key in existing:
                continue
            existing.add(key)
            rendered_attributes.append((key, attr_value))
        computed_styles = computed_styles_by_node.get(index, [])
        if computed_styles:
            style_index = next(
                (offset for offset, (key, _) in enumerate(rendered_attributes) if key == "style"),
                None,
            )
            if style_index is None:
                rendered_attributes.append(("style", ";".join(computed_styles)))
            else:
                existing_style = rendered_attributes[style_index][1] or ""
                existing_names = {
                    declaration.split(":", 1)[0].strip().casefold()
                    for declaration in existing_style.split(";")
                    if ":" in declaration
                }
                additions = [
                    declaration for declaration in computed_styles
                    if declaration.split(":", 1)[0] not in existing_names
                ]
                if additions:
                    rendered_attributes[style_index] = (
                        "style",
                        ";".join(filter(None, (existing_style.rstrip(";"), *additions))),
                    )
        backend_id = backend_ids[index]
        if isinstance(backend_id, int) and backend_id >= 0 and "backend_node_id" not in existing:
            rendered_attributes.append(("backend_node_id", str(backend_id)))
        bounds = bounds_by_node.get(index)
        if bounds is not None and "bounding_box_rect" not in existing:
            rendered_attributes.append((
                "bounding_box_rect",
                ",".join(f"{item:.6f}" for item in bounds),
            ))
            geometry_nodes += 1
        if index in clickable_indexes and "is_clickable" not in existing:
            rendered_attributes.append(("is_clickable", "true"))
        if index in input_values and "input_value" not in existing:
            rendered_attributes.append(("input_value", input_values[index]))
        if index in checked_indexes and "checked" not in existing:
            rendered_attributes.append(("checked", None))
        if index in selected_indexes and "selected" not in existing:
            rendered_attributes.append(("selected", None))
        attrs_text = "".join(
            f" {key}" if attr_value is None
            else f' {key}="{html.escape(attr_value, quote=True)}"'
            for key, attr_value in rendered_attributes
        )
        rendered_nodes += 1
        if tag in HistoricalHtmlSanitizer.VOID:
            return f"<{tag}{attrs_text}>"
        content = "".join(render(child, ancestry) for child in children.get(index, []))
        return f"<{tag}{attrs_text}>{content}</{tag}>"

    source = "".join(render(index, set()) for index in roots)
    return source, {
        "document_url": document_url,
        "documents": len(documents),
        "frames_excluded": 0,
        "geometry_nodes": geometry_nodes,
        "layout_nodes": len(bounds_by_node),
        "node_count": node_count,
        "computed_style_nodes": len(computed_styles_by_node),
        "multi_fragment_text_nodes": multi_fragment_text_nodes,
        "rendered_element_nodes": rendered_nodes,
        "rendered_text_fragments": rendered_text_fragments,
        "rendered_text_nodes": rendered_text_nodes,
        "text_boxes": len(text_box_layout_indexes),
    }


def write_evidence(args: argparse.Namespace, root: Path, site: str, evidence: dict[str, Any]) -> None:
    evidence_path = (
        args.evidence
        or root / "mirror_factory" / "evidence" / site / "historical-source.json"
    ).resolve()
    expected_parent = (root / "mirror_factory" / "evidence" / site).resolve()
    if expected_parent not in evidence_path.parents and evidence_path != expected_parent:
        raise HistoricalCaptureError("evidence output must stay under the site's evidence directory")
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def capture_mind2web(args: argparse.Namespace) -> dict[str, Any]:
    root = repository_root()
    site = validate_site_id(args.site)
    upstream_url = validate_public_https_url(args.upstream_url)
    current_revision = dataset_revision(args.dataset)
    if current_revision != args.revision:
        raise HistoricalCaptureError(
            f"dataset revision changed: expected {args.revision}, resolved {current_revision}"
        )
    response, response_body, query_url, retrieval_mode = retrieve_rows(args)
    index, row = select_row(
        response,
        row_index=args.row_index,
        annotation_id=args.annotation_id,
    )
    if row.get("website") != args.website:
        raise HistoricalCaptureError(
            f"historical row website differs from requested site: {row.get('website')!r}"
        )
    raw_source = row["raw_html"].encode("utf-8")
    if len(raw_source) > MAX_HTML_BYTES:
        raise HistoricalCaptureError("historical raw HTML exceeds byte limit")
    geometry: dict[str, dict[str, str]] | None = None
    geometry_evidence: dict[str, Any] | None = None
    if row["raw_html"].count("bounding_box_rect=") < 20:
        try:
            geometry, geometry_evidence = load_geometry_for_row(
                row,
                split=args.split,
                archive=HistoricalGeometryArchive(
                    cache_root=root / ".assets" / "historical-geometry",
                    timeout=args.timeout,
                ),
            )
        except HistoricalGeometryError as exc:
            raise HistoricalCaptureError(
                f"historical row lacks embedded geometry and enrichment failed: {exc}"
            ) from exc
    sanitized, redactions = sanitize_html(row["raw_html"], geometry=geometry)

    cache_root = root / "sites" / site / "static" / "external_cache" / "upstream"
    stem = f"{args.split}-{index}"
    page_path = cache_root / "historical" / "pages" / f"{stem}.html"
    screenshot_body, screenshot_type = fetch_bytes(
        row["screenshot"]["src"],
        timeout=args.timeout,
        limit=MAX_SCREENSHOT_BYTES,
    )
    width, height, screenshot_suffix, screenshot_type = validate_screenshot_bytes(
        screenshot_body, screenshot_type
    )
    screenshot_path = cache_root / "historical" / "references" / f"{stem}{screenshot_suffix}"

    page_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_bytes(sanitized)
    screenshot_path.write_bytes(screenshot_body)
    if screenshot_dimensions(screenshot_path) != (width, height):
        raise HistoricalCaptureError("stored screenshot dimensions changed after validation")
    declared_width = int(row["screenshot"].get("width") or width)
    declared_height = int(row["screenshot"].get("height") or height)
    if (width, height) != (declared_width, declared_height):
        raise HistoricalCaptureError(
            f"screenshot dimensions differ from dataset metadata: {(width, height)} != "
            f"{(declared_width, declared_height)}"
        )

    parsed = urlsplit(upstream_url)
    page_record = {
        "bytes": len(sanitized),
        "content_type": "text/html; charset=utf-8",
        "final_url": upstream_url,
        "historical_row_index": index,
        "local_path": relative_path(page_path, cache_root),
        "sha256": sha256_bytes(sanitized),
        "source_page_sha256": sha256_bytes(raw_source),
        "status": 200,
        "url": upstream_url,
    }
    evidence = {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "phase": "historical_source",
        "site": site,
        "generated_at": utc_now(),
        "status": "passed",
        "source_mode": "historical_benchmark_snapshot",
        "authorization": {
            "basis": "user_directed_bounded_private_research_snapshot",
            "publication": "not_authorized",
            "scope": "one public benchmark row and its published screenshot",
        },
        "scope": {
            "upstream_url": upstream_url,
            "start_urls": [upstream_url],
            "page_hosts": [parsed.hostname],
            "asset_hosts": [],
            "generated_get_queries": [],
        },
        "source": {
            "provider": "mind2web",
            "kind": "hf_dataset_row",
            "dataset": args.dataset,
            "revision": args.revision,
            "config": args.config,
            "split": args.split,
            "website": args.website,
            "row_index": index,
            "action_uid": row.get("action_uid"),
            "annotation_id": row.get("annotation_id"),
            "target_action_index": row.get("target_action_index"),
            "confirmed_task": row.get("confirmed_task"),
            "dataset_response": {
                "bytes": len(response_body),
                "mode": retrieval_mode,
                "sha256": sha256_bytes(response_body),
                "url": query_url,
            },
            "raw_html": {
                "bytes": len(raw_source),
                "sha256": sha256_bytes(raw_source),
                "stored": False,
                "reason": "only a sanitized static derivative is retained",
            },
            "sanitization": redactions,
            "license": "not_declared_by_dataset_metadata",
            **({"geometry_source": geometry_evidence} if geometry_evidence else {}),
        },
        "pages": [page_record],
        "assets": [],
        "screenshot": {
            "bytes": len(screenshot_body),
            "content_type": screenshot_type,
            "height": height,
            "local_path": relative_path(screenshot_path, root),
            "sha256": sha256_bytes(screenshot_body),
            "width": width,
        },
        "failures": [],
        "limitations": [
            "the source is a historical benchmark observation, not a current live-site capture",
            "the benchmark row does not provide a canonical page URL or original network assets",
            "production-site drift is not measured by this evidence",
            "publication rights for retained HTML and screenshot bytes are not asserted",
        ],
    }
    write_evidence(args, root, site, evidence)
    return evidence


def capture_weblinx(args: argparse.Namespace) -> dict[str, Any]:
    root = repository_root()
    site = validate_site_id(args.site)
    upstream_url = validate_public_https_url(args.upstream_url)
    demonstration = validate_repo_component(args.demonstration, "demonstration")
    page = validate_repo_component(args.page, "page")
    bbox = validate_repo_component(args.bbox, "bbox")
    screenshot = validate_repo_component(args.screenshot, "screenshot")
    html_sha256 = validate_sha256(args.html_sha256, "HTML hash")
    bbox_sha256 = validate_sha256(args.bbox_sha256, "bbox hash")
    screenshot_sha256 = validate_sha256(args.screenshot_sha256, "screenshot hash")
    current_revision = dataset_revision(args.dataset)
    if current_revision != args.revision:
        raise HistoricalCaptureError(
            f"dataset revision changed: expected {args.revision}, resolved {current_revision}"
        )

    prefix = f"demonstrations/{demonstration}"
    html_dataset_path = f"{prefix}/pages/{page}"
    bbox_dataset_path = f"{prefix}/bboxes/{bbox}"
    screenshot_dataset_path = f"{prefix}/screenshots/{screenshot}"
    replay_dataset_path = f"{prefix}/replay.json"
    raw_source, html_type, html_url = verified_dataset_file(
        dataset=args.dataset,
        revision=args.revision,
        path=html_dataset_path,
        expected_sha256=html_sha256,
        timeout=args.timeout,
        limit=MAX_HTML_BYTES,
    )
    bbox_body, bbox_type, bbox_url = verified_dataset_file(
        dataset=args.dataset,
        revision=args.revision,
        path=bbox_dataset_path,
        expected_sha256=bbox_sha256,
        timeout=args.timeout,
        limit=MAX_RESPONSE_BYTES,
    )
    screenshot_body, screenshot_type, screenshot_url = verified_dataset_file(
        dataset=args.dataset,
        revision=args.revision,
        path=screenshot_dataset_path,
        expected_sha256=screenshot_sha256,
        timeout=args.timeout,
        limit=MAX_SCREENSHOT_BYTES,
    )
    replay_url = dataset_file_url(args.dataset, args.revision, replay_dataset_path)
    replay_body, _ = fetch_bytes(
        replay_url,
        timeout=args.timeout,
        limit=MAX_RESPONSE_BYTES,
    )
    width, height, screenshot_suffix, screenshot_type = validate_screenshot_bytes(
        screenshot_body, screenshot_type
    )
    replay_validation = validate_weblinx_replay(
        replay_body,
        page=page,
        screenshot=screenshot,
        upstream_url=upstream_url,
        screenshot_size=(width, height),
    )
    geometry = weblinx_geometry(bbox_body, scale=replay_validation["zoom_level"])
    try:
        source_text = raw_source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HistoricalCaptureError("WebLINX HTML is not valid UTF-8") from exc
    sanitized, redactions = sanitize_html(source_text, geometry=geometry)
    if redactions["geometry_nodes"] < 20:
        raise HistoricalCaptureError(
            "WebLINX HTML and bbox file have fewer than 20 matching design elements"
        )

    cache_root = root / "sites" / site / "static" / "external_cache" / "upstream"
    stem = f"weblinx-{demonstration}-{Path(page).stem}"
    page_path = cache_root / "historical" / "pages" / f"{stem}.html"
    screenshot_path = cache_root / "historical" / "references" / f"{stem}{screenshot_suffix}"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_bytes(sanitized)
    screenshot_path.write_bytes(screenshot_body)
    if screenshot_dimensions(screenshot_path) != (width, height):
        raise HistoricalCaptureError("stored screenshot dimensions changed after validation")

    parsed = urlsplit(upstream_url)
    page_record = {
        "bytes": len(sanitized),
        "content_type": "text/html; charset=utf-8",
        "final_url": upstream_url,
        "historical_demonstration": demonstration,
        "historical_page": page,
        "local_path": relative_path(page_path, cache_root),
        "sha256": sha256_bytes(sanitized),
        "source_page_sha256": html_sha256,
        "status": 200,
        "url": upstream_url,
    }
    evidence = {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "phase": "historical_source",
        "site": site,
        "generated_at": utc_now(),
        "status": "passed",
        "source_mode": "historical_benchmark_snapshot",
        "authorization": {
            "basis": "user_directed_bounded_private_research_snapshot",
            "publication": "not_authorized",
            "scope": "one public pre-authentication benchmark page, bbox map, and screenshot",
        },
        "scope": {
            "upstream_url": upstream_url,
            "start_urls": [upstream_url],
            "page_hosts": [parsed.hostname],
            "asset_hosts": [],
            "generated_get_queries": [],
        },
        "source": {
            "provider": "weblinx",
            "kind": "hf_dataset_demonstration_files",
            "dataset": args.dataset,
            "revision": args.revision,
            "demonstration": demonstration,
            "license": args.license,
            "files": {
                "html": {
                    "bytes": len(raw_source),
                    "content_type": html_type,
                    "path": html_dataset_path,
                    "sha256": html_sha256,
                    "stored": False,
                    "url": html_url,
                },
                "bbox": {
                    "bytes": len(bbox_body),
                    "content_type": bbox_type,
                    "path": bbox_dataset_path,
                    "sha256": bbox_sha256,
                    "stored": False,
                    "url": bbox_url,
                },
                "screenshot": {
                    "bytes": len(screenshot_body),
                    "content_type": screenshot_type,
                    "path": screenshot_dataset_path,
                    "sha256": screenshot_sha256,
                    "stored": True,
                    "url": screenshot_url,
                },
                "replay": {
                    "path": replay_dataset_path,
                    "retained": False,
                    "url": replay_url,
                    "reason": "used only in memory to validate the public pre-authentication page URL",
                },
            },
            "replay_validation": replay_validation,
            "sanitization": redactions,
            "geometry_source": {
                "attribute": "data-webtasks-id",
                "bbox_nodes": len(geometry),
                "matched_html_nodes": redactions["geometry_nodes"],
                "scale": replay_validation["zoom_level"],
                "source_sha256": bbox_sha256,
            },
        },
        "pages": [page_record],
        "assets": [],
        "screenshot": {
            "bytes": len(screenshot_body),
            "content_type": screenshot_type,
            "height": height,
            "local_path": relative_path(screenshot_path, root),
            "sha256": screenshot_sha256,
            "width": width,
        },
        "failures": [],
        "limitations": [
            "the source is a historical benchmark observation, not a current live-site capture",
            "HTML structure, classes, text, controls, semantics, and bbox geometry are the primary design source",
            "the screenshot is retained only as a desktop calibration and bounded local-asset reference",
            "the full reference screenshot is never served by the mirror",
            "the selected replay state predates authentication and no replay conversation or credentials are retained",
            "production-site drift is not measured by this evidence",
            "publication rights for retained derivatives and screenshot bytes are not asserted",
        ],
    }
    write_evidence(args, root, site, evidence)
    return evidence


def same_origin(left: str, right: str) -> bool:
    left_url = urlsplit(left)
    right_url = urlsplit(right)
    return (
        left_url.scheme.casefold(), left_url.hostname, left_url.port
    ) == (
        right_url.scheme.casefold(), right_url.hostname, right_url.port
    )


def validate_webui_image_url(
    value: str,
    *,
    dataset: str,
    revision: str,
    config: str,
    split: str,
    row_index: int,
) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "datasets-server.huggingface.co"
        or parsed.username
        or parsed.password
    ):
        raise HistoricalCaptureError("WebUI screenshot URL is outside the dataset service")
    expected = f"/{dataset}/--/{revision}/--/{config}/{split}/{row_index}/image/"
    if expected not in unquote(parsed.path):
        raise HistoricalCaptureError("WebUI screenshot URL does not match the pinned row identity")
    return value


def capture_webui_dom_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    root = repository_root()
    site = validate_site_id(args.site)
    upstream_url = validate_public_https_url(args.upstream_url)
    capture_url = validate_public_https_url(args.capture_url)
    if not same_origin(upstream_url, capture_url):
        raise HistoricalCaptureError("WebUI capture URL must use the canonical upstream origin")
    dom_sha256 = validate_sha256(args.dom_sha256, "DOM snapshot hash")
    row_metadata_sha256 = validate_sha256(
        args.row_metadata_sha256, "row metadata hash"
    )
    screenshot_sha256 = validate_sha256(
        args.screenshot_sha256, "screenshot hash"
    )
    if args.row_index is None or args.row_index < 0:
        raise HistoricalCaptureError("WebUI capture requires a non-negative row index")
    current_revision = dataset_revision(args.dataset)
    if current_revision != args.revision:
        raise HistoricalCaptureError(
            f"dataset revision changed: expected {args.revision}, resolved {current_revision}"
        )

    query_url = rows_url(args.dataset, args.config, args.split, args.row_index)
    response, response_body = fetch_json(query_url, timeout=args.timeout)
    rows = response.get("rows")
    if not isinstance(rows, list) or len(rows) != 1:
        raise HistoricalCaptureError("WebUI exact-row response must contain one row")
    wrapper = rows[0]
    if (
        not isinstance(wrapper, dict)
        or wrapper.get("row_idx") != args.row_index
        or not isinstance(wrapper.get("row"), dict)
    ):
        raise HistoricalCaptureError("WebUI exact-row response identity is invalid")
    if wrapper.get("truncated_cells") not in (None, []):
        raise HistoricalCaptureError("WebUI exact-row response contains truncated cells")
    row = wrapper["row"]
    if row.get("requested_url") != capture_url or row.get("displayed_url") != capture_url:
        raise HistoricalCaptureError(
            "WebUI requested/displayed URL differs from the selected capture URL"
        )
    if row.get("num_frames") != 1:
        raise HistoricalCaptureError("WebUI capture must contain one public top-level frame")
    source_image_ssim = row.get("source_image_ssim")
    if (
        not isinstance(source_image_ssim, (int, float))
        or not math.isfinite(source_image_ssim)
        or source_image_ssim < args.minimum_source_image_ssim
    ):
        raise HistoricalCaptureError(
            "WebUI DOM-to-reference image similarity is below the required minimum"
        )

    metadata = stable_webui_row_metadata(row)
    metadata_body = canonical_json_bytes(metadata)
    observed_metadata_hash = sha256_bytes(metadata_body)
    if observed_metadata_hash != row_metadata_sha256:
        raise HistoricalCaptureError(
            "WebUI stable row metadata hash differs: "
            f"expected {row_metadata_sha256}, observed {observed_metadata_hash}"
        )
    snapshot = row.get("dom_snapshot")
    snapshot_body = canonical_json_bytes(snapshot)
    observed_dom_hash = sha256_bytes(snapshot_body)
    if observed_dom_hash != dom_sha256:
        raise HistoricalCaptureError(
            f"WebUI DOM snapshot hash differs: expected {dom_sha256}, observed {observed_dom_hash}"
        )
    capture_options = row.get("capture_options")
    computed_style_names = (
        capture_options.get("computedStyles", [])
        if isinstance(capture_options, dict)
        else []
    )
    source_html, conversion = webui_dom_snapshot_html(
        snapshot,
        capture_url=capture_url,
        computed_style_names=computed_style_names,
    )
    if conversion["geometry_nodes"] < 20:
        raise HistoricalCaptureError("WebUI DOM snapshot has fewer than 20 design geometry nodes")
    sanitized, redactions = sanitize_html(source_html)

    image = row.get("image")
    if not isinstance(image, dict) or not isinstance(image.get("src"), str):
        raise HistoricalCaptureError("WebUI row lacks a screenshot URL")
    image_url = validate_webui_image_url(
        image["src"],
        dataset=args.dataset,
        revision=args.revision,
        config=args.config,
        split=args.split,
        row_index=args.row_index,
    )
    screenshot_body, screenshot_type = fetch_bytes(
        image_url,
        timeout=args.timeout,
        limit=MAX_SCREENSHOT_BYTES,
    )
    observed_screenshot_hash = sha256_bytes(screenshot_body)
    if observed_screenshot_hash != screenshot_sha256:
        raise HistoricalCaptureError(
            "WebUI screenshot hash differs: "
            f"expected {screenshot_sha256}, observed {observed_screenshot_hash}"
        )
    width, height, screenshot_suffix, screenshot_type = validate_screenshot_bytes(
        screenshot_body, screenshot_type
    )
    if width < 1024 or height < 600:
        raise HistoricalCaptureError("WebUI screenshot is not a desktop observation")
    if (image.get("width"), image.get("height")) != (width, height):
        raise HistoricalCaptureError("WebUI screenshot dimensions differ from row metadata")

    cache_root = root / "sites" / site / "static" / "external_cache" / "upstream"
    stem = f"webui-dom-{args.split}-{args.row_index}"
    page_path = cache_root / "historical" / "pages" / f"{stem}.html"
    screenshot_path = cache_root / "historical" / "references" / f"{stem}{screenshot_suffix}"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_bytes(sanitized)
    screenshot_path.write_bytes(screenshot_body)
    if screenshot_dimensions(screenshot_path) != (width, height):
        raise HistoricalCaptureError("stored screenshot dimensions changed after validation")

    parsed = urlsplit(upstream_url)
    page_record = {
        "bytes": len(sanitized),
        "content_type": "text/html; charset=utf-8",
        "final_url": capture_url,
        "historical_row_index": args.row_index,
        "local_path": relative_path(page_path, cache_root),
        "sha256": sha256_bytes(sanitized),
        "source_page_sha256": dom_sha256,
        "status": 200,
        "url": capture_url,
    }
    evidence = {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "phase": "historical_source",
        "site": site,
        "generated_at": utc_now(),
        "status": "passed",
        "source_mode": "historical_benchmark_snapshot",
        "authorization": {
            "basis": "user_directed_bounded_private_research_snapshot",
            "publication": "not_authorized",
            "scope": "one public dataset DOMSnapshot row and its published desktop screenshot",
        },
        "scope": {
            "upstream_url": upstream_url,
            "capture_url": capture_url,
            "start_urls": [capture_url],
            "page_hosts": [parsed.hostname],
            "asset_hosts": [],
            "generated_get_queries": [],
        },
        "source": {
            "provider": "webui_dom_snapshot",
            "kind": "hf_dataset_dom_snapshot_row",
            "dataset": args.dataset,
            "revision": args.revision,
            "config": args.config,
            "split": args.split,
            "row_index": args.row_index,
            "license": args.license,
            "capture_url": capture_url,
            "stable_row_metadata": {
                "bytes": len(metadata_body),
                "sha256": row_metadata_sha256,
                "value": metadata,
            },
            "dom_snapshot": {
                "bytes": len(snapshot_body),
                "sha256": dom_sha256,
                "stored": False,
                "reason": "only a sanitized HTML and bbox derivative is retained",
            },
            "screenshot": {
                "bytes": len(screenshot_body),
                "content_type": screenshot_type,
                "dataset_asset_path": unquote(urlsplit(image_url).path),
                "sha256": screenshot_sha256,
                "stored": True,
            },
            "dataset_response": {
                "bytes": len(response_body),
                "sha256": sha256_bytes(response_body),
                "url": query_url,
                "authoritative_identity": False,
                "reason": "the response embeds an expiring signed screenshot URL; stable field hashes are authoritative",
            },
            "dom_conversion": conversion,
            "sanitization": redactions,
        },
        "pages": [page_record],
        "assets": [],
        "screenshot": {
            "bytes": len(screenshot_body),
            "content_type": screenshot_type,
            "height": height,
            "local_path": relative_path(screenshot_path, root),
            "sha256": screenshot_sha256,
            "width": width,
        },
        "failures": [],
        "limitations": [
            "the source is a historical benchmark observation, not a current live-site capture",
            "DOM structure, classes, attributes, text, controls, semantics, and layout bounds are the primary design source",
            "the screenshot is retained only as a desktop calibration and bounded local-asset reference",
            "the full reference screenshot is never served by the mirror",
            "scripts, event handlers, input values, and sensitive attributes are not retained",
            "production-site drift is not measured by this evidence",
            "publication rights for retained derivatives and screenshot bytes are not asserted",
        ],
    }
    write_evidence(args, root, site, evidence)
    return evidence


def capture(args: argparse.Namespace) -> dict[str, Any]:
    provider = getattr(args, "provider", "mind2web")
    if provider == "mind2web":
        return capture_mind2web(args)
    if provider == "weblinx":
        return capture_weblinx(args)
    if provider == "webui_dom_snapshot":
        return capture_webui_dom_snapshot(args)
    raise HistoricalCaptureError(f"unsupported historical provider: {provider}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("mind2web", "weblinx", "webui_dom_snapshot"),
        default="mind2web",
    )
    parser.add_argument("--site", required=True)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--website")
    parser.add_argument("--split", choices=("train", "test_domain", "test_task", "test_website"))
    parser.add_argument("--dataset")
    parser.add_argument("--revision")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--row-index", type=int)
    parser.add_argument("--annotation-id")
    parser.add_argument("--demonstration")
    parser.add_argument("--page")
    parser.add_argument("--bbox")
    parser.add_argument("--screenshot")
    parser.add_argument("--html-sha256")
    parser.add_argument("--bbox-sha256")
    parser.add_argument("--screenshot-sha256")
    parser.add_argument("--capture-url")
    parser.add_argument("--dom-sha256")
    parser.add_argument("--row-metadata-sha256")
    parser.add_argument("--minimum-source-image-ssim", type=float, default=0.7)
    parser.add_argument("--license")
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.provider == "mind2web":
        args.dataset = args.dataset or DEFAULT_DATASET
        args.revision = args.revision or DEFAULT_REVISION
        if not args.website or not args.split:
            parser.error("Mind2Web capture requires --website and --split")
    elif args.provider == "weblinx":
        args.dataset = args.dataset or DEFAULT_WEBLINX_DATASET
        args.revision = args.revision or DEFAULT_WEBLINX_REVISION
        args.license = args.license or DEFAULT_WEBLINX_LICENSE
        required = {
            "demonstration": args.demonstration,
            "page": args.page,
            "bbox": args.bbox,
            "screenshot": args.screenshot,
            "html-sha256": args.html_sha256,
            "bbox-sha256": args.bbox_sha256,
            "screenshot-sha256": args.screenshot_sha256,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            parser.error("WebLINX capture requires " + ", ".join(f"--{name}" for name in missing))
    else:
        args.dataset = args.dataset or DEFAULT_WEBUI_DOM_DATASET
        args.revision = args.revision or DEFAULT_WEBUI_DOM_REVISION
        args.license = args.license or DEFAULT_WEBUI_DOM_LICENSE
        args.split = args.split or "train"
        required = {
            "capture-url": args.capture_url,
            "row-index": args.row_index,
            "dom-sha256": args.dom_sha256,
            "row-metadata-sha256": args.row_metadata_sha256,
            "screenshot-sha256": args.screenshot_sha256,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error(
                "WebUI DOM capture requires "
                + ", ".join(f"--{name}" for name in missing)
            )
        if not 0 <= args.minimum_source_image_ssim <= 1:
            parser.error("--minimum-source-image-ssim must be between zero and one")
    evidence = capture(args)
    identity = evidence["source"]
    print(json.dumps({
        "site": evidence["site"],
        "status": evidence["status"],
        "source_mode": evidence["source_mode"],
        "provider": identity.get("provider", "mind2web"),
        "dataset": identity["dataset"],
        "revision": identity["revision"],
        **({"row_index": identity["row_index"]} if "row_index" in identity else {}),
        **({"demonstration": identity["demonstration"]} if "demonstration" in identity else {}),
        "html_sha256": evidence["pages"][0]["sha256"],
        "screenshot_sha256": evidence["screenshot"]["sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HistoricalCaptureError as exc:
        print(f"historical capture error: {exc}", file=sys.stderr)
        raise SystemExit(2)
