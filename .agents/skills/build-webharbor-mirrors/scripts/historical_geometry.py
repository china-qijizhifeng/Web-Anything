#!/usr/bin/env python3
"""Range-read pinned Mind2Web node geometry and bind it to exact HTML bytes."""

from __future__ import annotations

import binascii
import hashlib
import json
import math
import re
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GEOMETRY_DATASET = "neulab/Mind2Web_bbox_eval"
GEOMETRY_REVISION = "b089ea88cd2cabc44c10d62f7e99a896a4aeaf2d"
GEOMETRY_ARCHIVE = "bbox_generate_gt_crop_offline_data_-1choices.zip"
GEOMETRY_ARCHIVE_SHA256 = (
    "39f60389b8faca4d8c0b6f024c29e658632f2cd9948707ecc0bf93a2a7624715"
)
GEOMETRY_ARCHIVE_SIZE = 6_249_999_088
GEOMETRY_CENTRAL_OFFSET = 6_235_960_415
GEOMETRY_CENTRAL_SIZE = 14_038_575
GEOMETRY_CENTRAL_SHA256 = (
    "cb5505d0e8fd5d99a00a5250a438fd4876a09876155e6943d2504dfede20ee24"
)
GEOMETRY_LICENSE = "odc-by"
GEOMETRY_URL = (
    "https://huggingface.co/datasets/"
    f"{GEOMETRY_DATASET}/resolve/{GEOMETRY_REVISION}/{GEOMETRY_ARCHIVE}"
)
GEOMETRY_ROOT = "bbox_generate_gt_crop_offline_data_-1choices"
MAX_ENTRY_BYTES = 5 * 1024 * 1024
ALLOWED_GEOMETRY_ATTRIBUTES = {
    "alt",
    "aria_label",
    "aria_role",
    "bounding_box_rect",
    "class",
    "id",
    "is_clickable",
    "name",
    "placeholder",
    "role",
    "title",
    "type",
}
BBOX_RE = re.compile(
    r"^-?(?:\d+(?:\.\d+)?|\.\d+),"
    r"-?(?:\d+(?:\.\d+)?|\.\d+),"
    r"(?:\d+(?:\.\d+)?|\.\d+),"
    r"(?:\d+(?:\.\d+)?|\.\d+)$"
)
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class HistoricalGeometryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ZipEntry:
    name: str
    compression: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_header_offset: int


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fetch_range(
    start: int,
    length: int,
    *,
    timeout: float,
    attempts: int = 4,
) -> bytes:
    if start < 0 or length <= 0 or start + length > GEOMETRY_ARCHIVE_SIZE:
        raise HistoricalGeometryError("geometry archive range is outside pinned bounds")
    end = start + length - 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = Request(
            GEOMETRY_URL,
            headers={
                "Range": f"bytes={start}-{end}",
                "User-Agent": "WebHarbor/historical-geometry-v1",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                content_range = response.headers.get("Content-Range", "")
                expected = f"bytes {start}-{end}/{GEOMETRY_ARCHIVE_SIZE}"
                if response.status != 206 or content_range != expected:
                    raise HistoricalGeometryError(
                        "geometry archive server did not honor the exact byte range"
                    )
                body = response.read(length + 1)
                if len(body) != length:
                    raise HistoricalGeometryError(
                        f"geometry archive returned {len(body)} bytes, expected {length}"
                    )
                return body
        except (HTTPError, URLError, TimeoutError, OSError, HistoricalGeometryError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 4))
    raise HistoricalGeometryError(
        f"geometry archive range request failed after {attempts} attempts: {last_error}"
    )


def _zip64_values(
    extra: bytes,
    *,
    uncompressed_size: int,
    compressed_size: int,
    local_header_offset: int,
    disk_start: int,
) -> tuple[int, int, int]:
    zip64 = b""
    position = 0
    while position + 4 <= len(extra):
        kind, length = struct.unpack_from("<HH", extra, position)
        position += 4
        value = extra[position:position + length]
        position += length
        if kind == 0x0001:
            zip64 = value
            break
    offset = 0

    def take(size: int) -> int:
        nonlocal offset
        if offset + size > len(zip64):
            raise HistoricalGeometryError("truncated ZIP64 central-directory field")
        fmt = "<Q" if size == 8 else "<I"
        value = struct.unpack_from(fmt, zip64, offset)[0]
        offset += size
        return value

    if uncompressed_size == 0xFFFFFFFF:
        uncompressed_size = take(8)
    if compressed_size == 0xFFFFFFFF:
        compressed_size = take(8)
    if local_header_offset == 0xFFFFFFFF:
        local_header_offset = take(8)
    if disk_start == 0xFFFF:
        take(4)
    return uncompressed_size, compressed_size, local_header_offset


def parse_central_directory(body: bytes) -> dict[str, ZipEntry]:
    entries: dict[str, ZipEntry] = {}
    position = 0
    while position < len(body):
        if position + 46 > len(body):
            raise HistoricalGeometryError("truncated ZIP central-directory record")
        values = struct.unpack_from("<4s6H3I5H2I", body, position)
        if values[0] != b"PK\x01\x02":
            raise HistoricalGeometryError("invalid ZIP central-directory signature")
        (
            _,
            _,
            _,
            flags,
            compression,
            _,
            _,
            crc32,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
            comment_length,
            disk_start,
            _,
            _,
            local_header_offset,
        ) = values
        record_end = position + 46 + name_length + extra_length + comment_length
        if record_end > len(body):
            raise HistoricalGeometryError("ZIP central-directory entry exceeds pinned bytes")
        name_bytes = body[position + 46:position + 46 + name_length]
        extra = body[
            position + 46 + name_length:
            position + 46 + name_length + extra_length
        ]
        encoding = "utf-8" if flags & 0x800 else "cp437"
        try:
            name = name_bytes.decode(encoding)
        except UnicodeDecodeError as exc:
            raise HistoricalGeometryError("invalid ZIP entry filename encoding") from exc
        uncompressed_size, compressed_size, local_header_offset = _zip64_values(
            extra,
            uncompressed_size=uncompressed_size,
            compressed_size=compressed_size,
            local_header_offset=local_header_offset,
            disk_start=disk_start,
        )
        if name in entries:
            raise HistoricalGeometryError(f"duplicate geometry archive entry: {name}")
        entries[name] = ZipEntry(
            name=name,
            compression=compression,
            crc32=crc32,
            compressed_size=compressed_size,
            uncompressed_size=uncompressed_size,
            local_header_offset=local_header_offset,
        )
        position = record_end
    return entries


class HistoricalGeometryArchive:
    def __init__(self, *, cache_root: Path, timeout: float = 60) -> None:
        self.cache_root = cache_root
        self.timeout = timeout
        self._entries: dict[str, ZipEntry] | None = None

    def _central_directory(self) -> bytes:
        cache_path = self.cache_root / f"{GEOMETRY_CENTRAL_SHA256}.central"
        if cache_path.is_file():
            body = cache_path.read_bytes()
            if (
                len(body) == GEOMETRY_CENTRAL_SIZE
                and sha256_bytes(body) == GEOMETRY_CENTRAL_SHA256
            ):
                return body
        body = _fetch_range(
            GEOMETRY_CENTRAL_OFFSET,
            GEOMETRY_CENTRAL_SIZE,
            timeout=self.timeout,
        )
        if sha256_bytes(body) != GEOMETRY_CENTRAL_SHA256:
            raise HistoricalGeometryError("pinned geometry central-directory hash changed")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_bytes(body)
        temporary.replace(cache_path)
        return body

    def entries(self) -> dict[str, ZipEntry]:
        if self._entries is None:
            self._entries = parse_central_directory(self._central_directory())
        return self._entries

    def read(self, name: str) -> tuple[bytes, dict[str, Any]]:
        entry = self.entries().get(name)
        if entry is None:
            raise HistoricalGeometryError(f"geometry archive entry is absent: {name}")
        if entry.uncompressed_size > MAX_ENTRY_BYTES:
            raise HistoricalGeometryError("geometry entry exceeds uncompressed byte limit")
        header = _fetch_range(entry.local_header_offset, 30, timeout=self.timeout)
        values = struct.unpack("<4s5H3I2H", header)
        if values[0] != b"PK\x03\x04":
            raise HistoricalGeometryError("invalid geometry ZIP local-header signature")
        compression = values[3]
        name_length, extra_length = values[-2:]
        if compression != entry.compression:
            raise HistoricalGeometryError("geometry ZIP compression metadata differs")
        payload_offset = entry.local_header_offset + 30 + name_length + extra_length
        compressed = _fetch_range(
            payload_offset,
            entry.compressed_size,
            timeout=self.timeout,
        )
        if entry.compression == 0:
            body = compressed
        elif entry.compression == 8:
            try:
                body = zlib.decompress(compressed, -zlib.MAX_WBITS)
            except zlib.error as exc:
                raise HistoricalGeometryError("geometry ZIP deflate stream is invalid") from exc
        else:
            raise HistoricalGeometryError(
                f"unsupported geometry ZIP compression method: {entry.compression}"
            )
        if len(body) != entry.uncompressed_size:
            raise HistoricalGeometryError("geometry ZIP uncompressed size differs")
        if (binascii.crc32(body) & 0xFFFFFFFF) != entry.crc32:
            raise HistoricalGeometryError("geometry ZIP CRC32 differs")
        return body, {
            "entry": name,
            "entry_crc32": f"{entry.crc32:08x}",
            "entry_compressed_bytes": entry.compressed_size,
            "entry_compressed_sha256": sha256_bytes(compressed),
            "entry_bytes": len(body),
            "entry_sha256": sha256_bytes(body),
        }


def geometry_entry_name(split: str, annotation_id: str, action_uid: str) -> str:
    if split not in {"test_domain", "test_task", "test_website"}:
        raise HistoricalGeometryError(f"geometry archive does not cover split: {split}")
    identity = f"{annotation_id}_{action_uid}"
    if not re.fullmatch(rf"{UUID_RE}_{UUID_RE}", identity):
        raise HistoricalGeometryError("historical row has invalid geometry identity")
    return f"{GEOMETRY_ROOT}/{split}/{identity}/queries.jsonl"


def _safe_candidate_attributes(candidate: dict[str, Any]) -> tuple[str, str, dict[str, str]] | None:
    node_id = candidate.get("backend_node_id")
    tag = candidate.get("tag")
    raw_attributes = candidate.get("attributes")
    if not isinstance(node_id, str) or not node_id.isdigit():
        return None
    if not isinstance(tag, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9:_-]*", tag):
        return None
    if not isinstance(raw_attributes, str) or len(raw_attributes) > 16_384:
        return None
    try:
        attributes = json.loads(raw_attributes)
    except json.JSONDecodeError:
        return None
    if not isinstance(attributes, dict):
        return None
    if str(attributes.get("backend_node_id")) != node_id:
        return None
    output: dict[str, str] = {}
    for key in ALLOWED_GEOMETRY_ATTRIBUTES:
        value = attributes.get(key)
        if not isinstance(value, str) or len(value) > 2_048:
            continue
        if any(ord(character) < 32 for character in value):
            continue
        if key == "bounding_box_rect":
            if not BBOX_RE.fullmatch(value):
                continue
            try:
                x, y, width, height = (float(part) for part in value.split(","))
            except ValueError:
                continue
            if not all(math.isfinite(number) for number in (x, y, width, height)):
                continue
            if width <= 0 or height <= 0:
                continue
        output[key] = value
    if "bounding_box_rect" not in output:
        return None
    return node_id, tag.casefold(), output


def load_geometry_for_row(
    row: dict[str, Any],
    *,
    split: str,
    archive: HistoricalGeometryArchive,
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    annotation_id = row.get("annotation_id")
    action_uid = row.get("action_uid")
    source_html = row.get("raw_html")
    if not all(isinstance(value, str) and value for value in (
        annotation_id, action_uid, source_html
    )):
        raise HistoricalGeometryError("historical row lacks geometry identity or HTML")
    name = geometry_entry_name(split, annotation_id, action_uid)
    body, entry_evidence = archive.read(name)
    matching: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(body.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise HistoricalGeometryError(
                f"geometry query entry has invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(value, dict):
            continue
        if value.get("annotation_id") == annotation_id and value.get("action_uid") == action_uid:
            matching.append(value)
    if not matching:
        raise HistoricalGeometryError("geometry entry has no matching annotation/action row")
    for value in matching:
        if value.get("website") != row.get("website"):
            raise HistoricalGeometryError("geometry row website differs from source row")
        if value.get("cleaned_html") != source_html:
            raise HistoricalGeometryError("geometry row HTML differs from source row bytes")

    source_node_ids = set(re.findall(
        r"\bbackend_node_id\s*=\s*[\"'](\d+)[\"']",
        source_html,
        flags=re.IGNORECASE,
    ))
    geometry: dict[str, dict[str, str]] = {}
    conflicts = 0
    candidates = 0
    for value in matching:
        for key in ("pos_candidates", "neg_candidates"):
            values = value.get(key)
            if not isinstance(values, list):
                continue
            for candidate in values:
                if not isinstance(candidate, dict):
                    continue
                parsed = _safe_candidate_attributes(candidate)
                if parsed is None:
                    continue
                node_id, tag, attributes = parsed
                if node_id not in source_node_ids:
                    continue
                candidates += 1
                existing = geometry.setdefault(node_id, {"tag": tag})
                if existing["tag"] != tag:
                    conflicts += 1
                    continue
                for attr_name, attr_value in attributes.items():
                    if attr_name in existing and existing[attr_name] != attr_value:
                        conflicts += 1
                        continue
                    existing[attr_name] = attr_value
    bbox_nodes = sum("bounding_box_rect" in value for value in geometry.values())
    if bbox_nodes < 20:
        raise HistoricalGeometryError(
            f"geometry entry has only {bbox_nodes} usable bounded nodes"
        )
    evidence = {
        "kind": "hf_dataset_archive_entry",
        "dataset": GEOMETRY_DATASET,
        "revision": GEOMETRY_REVISION,
        "filename": GEOMETRY_ARCHIVE,
        "archive_bytes": GEOMETRY_ARCHIVE_SIZE,
        "archive_sha256": GEOMETRY_ARCHIVE_SHA256,
        "central_directory_offset": GEOMETRY_CENTRAL_OFFSET,
        "central_directory_bytes": GEOMETRY_CENTRAL_SIZE,
        "central_directory_sha256": GEOMETRY_CENTRAL_SHA256,
        "license": GEOMETRY_LICENSE,
        "html_sha256": hashlib.sha256(source_html.encode("utf-8")).hexdigest(),
        "matching_query_rows": len(matching),
        "candidate_records": candidates,
        "bbox_nodes": bbox_nodes,
        "conflicts_ignored": conflicts,
        **entry_evidence,
    }
    return geometry, evidence
