#!/usr/bin/env python3
"""Discover strong pinned historical HTML rows for replacement-queue sites."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from capture_historical import (
    DEFAULT_CONFIG,
    DEFAULT_DATASET,
    DEFAULT_REVISION,
    FILTER_API,
    HistoricalCaptureError,
    dataset_revision,
    fetch_bytes,
    rows_url,
    sanitize_html,
)
from compile_historical import HistoricalDomParser
from historical_geometry import (
    HistoricalGeometryArchive,
    HistoricalGeometryError,
    load_geometry_for_row,
)


TOOL_VERSION = "webharbor-historical-discovery-v1"
STATISTICS_API = "https://datasets-server.huggingface.co/statistics"
SPLITS = ("train", "test_domain", "test_task", "test_website")
COMMON_SUFFIXES = (".co.uk", ".com", ".org", ".gov", ".edu", ".net")


class HistoricalDiscoveryError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def statistics_url(dataset: str, config: str, split: str) -> str:
    return STATISTICS_API + "?" + urlencode({
        "dataset": dataset,
        "config": config,
        "split": split,
    })


def fetch_json_bounded(url: str, *, timeout: float) -> tuple[dict[str, Any], bytes]:
    # Dataset-server indexes are rebuilt lazily and may transiently return 5xx;
    # retain bounded retries so a recoverable service rebuild is not recorded as
    # a false "no usable row" result.
    body, _ = fetch_bytes(url, timeout=timeout, attempts=4)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HistoricalCaptureError(f"response is not valid JSON: {url}") from exc
    if not isinstance(value, dict):
        raise HistoricalCaptureError(f"JSON response must be an object: {url}")
    if value.get("error"):
        raise HistoricalCaptureError(f"dataset service error: {value['error']}")
    return value, body


def filtered_rows_url(
    dataset: str,
    config: str,
    split: str,
    website: str,
    limit: int,
) -> str:
    where = f'"website"=\'{website}\''
    return FILTER_API + "?" + urlencode({
        "dataset": dataset,
        "config": config,
        "split": split,
        "where": where,
        "offset": 0,
        "length": limit,
    })


def website_frequencies(value: dict[str, Any]) -> dict[str, int]:
    statistics = value.get("statistics")
    if not isinstance(statistics, list):
        raise HistoricalDiscoveryError("statistics response has no statistics list")
    for record in statistics:
        if not isinstance(record, dict) or record.get("column_name") != "website":
            continue
        frequencies = record.get("column_statistics", {}).get("frequencies")
        if not isinstance(frequencies, dict):
            break
        if not all(isinstance(key, str) and isinstance(count, int) for key, count in frequencies.items()):
            break
        return frequencies
    raise HistoricalDiscoveryError("statistics response has no website frequencies")


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def hostname_core(hostname: str) -> str:
    value = hostname.casefold().removeprefix("www.").rstrip(".")
    for suffix in COMMON_SUFFIXES:
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def website_match_score(entry: dict[str, Any], website: str) -> int:
    parsed = urlsplit(entry["upstream_url"])
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    label = website.casefold()
    if label == host:
        return 100
    core = hostname_core(host)
    if label == core:
        return 96
    label_compact = normalized(label)
    host_compact = normalized(host)
    core_compact = normalized(core)
    if len(label_compact) >= 4 and label_compact in host_compact:
        return 88
    if len(core_compact) >= 4 and core_compact in label_compact:
        return 86

    label_tokens = {
        token for token in re.split(r"[^a-z0-9]+", label) if len(token) >= 3
    }
    context = " ".join((
        entry.get("site", ""),
        entry.get("display_name", ""),
        host,
        parsed.path,
    )).casefold()
    context_tokens = {
        token for token in re.split(r"[^a-z0-9]+", context) if len(token) >= 3
    }
    if label_tokens and label_tokens <= context_tokens:
        return 78
    return 0


def choose_website(
    entry: dict[str, Any],
    coverage: dict[str, list[str]],
    *,
    minimum_score: int,
) -> tuple[str, list[str], int] | None:
    ranked = sorted(
        (
            (website_match_score(entry, website), website, splits)
            for website, splits in coverage.items()
        ),
        reverse=True,
    )
    if not ranked or ranked[0][0] < minimum_score:
        return None
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][0] == best[0]:
        return None
    return best[1], best[2], best[0]


def row_metrics(wrapper: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("row"), dict):
        return None
    index = wrapper.get("row_idx")
    row = wrapper["row"]
    if not isinstance(index, int) or str(row.get("target_action_index")) != "0":
        return None
    source = row.get("raw_html")
    screenshot = row.get("screenshot")
    if not isinstance(source, str) or not source.strip() or not isinstance(screenshot, dict):
        return None
    if not isinstance(screenshot.get("src"), str):
        return None
    try:
        width = int(screenshot.get("width") or 0)
        height = int(screenshot.get("height") or 0)
        sanitized, redactions = sanitize_html(source)
        parser = HistoricalDomParser()
        parser.feed(sanitized.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:
        return None
    nodes = parser.root.descendants()
    bbox_elements = sum(node.rect is not None for node in nodes)
    text_elements = sum(
        1 for node in nodes if node.rect is not None and node.direct_text().strip()
    )
    visual_elements = sum(
        1
        for node in nodes
        if node.rect is not None
        and (
            node.tag in {"img", "picture", "svg", "video", "canvas"}
            or node.attrs.get("aria_role", "").casefold() in {"img", "image"}
        )
    )
    return {
        "row_index": index,
        "annotation_id": row.get("annotation_id"),
        "action_uid": row.get("action_uid"),
        "confirmed_task": row.get("confirmed_task"),
        "bbox_elements": bbox_elements,
        "text_elements": text_elements,
        "visual_elements": visual_elements,
        "html_bytes": len(sanitized),
        "screenshot_width": width,
        "screenshot_height": height,
        "sanitization": redactions,
    }


def candidate_rank(value: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        value["bbox_elements"],
        value["visual_elements"],
        value["text_elements"],
        value["html_bytes"],
    )


def load_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalDiscoveryError(f"invalid overrides file: {exc}") from exc
    if not isinstance(value, dict):
        raise HistoricalDiscoveryError("historical overrides must be a JSON object")
    for site, override in value.items():
        if not isinstance(site, str) or not isinstance(override, dict):
            raise HistoricalDiscoveryError("historical overrides must map site ids to objects")
        if not isinstance(override.get("website"), str):
            raise HistoricalDiscoveryError(f"{site}: override requires website")
        if "split" in override and override["split"] not in SPLITS:
            raise HistoricalDiscoveryError(f"{site}: unsupported override split")
    return value


def discover(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    revision = dataset_revision(args.dataset)
    if revision != args.revision:
        raise HistoricalDiscoveryError(
            f"dataset revision changed: expected {args.revision}, resolved {revision}"
        )
    queue = json.loads(args.queue.read_text(encoding="utf-8"))
    entries = queue.get("entries")
    if not isinstance(entries, list):
        raise HistoricalDiscoveryError("replacement queue has no entries list")
    requested = set(args.sites)
    unknown = sorted(requested - {entry.get("site") for entry in entries})
    if unknown:
        raise HistoricalDiscoveryError("unknown queue sites: " + ", ".join(unknown))
    selected = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and (not requested or entry.get("site") in requested)
        and (args.include_passed or entry.get("status") != "passed")
    ]
    overrides = load_overrides(args.overrides)

    coverage: dict[str, list[str]] = {}
    statistics_evidence = []
    for split in SPLITS:
        url = statistics_url(args.dataset, args.config, split)
        response, body = fetch_json_bounded(url, timeout=args.timeout)
        frequencies = website_frequencies(response)
        statistics_evidence.append({
            "split": split,
            "url": url,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "websites": len(frequencies),
        })
        for website in frequencies:
            coverage.setdefault(website, []).append(split)

    report_entries = []
    spec_entries = []
    geometry_archive: HistoricalGeometryArchive | None = None
    for entry_number, entry in enumerate(selected, 1):
        site = entry["site"]
        override = overrides.get(site)
        if override:
            website = override["website"]
            splits = [override["split"]] if override.get("split") else coverage.get(website, [])
            match_score = 100
            match_mode = "explicit_override"
        else:
            match = choose_website(
                entry, coverage, minimum_score=args.minimum_match_score
            )
            if match is None:
                report_entries.append({
                    "site": site,
                    "status": "unmatched",
                    "upstream_url": entry["upstream_url"],
                })
                continue
            website, splits, match_score = match
            match_mode = "upstream_and_identity_match"

        print(
            f"[discover-historical {entry_number}/{len(selected)}] "
            f"{site} -> {website} ({','.join(splits)})",
            file=sys.stderr,
            flush=True,
        )

        row_candidates = []
        queries = []
        for split in splits:
            url = filtered_rows_url(
                args.dataset, args.config, split, website, args.row_limit
            )
            try:
                response, body = fetch_json_bounded(url, timeout=args.timeout)
            except HistoricalCaptureError as exc:
                queries.append({
                    "split": split,
                    "url": url,
                    "status": "failed",
                    "error": str(exc),
                })
                continue
            wrappers = response.get("rows")
            if not isinstance(wrappers, list):
                wrappers = []
            queries.append({
                "split": split,
                "url": url,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "rows": len(wrappers),
                "status": "passed",
            })
            for wrapper in wrappers:
                metrics = row_metrics(wrapper)
                if metrics is not None and metrics["bbox_elements"] >= args.minimum_bbox:
                    metrics["split"] = split
                    row_candidates.append(metrics)
                    continue
                if not isinstance(wrapper, dict) or not isinstance(wrapper.get("row_idx"), int):
                    continue
                wrapper_row = wrapper.get("row")
                if (
                    not isinstance(wrapper_row, dict)
                    or str(wrapper_row.get("target_action_index")) != "0"
                ):
                    continue

                # The filter endpoint may elide the screenshot object, and some
                # official rows carry geometry in a separately pinned bbox dataset.
                # Resolve only exact initial-action rows and require byte-identical
                # HTML before merging that node metadata.
                exact_url = rows_url(
                    args.dataset,
                    args.config,
                    split,
                    wrapper["row_idx"],
                )
                query_record: dict[str, Any] = {
                    "split": split,
                    "row_index": wrapper["row_idx"],
                    "url": exact_url,
                }
                try:
                    exact_response, exact_body = fetch_json_bounded(
                        exact_url, timeout=args.timeout
                    )
                except HistoricalCaptureError as exc:
                    query_record.update({"status": "failed", "error": str(exc)})
                    queries.append(query_record)
                    continue
                exact_rows = exact_response.get("rows")
                exact_wrapper = (
                    exact_rows[0]
                    if isinstance(exact_rows, list) and exact_rows
                    else None
                )
                exact_metrics = row_metrics(exact_wrapper)
                geometry_evidence = None
                geometry_error = None
                if (
                    isinstance(exact_wrapper, dict)
                    and isinstance(exact_wrapper.get("row"), dict)
                    and (
                        exact_metrics is None
                        or exact_metrics["bbox_elements"] < args.minimum_bbox
                    )
                ):
                    try:
                        if geometry_archive is None:
                            geometry_archive = HistoricalGeometryArchive(
                                cache_root=repository_root() / ".assets" / "historical-geometry",
                                timeout=args.timeout,
                            )
                        geometry, geometry_evidence = load_geometry_for_row(
                            exact_wrapper["row"],
                            split=split,
                            archive=geometry_archive,
                        )
                        enriched_html, _ = sanitize_html(
                            exact_wrapper["row"]["raw_html"], geometry=geometry
                        )
                        enriched_wrapper = {
                            **exact_wrapper,
                            "row": {
                                **exact_wrapper["row"],
                                "raw_html": enriched_html.decode("utf-8"),
                            },
                        }
                        exact_metrics = row_metrics(enriched_wrapper)
                    except (HistoricalGeometryError, HistoricalCaptureError) as exc:
                        geometry_error = str(exc)
                query_record.update({
                    "bytes": len(exact_body),
                    "sha256": hashlib.sha256(exact_body).hexdigest(),
                    "status": "passed" if exact_metrics is not None else "no_usable_row",
                    **({"geometry_source": geometry_evidence} if geometry_evidence else {}),
                    **({"geometry_error": geometry_error} if geometry_error else {}),
                })
                queries.append(query_record)
                if exact_metrics is not None:
                    exact_metrics["split"] = split
                    if geometry_evidence:
                        exact_metrics["geometry_source"] = geometry_evidence
                    row_candidates.append(exact_metrics)
        usable = [
            row for row in row_candidates
            if row["bbox_elements"] >= args.minimum_bbox
            and row["screenshot_width"] >= args.minimum_width
            and row["screenshot_height"] >= args.minimum_height
        ]
        usable.sort(key=candidate_rank, reverse=True)
        if not usable:
            report_entries.append({
                "site": site,
                "status": "no_usable_row",
                "upstream_url": entry["upstream_url"],
                "website": website,
                "match_score": match_score,
                "match_mode": match_mode,
                "queries": queries,
                "initial_rows": len(row_candidates),
                "minimum_bbox": args.minimum_bbox,
            })
            continue
        chosen = usable[0]
        report_entries.append({
            "site": site,
            "status": "candidate",
            "upstream_url": entry["upstream_url"],
            "website": website,
            "match_score": match_score,
            "match_mode": match_mode,
            "queries": queries,
            "chosen": chosen,
            "alternatives": usable[1:args.alternatives + 1],
        })
        spec_entries.append({
            "site": site,
            "upstream_url": entry["upstream_url"],
            "website": website,
            "split": chosen["split"],
            "row_index": chosen["row_index"],
            "minimum_pixel_similarity": args.minimum_pixel_similarity,
        })

    report = {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "generated_at": utc_now(),
        "status": "passed",
        "dataset": args.dataset,
        "revision": args.revision,
        "config": args.config,
        "selection": {
            "queue": str(args.queue),
            "sites": sorted(requested),
            "include_passed": args.include_passed,
            "minimum_match_score": args.minimum_match_score,
            "minimum_bbox": args.minimum_bbox,
            "minimum_width": args.minimum_width,
            "minimum_height": args.minimum_height,
        },
        "statistics": statistics_evidence,
        "summary": {
            "selected": len(selected),
            "candidates": len(spec_entries),
            "unmatched": sum(row["status"] == "unmatched" for row in report_entries),
            "no_usable_row": sum(row["status"] == "no_usable_row" for row in report_entries),
        },
        "entries": report_entries,
    }
    spec = {
        "schema_version": 1,
        "dataset": args.dataset,
        "revision": args.revision,
        "config": args.config,
        "entries": spec_entries,
    }
    return report, spec


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("sites", nargs="*")
    value.add_argument(
        "--queue",
        type=Path,
        default=Path("mirror_factory/evidence/generated-replacement/queue.json"),
    )
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--spec-output", type=Path, required=True)
    value.add_argument("--overrides", type=Path)
    value.add_argument("--dataset", default=DEFAULT_DATASET)
    value.add_argument("--revision", default=DEFAULT_REVISION)
    value.add_argument("--config", default=DEFAULT_CONFIG)
    value.add_argument("--include-passed", action="store_true")
    value.add_argument("--minimum-match-score", type=int, default=78)
    value.add_argument("--minimum-bbox", type=int, default=100)
    value.add_argument("--minimum-width", type=int, default=1024)
    value.add_argument("--minimum-height", type=int, default=600)
    value.add_argument("--minimum-pixel-similarity", type=float, default=0.85)
    value.add_argument("--row-limit", type=int, default=20)
    value.add_argument("--alternatives", type=int, default=2)
    value.add_argument("--timeout", type=float, default=30)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.row_limit < 1 or args.row_limit > 100:
        raise SystemExit("--row-limit must be between 1 and 100")
    if args.minimum_bbox < 20:
        raise SystemExit("--minimum-bbox must be at least 20")
    if not 0 <= args.minimum_pixel_similarity <= 1:
        raise SystemExit("--minimum-pixel-similarity must be between 0 and 1")
    report, spec = discover(args)
    atomic_json(args.output, report)
    atomic_json(args.spec_output, spec)
    print(json.dumps({
        "status": report["status"],
        "summary": report["summary"],
        "report": str(args.output),
        "spec": str(args.spec_output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HistoricalDiscoveryError as exc:
        print(f"historical discovery error: {exc}", file=sys.stderr)
        raise SystemExit(2)
