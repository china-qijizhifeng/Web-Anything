#!/usr/bin/env python3
"""Probe one pinned Common Crawl index for exact-origin HTML records."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from probe_historical_indexes import exact_origin, load_targets, same_origin


TOOL_VERSION = "webharbor-common-crawl-probe-v1"
INDEX_ID = "CC-MAIN-2026-30"
INDEX_API = f"https://index.commoncrawl.org/{INDEX_ID}-index"
COLLECTIONS_URL = "https://index.commoncrawl.org/collinfo.json"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class CommonCrawlProbeError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def fetch_response(
    url: str, *, timeout: float, attempts: int
) -> tuple[int | None, bytes, dict[str, str], str | None]:
    last_status: int | None = None
    last_body = b""
    last_headers: dict[str, str] = {}
    last_error: str | None = None
    for attempt in range(attempts):
        request = Request(
            url,
            headers={"User-Agent": f"WebHarbor/{TOOL_VERSION}"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CommonCrawlProbeError(
                        f"response exceeds byte limit: {url}"
                    )
                return (
                    getattr(response, "status", response.getcode()),
                    body,
                    {
                        key.casefold(): value
                        for key, value in response.headers.items()
                    },
                    None,
                )
        except HTTPError as exc:
            last_status = exc.code
            last_body = exc.read(MAX_RESPONSE_BYTES + 1)
            last_headers = {
                key.casefold(): value for key, value in exc.headers.items()
            }
            last_error = str(exc)
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            time.sleep(min(2**attempt, 8))
    return last_status, last_body, last_headers, last_error


def collections_evidence(*, timeout: float, attempts: int) -> dict[str, Any]:
    status, body, _, error = fetch_response(
        COLLECTIONS_URL, timeout=timeout, attempts=attempts
    )
    evidence = {
        "url": COLLECTIONS_URL,
        "http_status": status,
        "bytes": len(body),
        "sha256": sha256_bytes(body),
    }
    if status != 200:
        return {**evidence, "status": "unavailable", "error": error}
    try:
        collections = json.loads(body)
    except json.JSONDecodeError as exc:
        return {**evidence, "status": "invalid", "error": str(exc)}
    if not isinstance(collections, list):
        return {**evidence, "status": "invalid", "error": "root is not a list"}
    collection = next(
        (
            item
            for item in collections
            if isinstance(item, dict) and item.get("id") == INDEX_ID
        ),
        None,
    )
    if collection is None or collection.get("cdx-api") != INDEX_API:
        return {
            **evidence,
            "status": "invalid",
            "error": f"pinned index {INDEX_ID} is absent or changed",
        }
    return {
        **evidence,
        "status": "passed",
        "index": INDEX_ID,
        "name": collection.get("name"),
        "from": collection.get("from"),
        "to": collection.get("to"),
        "cdx_api": collection.get("cdx-api"),
    }


def target_query_urls(upstream_url: str) -> list[str]:
    exact_origin(upstream_url)
    parsed = urlsplit(upstream_url)
    normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
    output = [normalized]
    if parsed.path and parsed.path != "/":
        alternate_path = parsed.path.rstrip("/") + "/"
        alternate = urlunsplit((parsed.scheme, parsed.netloc, alternate_path, "", ""))
        if alternate not in output:
            output.append(alternate)
    return output


def query_url(capture_url: str, *, row_limit: int) -> str:
    return INDEX_API + "?" + urlencode([
        ("url", capture_url),
        ("output", "json"),
        ("matchType", "exact"),
        ("filter", "status:200"),
        ("filter", "mime:text/html"),
        ("pageSize", str(row_limit)),
        ("page", "0"),
    ])


def parse_rows(body: bytes, target_url: str) -> tuple[list[dict[str, Any]], list[str]]:
    rows = []
    errors = []
    text = body.decode("utf-8", errors="replace").strip()
    if not text or text.casefold().startswith("no captures found for"):
        return rows, errors
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"line {line_number} is not JSON")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {line_number} is not an object")
            continue
        source_url = row.get("url")
        if not isinstance(source_url, str) or not same_origin(target_url, source_url):
            errors.append(f"line {line_number} is not the exact target origin")
            continue
        filename = row.get("filename")
        timestamp = row.get("timestamp")
        digest = row.get("digest")
        try:
            offset = int(row.get("offset"))
            length = int(row.get("length"))
        except (TypeError, ValueError):
            errors.append(f"line {line_number} lacks numeric WARC coordinates")
            continue
        if (
            not isinstance(filename, str)
            or not filename.startswith(f"crawl-data/{INDEX_ID}/")
            or not isinstance(timestamp, str)
            or not re.fullmatch(r"\d{14}", timestamp)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[A-Z2-7]{32}", digest)
            or offset < 0
            or length < 1
        ):
            errors.append(f"line {line_number} lacks pinned WARC identity")
            continue
        stable = {
            "url": source_url,
            "timestamp": timestamp,
            "filename": filename,
            "offset": offset,
            "length": length,
            "digest": digest,
            "status": row.get("status"),
            "mime": row.get("mime"),
        }
        rows.append({
            **stable,
            "row_sha256": sha256_bytes(
                json.dumps(stable, separators=(",", ":"), sort_keys=True).encode()
            ),
        })
    return rows, errors


def probe_query(
    site: str,
    target_url: str,
    capture_url: str,
    *,
    row_limit: int,
    timeout: float,
    attempts: int,
) -> dict[str, Any]:
    url = query_url(capture_url, row_limit=row_limit)
    status, body, headers, error = fetch_response(
        url, timeout=timeout, attempts=attempts
    )
    evidence = {
        "site": site,
        "capture_url": capture_url,
        "query_url": url,
        "query_sha256": sha256_bytes(url.encode()),
        "http_status": status,
        "response_bytes": len(body),
        "response_sha256": sha256_bytes(body),
        "retry_after": headers.get("retry-after"),
    }
    no_captures = body.decode("utf-8", errors="replace").casefold().startswith(
        "no captures found for"
    )
    if status not in (200, 404) or (status == 404 and not no_captures):
        return {
            **evidence,
            "status": "unavailable",
            "error": error or f"HTTP {status}",
            "records": [],
            "rejected_rows": [],
        }
    records, rejected_rows = parse_rows(body, target_url)
    return {
        **evidence,
        "status": "passed",
        "records": records,
        "rejected_rows": rejected_rows,
    }


def probe(args: argparse.Namespace) -> dict[str, Any]:
    targets = load_targets(args.queue, args.targets)
    collections = collections_evidence(
        timeout=args.timeout, attempts=args.attempts
    )
    jobs = []
    for target in targets:
        for capture_url in target_query_urls(target["upstream_url"]):
            jobs.append((target, capture_url))
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                probe_query,
                target["site"],
                target["upstream_url"],
                capture_url,
                row_limit=args.row_limit,
                timeout=args.timeout,
                attempts=args.attempts,
            ): (target["site"], capture_url)
            for target, capture_url in jobs
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: (item["site"], item["capture_url"]))
    entries = []
    for target in targets:
        queries = [item for item in results if item["site"] == target["site"]]
        records = [record for query in queries for record in query["records"]]
        unavailable = any(query["status"] == "unavailable" for query in queries)
        retry_conditions = []
        if unavailable:
            retry_conditions.append(
                f"retry {INDEX_ID} only after its CDX service stops returning transport errors"
            )
        if not records:
            retry_conditions.append(
                "retry promotion only when a fixed Common Crawl index returns an exact-origin HTML WARC coordinate"
            )
        else:
            retry_conditions.append(
                "fetch the pinned WARC record and require hash-verified HTML, CSS, fonts, images, and matched desktop visual evidence before promotion"
            )
        entries.append({
            **target,
            "status": "record_coordinates_found" if records else (
                "provider_unavailable" if unavailable else "no_exact_origin_record"
            ),
            "queries": queries,
            "records": records,
            "eligible_candidates": [],
            "retry_conditions": retry_conditions,
        })
    query_failures = sum(
        query["status"] == "unavailable" for query in results
    )
    records = sum(len(entry["records"]) for entry in entries)
    return {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "generated_at": utc_now(),
        "status": (
            "passed" if not query_failures and collections["status"] == "passed"
            else "completed_with_provider_errors"
        ),
        "selection": {
            "queue": str(args.queue),
            "targets": args.targets,
            "index": INDEX_ID,
            "match_type": "exact",
            "filters": ["status:200", "mime:text/html"],
            "exact_scheme_hostname_port_required": True,
            "row_limit": args.row_limit,
        },
        "provider": collections,
        "entries": entries,
        "summary": {
            "targets": len(targets),
            "queries": len(results),
            "query_failures": query_failures,
            "exact_origin_records": records,
            "targets_with_records": sum(bool(entry["records"]) for entry in entries),
            "eligible_candidates": 0,
        },
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
    value.add_argument("--row-limit", type=int, default=20)
    value.add_argument("--workers", type=int, default=6)
    value.add_argument("--timeout", type=float, default=20)
    value.add_argument("--attempts", type=int, default=2)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.row_limit < 1 or args.row_limit > 100:
        raise SystemExit("--row-limit must be between 1 and 100")
    if args.workers < 1 or args.workers > 12:
        raise SystemExit("--workers must be between 1 and 12")
    if args.attempts < 1 or args.attempts > 6:
        raise SystemExit("--attempts must be between 1 and 6")
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
    except CommonCrawlProbeError as exc:
        print(f"Common Crawl probe error: {exc}", file=sys.stderr)
        raise SystemExit(2)
