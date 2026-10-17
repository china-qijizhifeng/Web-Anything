#!/usr/bin/env python3
"""Extract a verified, traceable content inventory from captured HTML pages."""

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
from urllib.parse import urldefrag, urljoin


class ExtractError(RuntimeError):
    pass


SPACE_RE = re.compile(r"\s+")
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


def clean_text(parts: list[str]) -> str:
    return SPACE_RE.sub(" ", " ".join(parts)).strip()


class HtmlContentCollector(HTMLParser):
    """Collect stable content from the document's main landmark only."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.main_depth = 0
        self.ignored_depth = 0
        self.title_depth = 0
        self.title_parts: list[str] = []
        self.meta_description = ""
        self.visible_parts: list[str] = []
        self.headings: list[dict[str, Any]] = []
        self.links: list[dict[str, str]] = []
        self.sections: list[dict[str, Any]] = []
        self.current_heading: dict[str, Any] | None = None
        self.current_link: dict[str, Any] | None = None
        self.current_section: dict[str, Any] | None = None

    def _finish_section(self) -> None:
        if self.current_section is None:
            return
        self.current_section["text"] = clean_text(self.current_section.pop("parts"))
        self.sections.append(self.current_section)
        self.current_section = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag == "meta" and values.get("name", "").lower() == "description":
            self.meta_description = clean_text([values.get("content", "")])
        if tag == "title" and not self.title_parts and not self.title_depth:
            self.title_depth += 1
        if tag == "main" and self.main_depth == 0:
            self.main_depth = 1
        elif self.main_depth and tag not in VOID_TAGS:
            self.main_depth += 1
        if tag in {"script", "style", "svg", "noscript", "template"}:
            self.ignored_depth += 1
        if not self.main_depth or self.ignored_depth:
            return
        if tag in {"h1", "h2", "h3", "h4"}:
            self._finish_section()
            self.current_heading = {"level": int(tag[1]), "parts": []}
        if tag == "a" and values.get("href"):
            absolute, _ = urldefrag(urljoin(self.base_url, values["href"]))
            self.current_link = {"url": absolute, "parts": []}

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if not self.main_depth or self.ignored_depth or not data.strip():
            return
        self.visible_parts.append(data)
        if self.current_heading is not None:
            self.current_heading["parts"].append(data)
        elif self.current_section is not None:
            self.current_section["parts"].append(data)
        if self.current_link is not None:
            self.current_link["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.title_depth = max(0, self.title_depth - 1)
        if tag in {"script", "style", "svg", "noscript", "template"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        if tag in {"h1", "h2", "h3", "h4"} and self.current_heading is not None:
            text = clean_text(self.current_heading.pop("parts"))
            if text:
                self.current_heading["text"] = text
                self.headings.append(self.current_heading)
                self.current_section = {
                    "level": self.current_heading["level"],
                    "heading": text,
                    "parts": [],
                    "links": [],
                }
            self.current_heading = None
        if tag == "a" and self.current_link is not None:
            text = clean_text(self.current_link.pop("parts"))
            if text:
                self.current_link["text"] = text
                if self.current_link not in self.links:
                    self.links.append(self.current_link)
                if self.current_section is not None and self.current_link not in self.current_section["links"]:
                    self.current_section["links"].append(dict(self.current_link))
            self.current_link = None
        if tag == "main":
            self._finish_section()
        if self.main_depth:
            self.main_depth = max(0, self.main_depth - 1)

    def record(self) -> dict[str, Any]:
        self._finish_section()
        return {
            "title": clean_text(self.title_parts),
            "meta_description": self.meta_description,
            "headings": self.headings,
            "links": self.links,
            "sections": self.sections,
            "main_text": clean_text(self.visible_parts),
        }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_records(evidence_path: Path, capture_root: Path) -> list[dict[str, Any]]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("status") != "passed" or evidence.get("failures"):
        raise ExtractError("upstream evidence must be passing and failure-free")
    records: list[dict[str, Any]] = []
    for page in evidence.get("pages", []):
        source = capture_root / page["local_path"]
        if not source.is_file():
            raise ExtractError(f"captured page missing: {source}")
        actual_hash = file_sha256(source)
        if actual_hash != page["sha256"]:
            raise ExtractError(f"captured page hash mismatch: {source}")
        collector = HtmlContentCollector(page.get("final_url") or page["url"])
        collector.feed(source.read_text(encoding="utf-8", errors="replace"))
        content = collector.record()
        if not content["title"] or not content["headings"] or not content["main_text"]:
            raise ExtractError(f"captured page lacks required content landmarks: {source}")
        records.append({
            "source_page_url": page["url"],
            "source_page_final_url": page.get("final_url") or page["url"],
            "source_page_sha256": page["sha256"],
            **content,
        })
    if not records:
        raise ExtractError("upstream evidence contains no captured pages")
    return sorted(records, key=lambda item: item["source_page_url"])


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    root = repository_root()
    evidence_path = (
        args.evidence
        or root / "mirror_factory" / "evidence" / args.site / "upstream.json"
    ).resolve()
    capture_root = root / "sites" / args.site / "static" / "external_cache" / "upstream"
    output = args.output.resolve()
    site_root = (root / "sites" / args.site).resolve()
    if site_root not in output.parents:
        raise ExtractError(f"output must stay under {site_root}")
    records = extract_records(evidence_path, capture_root)
    header = (
        '"""Generated from pinned upstream HTML evidence; do not edit by hand."""\n\n'
        f'CAPTURE_EVIDENCE_SHA256 = "{file_sha256(evidence_path)}"\n'
        "UPSTREAM_PAGES = "
    )
    output.parent.mkdir(parents=True, exist_ok=True)
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
