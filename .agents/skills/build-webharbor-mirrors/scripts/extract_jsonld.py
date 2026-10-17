#!/usr/bin/env python3
"""Extract selected JSON-LD types from a verified upstream capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import pprint
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


class ExtractError(RuntimeError):
    pass


LAST_UPDATED_RE = re.compile(r"\bLast\s+updated\s+(\d{2}/\d{2}/\d{4})\b", re.IGNORECASE)


class JsonLdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_jsonld = False
        self.ignored_text_depth = 0
        self.buffer: list[str] = []
        self.documents: list[Any] = []
        self.visible_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag in {"script", "style"}:
            self.ignored_text_depth += 1
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self.in_jsonld = True
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.in_jsonld:
            self.buffer.append(data)
        elif self.ignored_text_depth == 0 and data.strip():
            self.visible_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag not in {"script", "style"}:
            return
        if tag == "script" and self.in_jsonld:
            self.in_jsonld = False
            raw = "".join(self.buffer).strip()
            if raw:
                try:
                    self.documents.append(json.loads(raw))
                except json.JSONDecodeError as exc:
                    raise ExtractError(f"invalid JSON-LD: {exc}") from exc
        self.ignored_text_depth = max(0, self.ignored_text_depth - 1)

    def page_metadata(self) -> dict[str, str]:
        match = LAST_UPDATED_RE.search(" ".join(self.visible_text))
        if match is None:
            return {}
        return {"last_updated": match.group(1)}


def iter_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_objects(child)


def type_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_records(evidence_path: Path, capture_root: Path, selected_types: set[str]) -> list[dict[str, Any]]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assets: dict[str, str] = {}
    for asset in evidence.get("assets", []):
        assets[asset["url"]] = asset["local_path"]
        assets[asset["final_url"]] = asset["local_path"]
    records: list[dict[str, Any]] = []
    for page in evidence.get("pages", []):
        source = capture_root / page["local_path"]
        if not source.is_file():
            raise ExtractError(f"captured page missing: {source}")
        actual_hash = file_sha256(source)
        if actual_hash != page["sha256"]:
            raise ExtractError(f"captured page hash mismatch: {source}")
        collector = JsonLdCollector()
        collector.feed(source.read_text(encoding="utf-8", errors="replace"))
        matches: dict[str, dict[str, Any]] = {}
        for document in collector.documents:
            for value in iter_objects(document):
                for type_name in type_names(value.get("@type")) & selected_types:
                    matches.setdefault(type_name, value)
        if not matches:
            continue
        for value in matches.values():
            image = value.get("image")
            if isinstance(image, dict) and image.get("contentUrl") in assets:
                image["captureLocalPath"] = assets[image["contentUrl"]]
            provider = value.get("provider")
            if isinstance(provider, dict) and isinstance(provider.get("logo"), dict):
                logo = provider["logo"]
                if logo.get("contentUrl") in assets:
                    logo["captureLocalPath"] = assets[logo["contentUrl"]]
        record = {
            "source_page_url": page["url"],
            "source_page_sha256": page["sha256"],
            "objects": dict(sorted(matches.items())),
        }
        metadata = collector.page_metadata()
        if metadata:
            record["metadata"] = metadata
        records.append(record)
    return sorted(records, key=lambda item: item["source_page_url"])


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--types", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    root = repository_root()
    evidence_path = (args.evidence or root / "mirror_factory" / "evidence" / args.site / "upstream.json").resolve()
    capture_root = root / "sites" / args.site / "static" / "external_cache" / "upstream"
    output = args.output.resolve()
    site_root = (root / "sites" / args.site).resolve()
    if site_root not in output.parents:
        raise ExtractError(f"output must stay under {site_root}")
    records = extract_records(evidence_path, capture_root, set(args.types))
    if not records:
        raise ExtractError("no matching JSON-LD records found")
    output.parent.mkdir(parents=True, exist_ok=True)
    header = (
        '"""Generated from pinned upstream capture evidence; do not edit by hand."""\n\n'
        f'CAPTURE_EVIDENCE_SHA256 = "{file_sha256(evidence_path)}"\n'
        f'JSONLD_TYPES = {tuple(args.types)!r}\n'
        "UPSTREAM_RECORDS = "
    )
    output.write_text(
        header + pprint.pformat(records, width=100, sort_dicts=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "site": args.site,
        "records": len(records),
        "output": str(output),
        "sha256": file_sha256(output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExtractError as exc:
        print(f"extract error: {exc}", file=sys.stderr)
        raise SystemExit(1)
