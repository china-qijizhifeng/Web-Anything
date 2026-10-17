#!/usr/bin/env python3
"""Extract declarative repeated components from verified captured HTML."""

from __future__ import annotations

import argparse
import hashlib
import json
import pprint
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urldefrag, urljoin


class ExtractError(RuntimeError):
    pass


SPACE_RE = re.compile(r"\s+")
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
IGNORED_TEXT_TAGS = {"script", "style", "svg", "noscript", "template"}


def clean_text(value: str) -> str:
    return SPACE_RE.sub(" ", value).strip()


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        parts: list[str] = []

        def collect(node: Node) -> None:
            if node.tag in IGNORED_TEXT_TAGS:
                return
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    collect(child)

        collect(self)
        return clean_text(" ".join(parts))

    def descendants(
        self, *, tag: str | None = None, class_token: str | None = None
    ) -> Iterator[Node]:
        for child in self.children:
            if not isinstance(child, Node):
                continue
            if (tag is None or child.tag == tag) and (
                class_token is None or class_token in child.classes
            ):
                yield child
            yield from child.descendants(tag=tag, class_token=class_token)


class DomBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_document(path: Path) -> Node:
    builder = DomBuilder()
    builder.feed(path.read_text(encoding="utf-8", errors="replace"))
    return builder.root


def matching_nodes(root: Node, selector: dict[str, Any]) -> list[Node]:
    class_token = selector.get("class")
    tag = selector.get("tag")
    if class_token is not None and not isinstance(class_token, str):
        raise ExtractError("field class selector must be a string")
    if tag is not None and not isinstance(tag, str):
        raise ExtractError("field tag selector must be a string")
    return list(root.descendants(tag=tag, class_token=class_token))


def field_values(
    root: Node, field_spec: dict[str, Any], *, base_url: str
) -> Any:
    nodes = matching_nodes(root, field_spec)
    descendant_tag = field_spec.get("descendant_tag")
    if descendant_tag:
        descendants = []
        for node in nodes:
            descendants.extend(node.descendants(tag=descendant_tag))
        nodes = descendants
    value_kind = field_spec.get("value", "text")
    multiple = value_kind in {"texts", "attrs"}
    if not nodes:
        if field_spec.get("required", True):
            raise ExtractError(f"required field {field_spec.get('name')!r} matched no nodes")
        return [] if multiple else ""

    def value(node: Node) -> str:
        if value_kind in {"text", "texts"}:
            return node.text()
        if value_kind in {"attr", "attrs"}:
            attribute = field_spec.get("attr")
            if not isinstance(attribute, str) or not attribute:
                raise ExtractError("attr/attrs field requires attr")
            raw = node.attrs.get(attribute, "")
            if attribute in {"href", "src"} and raw:
                raw, _ = urldefrag(urljoin(base_url, raw))
            return raw
        raise ExtractError(f"unsupported field value kind: {value_kind}")

    values = [value(node) for node in nodes]
    if multiple:
        return [item for item in values if item]
    return values[0]


def extract_item(node: Node, fields: list[dict[str, Any]], base_url: str) -> dict[str, Any]:
    item: dict[str, Any] = {}
    for field_spec in fields:
        name = field_spec.get("name")
        if not isinstance(name, str) or not name:
            raise ExtractError("every component field requires a name")
        item[name] = field_values(node, field_spec, base_url=base_url)
    return item


def extract_collection(
    document: Node, collection: dict[str, Any], *, base_url: str
) -> dict[str, Any]:
    item_class = collection.get("item_class")
    fields = collection.get("fields")
    if not isinstance(item_class, str) or not item_class:
        raise ExtractError("collection item_class must be a non-empty string")
    if not isinstance(fields, list) or not fields:
        raise ExtractError("collection fields must be a non-empty list")
    container_class = collection.get("container_class")
    if container_class is None:
        items = [
            extract_item(node, fields, base_url)
            for node in document.descendants(class_token=item_class)
        ]
        return {"items": items}
    if not isinstance(container_class, str) or not container_class:
        raise ExtractError("container_class must be a non-empty string")
    container_fields = collection.get("container_fields", [])
    groups = []
    for container in document.descendants(class_token=container_class):
        group = extract_item(container, container_fields, base_url) if container_fields else {}
        group["items"] = [
            extract_item(node, fields, base_url)
            for node in container.descendants(class_token=item_class)
        ]
        groups.append(group)
    return {"groups": groups}


def extract_records(
    evidence_path: Path, capture_root: Path, spec_path: Path
) -> dict[str, Any]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if evidence.get("status") != "passed" or evidence.get("failures"):
        raise ExtractError("upstream evidence must be passing and failure-free")
    if spec.get("site") != evidence.get("site"):
        raise ExtractError("component spec site does not match upstream evidence")
    pages = {page["url"]: page for page in evidence.get("pages", [])}
    records: dict[str, Any] = {}
    for collection in spec.get("collections", []):
        name = collection.get("name")
        page_url = collection.get("page_url")
        if not isinstance(name, str) or not name or name in records:
            raise ExtractError("collection names must be unique non-empty strings")
        page = pages.get(page_url)
        if page is None:
            raise ExtractError(f"component page was not captured: {page_url}")
        source = capture_root / page["local_path"]
        if not source.is_file() or file_sha256(source) != page["sha256"]:
            raise ExtractError(f"captured page missing or hash-mismatched: {source}")
        extracted = extract_collection(
            parse_document(source), collection, base_url=page.get("final_url") or page_url
        )
        minimum = int(collection.get("minimum_items", 1))
        count = sum(len(group["items"]) for group in extracted.get("groups", []))
        count = count or len(extracted.get("items", []))
        if count < minimum:
            raise ExtractError(f"collection {name!r} extracted {count}, expected at least {minimum}")
        records[name] = {
            "source_page_url": page_url,
            "source_page_sha256": page["sha256"],
            **extracted,
        }
    if not records:
        raise ExtractError("component spec contains no collections")
    return records


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    root = repository_root()
    evidence_path = (
        args.evidence
        or root / "mirror_factory" / "evidence" / args.site / "upstream.json"
    ).resolve()
    capture_root = root / "sites" / args.site / "static" / "external_cache" / "upstream"
    spec_path = args.spec.resolve()
    output = args.output.resolve()
    site_root = (root / "sites" / args.site).resolve()
    if site_root not in output.parents:
        raise ExtractError(f"output must stay under {site_root}")
    records = extract_records(evidence_path, capture_root, spec_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    header = (
        '"""Generated from declarative, hash-verified upstream components."""\n\n'
        f'CAPTURE_EVIDENCE_SHA256 = "{file_sha256(evidence_path)}"\n'
        f'COMPONENT_SPEC_SHA256 = "{file_sha256(spec_path)}"\n'
        "COMPONENT_RECORDS = "
    )
    output.write_text(
        header + pprint.pformat(records, width=100, sort_dicts=True) + "\n",
        encoding="utf-8",
    )
    counts = {
        name: sum(len(group["items"]) for group in value.get("groups", []))
        or len(value.get("items", []))
        for name, value in records.items()
    }
    print(json.dumps({
        "site": args.site,
        "collections": counts,
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
