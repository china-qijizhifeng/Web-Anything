#!/usr/bin/env python3
"""Scan pinned Parquet URL columns for exact-origin historical candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from probe_historical_indexes import PROVIDERS, exact_origin, load_targets


TOOL_VERSION = "webharbor-parquet-origin-scan-v1"
TREE_API = "https://huggingface.co/api/datasets/{dataset}/tree/{revision}/data"
RESOLVE_URL = "https://huggingface.co/datasets/{dataset}/resolve/{revision}/{path}"
MAX_TREE_BYTES = 8 * 1024 * 1024
PROVIDER_ALIASES = {
    "webui_dom_snapshot": "dom",
    "webui_source_bundle": "source",
}


class ParquetOriginScanError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def fetch_bytes(url: str, *, timeout: float, attempts: int) -> tuple[bytes, Any]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = Request(
            url,
            headers={"User-Agent": f"WebHarbor/{TOOL_VERSION}"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_TREE_BYTES + 1)
                if len(body) > MAX_TREE_BYTES:
                    raise ParquetOriginScanError(f"response exceeds byte limit: {url}")
                return body, response.headers
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(min(2**attempt, 8))
    raise ParquetOriginScanError(
        f"request failed after {attempts} attempts: {url}: {last_error}"
    )


def fetch_json_list(url: str, *, timeout: float, attempts: int) -> tuple[list[Any], bytes]:
    body, _ = fetch_bytes(url, timeout=timeout, attempts=attempts)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParquetOriginScanError(f"response is not valid JSON: {url}") from exc
    if not isinstance(value, list):
        raise ParquetOriginScanError(f"JSON response must be a list: {url}")
    return value, body


def fetch_range(
    url: str,
    start: int,
    end: int,
    *,
    timeout: float,
    attempts: int,
) -> tuple[bytes, dict[str, str]]:
    if start < 0 or end < start:
        raise ParquetOriginScanError(f"invalid byte range: {start}-{end}")
    expected = end - start + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = Request(
            url,
            headers={
                "User-Agent": f"WebHarbor/{TOOL_VERSION}",
                "Range": f"bytes={start}-{end}",
                "Accept-Encoding": "identity",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(expected + 1)
                headers = {key.casefold(): value for key, value in response.headers.items()}
                status = getattr(response, "status", response.getcode())
                if status != 206:
                    raise ParquetOriginScanError(
                        f"range request returned HTTP {status}: {url}"
                    )
                if len(body) != expected:
                    raise ParquetOriginScanError(
                        f"range returned {len(body)} bytes, expected {expected}: {url}"
                    )
                content_range = headers.get("content-range")
                if content_range != f"bytes {start}-{end}/{headers_size(headers)}":
                    raise ParquetOriginScanError(
                        f"unexpected Content-Range {content_range!r}: {url}"
                    )
                return body, headers
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(min(2**attempt, 8))
    if isinstance(last_error, ParquetOriginScanError):
        raise last_error
    raise ParquetOriginScanError(
        f"range request failed after {attempts} attempts: {url}: {last_error}"
    )


def headers_size(headers: dict[str, str]) -> int:
    content_range = headers.get("content-range", "")
    match = re.fullmatch(r"bytes \d+-\d+/(\d+)", content_range)
    if not match:
        raise ParquetOriginScanError(
            f"missing or invalid Content-Range: {content_range!r}"
        )
    return int(match.group(1))


def parquet_files(
    provider_name: str,
    provider: dict[str, Any],
    *,
    timeout: float,
    attempts: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = TREE_API.format(
        dataset=quote(provider["dataset"], safe="/"),
        revision=provider["revision"],
    ) + "?recursive=true&expand=true"
    rows, body = fetch_json_list(url, timeout=timeout, attempts=attempts)
    split_pattern = re.compile(
        rf"^data/({'|'.join(re.escape(value) for value in provider['splits'])})-"
        r"(\d+)-of-(\d+)\.parquet$"
    )
    files = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "file":
            continue
        path = row.get("path")
        match = split_pattern.fullmatch(path) if isinstance(path, str) else None
        if match is None:
            continue
        size = row.get("size")
        lfs = row.get("lfs")
        xet_hash = row.get("xetHash")
        if (
            not isinstance(size, int)
            or size < 12
            or not isinstance(lfs, dict)
            or lfs.get("size") != size
            or not isinstance(lfs.get("oid"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", lfs["oid"])
            or not isinstance(xet_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", xet_hash)
        ):
            raise ParquetOriginScanError(
                f"tree entry lacks pinned Parquet identity: {provider['dataset']} {path}"
            )
        files.append({
            "provider": provider_name,
            "dataset": provider["dataset"],
            "revision": provider["revision"],
            "path": path,
            "split": match.group(1),
            "shard": int(match.group(2)),
            "shards": int(match.group(3)),
            "size": size,
            "repository_oid": row.get("oid"),
            "lfs_sha256": lfs["oid"],
            "xet_hash": xet_hash,
            "url_field": provider["url_field"],
        })
    files.sort(key=lambda item: (provider["splits"].index(item["split"]), item["shard"]))
    for split in provider["splits"]:
        split_files = [item for item in files if item["split"] == split]
        if not split_files:
            raise ParquetOriginScanError(
                f"no pinned Parquet files for {provider['dataset']} split {split}"
            )
        expected = split_files[0]["shards"]
        if (
            len(split_files) != expected
            or [item["shard"] for item in split_files] != list(range(expected))
            or any(item["shards"] != expected for item in split_files)
        ):
            raise ParquetOriginScanError(
                f"incomplete pinned Parquet shard set for {provider['dataset']} split {split}"
            )
    return files, {
        "provider": provider_name,
        "dataset": provider["dataset"],
        "revision": provider["revision"],
        "url_field": provider["url_field"],
        "splits": list(provider["splits"]),
        "tree_url": url,
        "tree_bytes": len(body),
        "tree_sha256": sha256_bytes(body),
        "files": len(files),
        "status": "passed",
    }


def cache_prefix(cache_dir: Path, item: dict[str, Any]) -> Path:
    alias = PROVIDER_ALIASES[item["provider"]]
    return cache_dir / f"wh-parquet-{alias}-{item['split']}-{item['shard']:05d}"


def footer_span(tail: bytes, size: int) -> tuple[int, bytes]:
    if len(tail) < 8 or tail[-4:] != b"PAR1":
        raise ParquetOriginScanError("Parquet footer magic is absent")
    metadata_length = struct.unpack("<I", tail[-8:-4])[0]
    span = metadata_length + 8
    if span > size or span > len(tail):
        raise ParquetOriginScanError(
            f"Parquet footer requires {span} bytes but only {len(tail)} are cached"
        )
    return size - span, tail[-span:]


def cached_or_fetch_footer(
    item: dict[str, Any],
    prefix: Path,
    *,
    timeout: float,
    attempts: int,
) -> tuple[int, bytes, list[dict[str, Any]], dict[str, str]]:
    size = item["size"]
    footer_path = prefix.with_name(prefix.name + "-footer.bin")
    legacy_tail_path = prefix.with_name(prefix.name + "-tail1m.bin")
    if footer_path.exists():
        footer = footer_path.read_bytes()
        footer_start, footer = footer_span(footer, size)
        return footer_start, footer, [], {}
    if legacy_tail_path.exists():
        legacy_tail = legacy_tail_path.read_bytes()
        footer_start, footer = footer_span(legacy_tail, size)
        return footer_start, footer, [], {}

    url = RESOLVE_URL.format(
        dataset=quote(item["dataset"], safe="/"),
        revision=item["revision"],
        path=quote(item["path"], safe="/"),
    ) + "?download=true"
    tail, tail_headers = fetch_range(
        url, size - 8, size - 1, timeout=timeout, attempts=attempts
    )
    metadata_length = struct.unpack("<I", tail[:4])[0]
    footer_start = size - metadata_length - 8
    if footer_start < 4:
        raise ParquetOriginScanError(f"invalid Parquet footer length: {url}")
    footer, footer_headers = fetch_range(
        url, footer_start, size - 1, timeout=timeout, attempts=attempts
    )
    footer_path.write_bytes(footer)
    return footer_start, footer, [
        range_evidence(size - 8, tail, tail_headers),
        range_evidence(footer_start, footer, footer_headers),
    ], footer_headers


def range_evidence(
    start: int, body: bytes, headers: dict[str, str]
) -> dict[str, Any]:
    return {
        "offset": start,
        "bytes": len(body),
        "sha256": sha256_bytes(body),
        "etag": headers.get("etag"),
        "content_range": headers.get("content-range"),
    }


def parquet_metadata(sparse_path: Path, size: int, footer_start: int, footer: bytes):
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ParquetOriginScanError(
            "pyarrow is required; run the Skill through run.sh or install builder requirements"
        ) from exc
    sparse_path.parent.mkdir(parents=True, exist_ok=True)
    if not sparse_path.exists() or sparse_path.stat().st_size != size:
        with sparse_path.open("wb") as handle:
            handle.write(b"PAR1")
            handle.truncate(size)
    with sparse_path.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"PAR1")
        handle.seek(footer_start)
        handle.write(footer)
    return pq.ParquetFile(sparse_path)


def column_index(metadata: Any, url_field: str) -> int:
    indexes = []
    for index in range(metadata.row_group(0).num_columns):
        if metadata.row_group(0).column(index).path_in_schema == url_field:
            indexes.append(index)
    if len(indexes) != 1:
        raise ParquetOriginScanError(
            f"expected one leaf column named {url_field!r}, found {len(indexes)}"
        )
    return indexes[0]


def column_range(column: Any, file_size: int) -> tuple[int, int]:
    offsets = [column.data_page_offset]
    if column.dictionary_page_offset is not None:
        offsets.append(column.dictionary_page_offset)
    start = min(value for value in offsets if isinstance(value, int) and value >= 0)
    length = column.total_compressed_size
    end = start + length - 1
    if length < 1 or start < 4 or end >= file_size:
        raise ParquetOriginScanError(
            f"invalid Parquet column range: {start}-{end}/{file_size}"
        )
    return start, end


def scan_file(
    item: dict[str, Any],
    target_origins: dict[tuple[str, str, int | None], list[str]],
    cache_dir: Path,
    *,
    timeout: float,
    attempts: int,
) -> dict[str, Any]:
    prefix = cache_prefix(cache_dir, item)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    footer_start, footer, transfers, footer_headers = cached_or_fetch_footer(
        item, prefix, timeout=timeout, attempts=attempts
    )
    sparse_path = prefix.with_name(prefix.name + "-sparse.parquet")
    parquet = parquet_metadata(sparse_path, item["size"], footer_start, footer)
    metadata = parquet.metadata
    url_column_index = column_index(metadata, item["url_field"])
    column_evidence = []
    transport_etag = footer_headers.get("etag")
    for row_group_index in range(metadata.num_row_groups):
        row_group = metadata.row_group(row_group_index)
        start, end = column_range(
            row_group.column(url_column_index), item["size"]
        )
        range_path = prefix.with_name(prefix.name + f"-rg{row_group_index:02d}.bin")
        if range_path.exists():
            body = range_path.read_bytes()
            if len(body) != end - start + 1:
                raise ParquetOriginScanError(
                    f"cached column range has wrong length: {range_path}"
                )
            headers = {}
        else:
            url = RESOLVE_URL.format(
                dataset=quote(item["dataset"], safe="/"),
                revision=item["revision"],
                path=quote(item["path"], safe="/"),
            ) + "?download=true"
            body, headers = fetch_range(
                url, start, end, timeout=timeout, attempts=attempts
            )
            range_path.write_bytes(body)
            transfers.append(range_evidence(start, body, headers))
            transport_etag = transport_etag or headers.get("etag")
        with sparse_path.open("r+b") as handle:
            handle.seek(start)
            handle.write(body)
        column_evidence.append({
            "row_group": row_group_index,
            "rows": row_group.num_rows,
            "offset": start,
            "bytes": len(body),
            "sha256": sha256_bytes(body),
        })

    urls: list[Any] = []
    matches = []
    malformed_urls = 0
    file_row_index = 0
    for row_group_index in range(metadata.num_row_groups):
        table = parquet.read_row_group(row_group_index, columns=[item["url_field"]])
        values = table[item["url_field"]].to_pylist()
        urls.extend(values)
        for row_group_row, value in enumerate(values):
            if not isinstance(value, str):
                malformed_urls += 1
                file_row_index += 1
                continue
            try:
                origin = exact_origin(value)
            except (RuntimeError, ValueError):
                malformed_urls += 1
                file_row_index += 1
                continue
            sites = target_origins.get(origin)
            if sites:
                matches.append({
                    "sites": sites,
                    "source_url": value,
                    "file_row_index": file_row_index,
                    "row_group": row_group_index,
                    "row_group_row": row_group_row,
                })
            file_row_index += 1
    if len(urls) != metadata.num_rows:
        raise ParquetOriginScanError(
            f"decoded {len(urls)} URLs for {metadata.num_rows} Parquet rows"
        )
    transferred_bytes = sum(entry["bytes"] for entry in transfers)
    scanned_range_bytes = len(footer) + sum(
        entry["bytes"] for entry in column_evidence
    )
    return {
        **item,
        "status": "passed",
        "transport_etag": transport_etag or item["xet_hash"],
        "transport_etag_source": (
            "range_response" if transport_etag else "repository_tree_xet_hash"
        ),
        "rows": metadata.num_rows,
        "row_groups": metadata.num_row_groups,
        "malformed_urls": malformed_urls,
        "url_values_sha256": sha256_bytes(canonical_json_bytes(urls)),
        "footer": {
            "offset": footer_start,
            "bytes": len(footer),
            "sha256": sha256_bytes(footer),
        },
        "column_ranges": column_evidence,
        "scanned_range_bytes": scanned_range_bytes,
        "transferred_bytes": transferred_bytes,
        "range_requests": len(transfers),
        "cache_reused": len(transfers) == 0,
        "transfers": transfers,
        "exact_origin_matches": matches,
    }


def scan(args: argparse.Namespace) -> dict[str, Any]:
    targets = load_targets(args.queue, args.targets)
    target_origins: dict[tuple[str, str, int | None], list[str]] = {}
    for target in targets:
        target_origins.setdefault(exact_origin(target["upstream_url"]), []).append(
            target["site"]
        )
    provider_names = args.providers or list(PROVIDERS)
    files = []
    provider_evidence = []
    for provider_name in provider_names:
        provider_files, evidence = parquet_files(
            provider_name,
            PROVIDERS[provider_name],
            timeout=args.timeout,
            attempts=args.attempts,
        )
        files.extend(provider_files)
        provider_evidence.append(evidence)
    file_evidence = [
        scan_file(
            item,
            target_origins,
            args.cache_dir,
            timeout=args.timeout,
            attempts=args.attempts,
        )
        for item in files
    ]
    matches = [
        {"provider": item["provider"], "path": item["path"], **match}
        for item in file_evidence
        for match in item["exact_origin_matches"]
    ]
    matched_sites = {
        site for match in matches for site in match["sites"]
    }
    return {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "generated_at": utc_now(),
        "status": "passed",
        "selection": {
            "queue": str(args.queue),
            "targets": targets,
            "exact_scheme_hostname_port_required": True,
            "url_columns_only": True,
            "full_parquet_download": False,
        },
        "providers": provider_evidence,
        "files": file_evidence,
        "matches": matches,
        "entries": [{
            **target,
            "status": (
                "exact_origin_candidate" if target["site"] in matched_sites
                else "no_exact_origin_candidate"
            ),
            "retry_conditions": [] if target["site"] in matched_sites else [
                "retry only when a new pinned dataset revision contains an exact-origin URL row"
            ],
        } for target in targets],
        "summary": {
            "targets": len(targets),
            "providers": len(provider_evidence),
            "files": len(file_evidence),
            "rows": sum(item["rows"] for item in file_evidence),
            "scanned_range_bytes": sum(
                item["scanned_range_bytes"] for item in file_evidence
            ),
            "transferred_bytes": sum(
                item["transferred_bytes"] for item in file_evidence
            ),
            "range_requests": sum(item["range_requests"] for item in file_evidence),
            "exact_origin_matches": len(matches),
            "targets_without_match": len(targets) - len(matched_sites),
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
    value.add_argument(
        "--cache-dir", type=Path, default=Path(".assets/parquet-origin-scan")
    )
    value.add_argument("--providers", nargs="*", choices=sorted(PROVIDERS))
    value.add_argument("--timeout", type=float, default=60)
    value.add_argument("--attempts", type=int, default=6)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.attempts < 1 or args.attempts > 10:
        raise SystemExit("--attempts must be between 1 and 10")
    report = scan(args)
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
    except ParquetOriginScanError as exc:
        print(f"Parquet origin scan error: {exc}", file=sys.stderr)
        raise SystemExit(2)
