#!/usr/bin/env python3
"""Probe pinned public dataset indexes for exact-origin HTML-first snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from page_content_policy import content_error_reason


TOOL_VERSION = "webharbor-historical-index-probe-v1"
DATASET_API = "https://huggingface.co/api/datasets/{dataset}"
FILTER_API = "https://datasets-server.huggingface.co/filter"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
PROVIDERS = {
    "webui_dom_snapshot": {
        "dataset": "gbenson/webui-dom-snapshots",
        "revision": "3dc9dea26bb353d59d0653a2a233ff80bf4e3d8b",
        "config": "default",
        "splits": ("train",),
        "url_field": "requested_url",
    },
    "webui_source_bundle": {
        "dataset": "ronantakizawa/webui",
        "revision": "71982976513a2d0faa88930dc57169b0d59b878b",
        "config": "default",
        "splits": ("train", "validation", "test"),
        "url_field": "source_url",
    },
}


class HistoricalIndexProbeError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def exact_origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HistoricalIndexProbeError(
            "targets and indexed source URLs must be public HTTPS URLs without credentials"
        )
    return parsed.scheme.casefold(), parsed.hostname.casefold(), parsed.port


def same_origin(left: str, right: str) -> bool:
    try:
        return exact_origin(left) == exact_origin(right)
    except (HistoricalIndexProbeError, ValueError):
        return False


def fetch_bytes(
    url: str,
    *,
    timeout: float,
    attempts: int,
    limit: int = MAX_RESPONSE_BYTES,
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
                    raise HistoricalIndexProbeError(
                        f"response exceeds byte limit: {url}"
                    )
                return body, response.headers.get_content_type()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            folded = str(exc).casefold()
            if "name resolution" in folded or "temporary failure in name" in folded:
                return fetch_bytes_with_curl(url, timeout=timeout, limit=limit)
        if attempt + 1 < attempts:
            time.sleep(min(2**attempt, 8))
    raise HistoricalIndexProbeError(
        f"request failed after {attempts} attempts: {url}: {last_error}"
    )


def fetch_bytes_with_curl(
    url: str,
    *,
    timeout: float,
    limit: int,
) -> tuple[bytes, str]:
    exact_origin(url)
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--connect-timeout",
        str(max(1, min(timeout, 30))),
        "--max-time",
        str(max(1, timeout)),
        "--max-filesize",
        str(limit),
        "--user-agent",
        f"WebHarbor/{TOOL_VERSION}",
        url,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=timeout + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HistoricalIndexProbeError(f"curl transport failed: {url}: {exc}") from exc
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise HistoricalIndexProbeError(
            f"curl transport returned {result.returncode}: {url}: {error}"
        )
    if len(result.stdout) > limit:
        raise HistoricalIndexProbeError(f"response exceeds byte limit: {url}")
    return result.stdout, "application/octet-stream"


def fetch_json(
    url: str, *, timeout: float, attempts: int
) -> tuple[dict[str, Any], bytes]:
    last_error: str | None = None
    for attempt in range(attempts):
        try:
            body, _ = fetch_bytes(url, timeout=timeout, attempts=1)
        except HistoricalIndexProbeError as exc:
            last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 8))
                continue
            break
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HistoricalIndexProbeError(
                f"response is not valid JSON: {url}"
            ) from exc
        if not isinstance(value, dict):
            raise HistoricalIndexProbeError(f"JSON response must be an object: {url}")
        error = value.get("error")
        if not error:
            return value, body
        last_error = str(error)
        if "index is loading" not in last_error.casefold() or attempt + 1 >= attempts:
            break
        time.sleep(min(2**attempt, 8))
    raise HistoricalIndexProbeError(f"dataset service error: {last_error}")


def dataset_revision(dataset: str, *, timeout: float, attempts: int) -> str:
    url = DATASET_API.format(dataset=quote(dataset, safe="/"))
    value, _ = fetch_json(url, timeout=timeout, attempts=attempts)
    revision = value.get("sha")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise HistoricalIndexProbeError(
            f"dataset API did not return a full revision: {dataset}"
        )
    return revision


def filter_url(provider: dict[str, Any], split: str, hostname: str, limit: int) -> str:
    safe_hostname = hostname.replace("'", "''")
    where = f'"{provider["url_field"]}" LIKE \'%{safe_hostname}%\''
    return FILTER_API + "?" + urlencode({
        "dataset": provider["dataset"],
        "config": provider["config"],
        "split": split,
        "where": where,
        "offset": 0,
        "length": limit,
    })


def image_dimensions(value: Any) -> tuple[int, int]:
    if not isinstance(value, dict):
        return 0, 0
    try:
        return int(value.get("width") or 0), int(value.get("height") or 0)
    except (TypeError, ValueError):
        return 0, 0


def image_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("src"), str):
        return None
    width, height = image_dimensions(value)
    if width < 1 or height < 1:
        return None
    return {
        key: item
        for key, item in value.items()
        if key != "src" and isinstance(item, (str, int, float, bool, type(None)))
    }


def snapshot_string(strings: list[Any], index: Any) -> str | None:
    if not isinstance(index, int) or not 0 <= index < len(strings):
        return None
    value = strings[index]
    return value if isinstance(value, str) else None


def dom_snapshot_metrics(
    snapshot: Any,
    *,
    capture_options: Any,
    displayed_url: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(snapshot, dict):
        return None, "DOMSnapshot payload is absent"
    strings = snapshot.get("strings")
    documents = snapshot.get("documents")
    if (
        not isinstance(strings, list)
        or not isinstance(documents, list)
        or len(documents) != 1
        or not isinstance(documents[0], dict)
    ):
        return None, "DOMSnapshot lacks one document and a string table"
    document = documents[0]
    if snapshot_string(strings, document.get("documentURL")) != displayed_url:
        return None, "DOMSnapshot document URL differs from displayed_url"
    nodes = document.get("nodes")
    layout = document.get("layout")
    text_boxes = document.get("textBoxes")
    if not all(isinstance(value, dict) for value in (nodes, layout, text_boxes)):
        return None, "DOMSnapshot lacks nodes, layout, or text box geometry"
    node_arrays = [
        nodes.get("parentIndex"),
        nodes.get("nodeType"),
        nodes.get("nodeName"),
        nodes.get("nodeValue"),
        nodes.get("backendNodeId"),
        nodes.get("attributes"),
    ]
    if not all(isinstance(value, list) for value in node_arrays):
        return None, "DOMSnapshot node arrays are absent"
    node_count = len(node_arrays[0])
    if node_count < 20 or any(len(value) != node_count for value in node_arrays[1:]):
        return None, "DOMSnapshot has fewer than 20 consistent nodes"
    layout_indexes = layout.get("nodeIndex")
    layout_bounds = layout.get("bounds")
    layout_styles = layout.get("styles")
    layout_text = layout.get("text")
    layout_arrays = (layout_indexes, layout_bounds, layout_styles, layout_text)
    if (
        not all(isinstance(value, list) for value in layout_arrays)
        or len({len(value) for value in layout_arrays}) != 1
    ):
        return None, "DOMSnapshot layout arrays are inconsistent"
    geometry_nodes = 0
    for node_index, bounds in zip(layout_indexes, layout_bounds):
        if (
            not isinstance(node_index, int)
            or not 0 <= node_index < node_count
            or not isinstance(bounds, list)
            or len(bounds) != 4
            or not all(
                isinstance(item, (int, float)) and math.isfinite(item)
                for item in bounds
            )
        ):
            return None, "DOMSnapshot contains invalid layout geometry"
        if bounds[2] > 0 and bounds[3] > 0:
            geometry_nodes += 1
    if geometry_nodes < 20:
        return None, "DOMSnapshot has fewer than 20 bounded layout nodes"
    text_box_arrays = (
        text_boxes.get("layoutIndex"),
        text_boxes.get("bounds"),
        text_boxes.get("start"),
        text_boxes.get("length"),
    )
    if (
        not all(isinstance(value, list) for value in text_box_arrays)
        or len({len(value) for value in text_box_arrays}) != 1
        or not text_box_arrays[0]
    ):
        return None, "DOMSnapshot lacks consistent text box geometry"
    if not isinstance(capture_options, dict):
        return None, "DOMSnapshot capture options are absent"
    computed_styles = capture_options.get("computedStyles")
    if not isinstance(computed_styles, list) or not computed_styles or not all(
        isinstance(value, str) and value for value in computed_styles
    ):
        return None, "DOMSnapshot computed style names are absent"
    styled_nodes = 0
    for styles in layout_styles:
        if styles == []:
            continue
        if not isinstance(styles, list) or len(styles) != len(computed_styles):
            return None, "DOMSnapshot computed style values are inconsistent"
        styled_nodes += 1
    if styled_nodes < 1:
        return None, "DOMSnapshot has no bounded computed style values"
    marker = content_error_reason("\n".join(
        value for value in strings if isinstance(value, str)
    ))
    if marker:
        return None, f"DOMSnapshot is an error or challenge page: {marker}"
    return {
        "nodes": node_count,
        "geometry_nodes": geometry_nodes,
        "text_boxes": len(text_box_arrays[0]),
        "computed_style_names": computed_styles,
        "computed_style_nodes": styled_nodes,
    }, None


def summarize_webui_dom(
    wrapper: dict[str, Any], target_url: str
) -> tuple[dict[str, Any] | None, str | None]:
    row = wrapper.get("row")
    if not isinstance(row, dict):
        return None, "row payload is absent"
    requested_url = row.get("requested_url")
    displayed_url = row.get("displayed_url")
    if not isinstance(requested_url, str) or not same_origin(target_url, requested_url):
        return None, "requested_url is not the exact target origin"
    if not isinstance(displayed_url, str) or not same_origin(target_url, displayed_url):
        return None, "displayed_url is not the exact target origin"
    if row.get("num_frames") != 1:
        return None, "snapshot is not one top-level frame"
    snapshot = row.get("dom_snapshot")
    width, height = image_dimensions(row.get("image"))
    if width < 1024 or height < 600:
        return None, "snapshot is not a desktop viewport"
    stable_image = image_metadata(row.get("image"))
    if stable_image is None:
        return None, "same-row screenshot metadata is absent"
    source_image_ssim = row.get("source_image_ssim")
    if (
        not isinstance(source_image_ssim, (int, float))
        or not math.isfinite(source_image_ssim)
        or source_image_ssim < 0.7
    ):
        return None, "DOM-to-reference image SSIM is below 0.7"
    capture_options = row.get("capture_options")
    metrics, reason = dom_snapshot_metrics(
        snapshot,
        capture_options=capture_options,
        displayed_url=displayed_url,
    )
    if metrics is None:
        return None, reason
    stable_metadata = {
        "requested_url": requested_url,
        "displayed_url": displayed_url,
        "num_frames": row.get("num_frames"),
        "source_index": row.get("source_index"),
        "source_key_name": row.get("source_key_name"),
        "source_image_ssim": source_image_ssim,
        "capture_options": capture_options,
        "image": stable_image,
    }
    return {
        "row_index": wrapper.get("row_idx"),
        "requested_url": requested_url,
        "displayed_url": displayed_url,
        "source_index": row.get("source_index"),
        "source_key_name": row.get("source_key_name"),
        "source_image_ssim": source_image_ssim,
        "viewport": {"width": width, "height": height},
        "dom_snapshot_sha256": sha256_bytes(canonical_json_bytes(snapshot)),
        "row_metadata_sha256": sha256_bytes(canonical_json_bytes(stable_metadata)),
        "capture_options_sha256": sha256_bytes(
            canonical_json_bytes(capture_options)
        ),
        "image_metadata_sha256": sha256_bytes(canonical_json_bytes(stable_image)),
        "dom": metrics,
    }, None


def summarize_webui_source(
    wrapper: dict[str, Any], target_url: str
) -> tuple[dict[str, Any] | None, str | None]:
    row = wrapper.get("row")
    if not isinstance(row, dict):
        return None, "row payload is absent"
    source_url = row.get("source_url")
    if not isinstance(source_url, str) or not same_origin(target_url, source_url):
        return None, "source_url is not the exact target origin"
    if str(row.get("viewport", "")).casefold() != "desktop":
        return None, "row is not the desktop variant"
    html = row.get("html")
    css = row.get("css")
    javascript = row.get("js")
    bboxes = row.get("bboxes")
    if not isinstance(html, str) or len(html.encode("utf-8")) < 500:
        return None, "HTML source is absent or too small"
    has_design_code = (
        isinstance(css, str) and bool(css.strip())
    ) or "<style" in html.casefold() or "style=" in html.casefold()
    if not has_design_code:
        return None, "captured CSS or inline design code is absent"
    if not isinstance(bboxes, list) or len(bboxes) < 20:
        return None, "DOM geometry has fewer than 20 elements"
    width, height = image_dimensions(row.get("image"))
    if width < 1024 or height < 600:
        return None, "snapshot is not a desktop viewport"
    stable_image = image_metadata(row.get("image"))
    if stable_image is None:
        return None, "same-row screenshot metadata is absent"
    marker = content_error_reason(html)
    if marker:
        return None, f"HTML is an error or challenge page: {marker}"
    material_hashes = {
        "html": sha256_bytes(html.encode("utf-8")),
        "css": sha256_bytes(css.encode("utf-8")) if isinstance(css, str) else None,
        "js": (
            sha256_bytes(javascript.encode("utf-8"))
            if isinstance(javascript, str)
            else None
        ),
        "bboxes": sha256_bytes(canonical_json_bytes(bboxes)),
        "image_metadata": sha256_bytes(canonical_json_bytes(stable_image)),
    }
    return {
        "row_index": wrapper.get("row_idx"),
        "sample_id": row.get("sample_id"),
        "source_name": row.get("source_name"),
        "source_url": source_url,
        "viewport": {"width": width, "height": height},
        "elements": len(bboxes),
        "html_bytes": len(html.encode("utf-8")),
        "html_sha256": material_hashes["html"],
        "css_bytes": len(css.encode("utf-8")) if isinstance(css, str) else 0,
        "css_sha256": material_hashes["css"],
        "js_bytes": len(javascript.encode("utf-8")) if isinstance(javascript, str) else 0,
        "js_sha256": material_hashes["js"],
        "bboxes_sha256": material_hashes["bboxes"],
        "image_metadata_sha256": material_hashes["image_metadata"],
        "source_bundle_sha256": sha256_bytes(canonical_json_bytes(material_hashes)),
    }, None


def target_from_url(value: str) -> dict[str, str]:
    _, hostname, _ = exact_origin(value)
    site = re.sub(r"[^a-z0-9]+", "_", hostname.removeprefix("www.")).strip("_")
    return {"site": site, "upstream_url": value}


def load_targets(queue_path: Path, requested: list[str]) -> list[dict[str, str]]:
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    entries = queue.get("entries")
    if not isinstance(entries, list):
        raise HistoricalIndexProbeError("replacement queue has no entries list")
    by_site = {
        entry.get("site"): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("site"), str)
    }
    if not requested:
        selected = [entry for entry in entries if entry.get("status") == "rejected"]
    else:
        selected = []
        for value in requested:
            if value.startswith("https://"):
                selected.append(target_from_url(value))
            elif value in by_site:
                selected.append(by_site[value])
            else:
                raise HistoricalIndexProbeError(f"unknown queue site or URL: {value}")
    output = []
    seen = set()
    for entry in selected:
        site = entry.get("site")
        upstream_url = entry.get("upstream_url")
        if not isinstance(site, str) or not isinstance(upstream_url, str):
            raise HistoricalIndexProbeError("target requires site and upstream_url strings")
        exact_origin(upstream_url)
        if site in seen:
            continue
        seen.add(site)
        output.append({"site": site, "upstream_url": upstream_url})
    return output


def probe(args: argparse.Namespace) -> dict[str, Any]:
    targets = load_targets(args.queue, args.targets)
    provider_names = args.providers or list(PROVIDERS)
    unknown = sorted(set(provider_names) - set(PROVIDERS))
    if unknown:
        raise HistoricalIndexProbeError("unknown providers: " + ", ".join(unknown))

    provider_evidence = []
    resolved = {}
    provider_failures = {}
    for name in provider_names:
        provider = PROVIDERS[name]
        try:
            revision = dataset_revision(
                provider["dataset"], timeout=args.timeout, attempts=args.attempts
            )
        except HistoricalIndexProbeError as exc:
            provider_failures[name] = str(exc)
            provider_evidence.append({
                "provider": name,
                "dataset": provider["dataset"],
                "expected_revision": provider["revision"],
                "status": "unavailable",
                "error": str(exc),
            })
            continue
        if revision != provider["revision"]:
            error = (
                f"revision changed: expected {provider['revision']}, "
                f"resolved {revision}"
            )
            provider_failures[name] = error
            provider_evidence.append({
                "provider": name,
                "dataset": provider["dataset"],
                "expected_revision": provider["revision"],
                "resolved_revision": revision,
                "status": "revision_mismatch",
                "error": error,
            })
            continue
        resolved[name] = provider
        provider_evidence.append({
            "provider": name,
            "dataset": provider["dataset"],
            "revision": revision,
            "status": "passed",
            "config": provider["config"],
            "splits": list(provider["splits"]),
            "url_field": provider["url_field"],
        })

    entries = []
    for target in targets:
        _, hostname, _ = exact_origin(target["upstream_url"])
        candidates = []
        queries = [
            {
                "provider": name,
                "status": "unavailable",
                "stage": "revision_check",
                "error": error,
            }
            for name, error in provider_failures.items()
        ]
        rejected_rows = []
        for name, provider in resolved.items():
            summarizer = (
                summarize_webui_dom
                if name == "webui_dom_snapshot"
                else summarize_webui_source
            )
            for split in provider["splits"]:
                url = filter_url(provider, split, hostname, args.row_limit)
                try:
                    response, body = fetch_json(
                        url, timeout=args.timeout, attempts=args.attempts
                    )
                except HistoricalIndexProbeError as exc:
                    queries.append({
                        "provider": name,
                        "split": split,
                        "url": url,
                        "status": "unavailable",
                        "error": str(exc),
                    })
                    continue
                rows = response.get("rows")
                if not isinstance(rows, list):
                    rows = []
                queries.append({
                    "provider": name,
                    "split": split,
                    "url": url,
                    "status": "passed",
                    "bytes": len(body),
                    "sha256": sha256_bytes(body),
                    "rows": len(rows),
                })
                for wrapper in rows:
                    if not isinstance(wrapper, dict):
                        continue
                    candidate, reason = summarizer(wrapper, target["upstream_url"])
                    if candidate is not None:
                        candidates.append({
                            "provider": name,
                            "dataset": provider["dataset"],
                            "revision": provider["revision"],
                            "config": provider["config"],
                            "split": split,
                            **candidate,
                        })
                    elif len(rejected_rows) < args.rejected_row_limit:
                        rejected_rows.append({
                            "provider": name,
                            "split": split,
                            "row_index": wrapper.get("row_idx"),
                            "reason": reason,
                        })
        unavailable = [
            query["provider"]
            for query in queries
            if query["status"] == "unavailable"
        ]
        retry_conditions = []
        if unavailable:
            retry_conditions.append(
                "retry unavailable providers only after their pinned dataset revision "
                "and query service are available"
            )
        if not candidates:
            retry_conditions.append(
                "retry promotion only when a new pinned exact-origin desktop row provides "
                "HTML/DOM, design code, geometry, and a matched image"
            )
        entries.append({
            **target,
            "status": "candidate" if candidates else "no_exact_origin_candidate",
            "candidates": candidates,
            "queries": queries,
            "rejected_rows": rejected_rows,
            "retry_conditions": retry_conditions,
        })

    provider_errors = len(provider_failures)
    return {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "generated_at": utc_now(),
        "status": "passed" if not provider_errors else "completed_with_provider_errors",
        "selection": {
            "queue": str(args.queue),
            "targets": args.targets,
            "exact_origin_required": True,
            "html_first": True,
        },
        "providers": provider_evidence,
        "summary": {
            "targets": len(entries),
            "candidates": sum(bool(entry["candidates"]) for entry in entries),
            "without_candidate": sum(not entry["candidates"] for entry in entries),
            "provider_errors": provider_errors,
        },
        "entries": entries,
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("targets", nargs="*", help="queue site IDs or public HTTPS URLs")
    value.add_argument(
        "--queue",
        type=Path,
        default=Path("mirror_factory/evidence/generated-replacement/queue.json"),
    )
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--providers", nargs="*", choices=sorted(PROVIDERS))
    value.add_argument("--row-limit", type=int, default=20)
    value.add_argument("--rejected-row-limit", type=int, default=10)
    value.add_argument("--timeout", type=float, default=45)
    value.add_argument("--attempts", type=int, default=6)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.row_limit < 1 or args.row_limit > 100:
        raise SystemExit("--row-limit must be between 1 and 100")
    if args.rejected_row_limit < 0 or args.rejected_row_limit > 100:
        raise SystemExit("--rejected-row-limit must be between 0 and 100")
    if args.attempts < 1 or args.attempts > 10:
        raise SystemExit("--attempts must be between 1 and 10")
    report = probe(args)
    atomic_json(args.output, report)
    print(json.dumps({
        "status": report["status"],
        "summary": report["summary"],
        "output": str(args.output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HistoricalIndexProbeError as exc:
        print(f"historical index probe error: {exc}", file=sys.stderr)
        raise SystemExit(2)
