#!/usr/bin/env python3
"""Inventory candidate interaction code and an isolated Flask route map.

The analyzer is intentionally bounded.  It recognizes deterministic Python,
HTML/Jinja, and JavaScript forms and records every unresolved active target
instead of guessing.  Flask applications are imported only from a temporary
copy whose runtime database is restored from canonical seed bytes.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from contract import (
    CODE_ANALYZER_TOOL,
    analyzer_implementation_sha256,
    atomic_json,
    canonical_control_identity,
    canonical_json,
    canonical_query,
    semantic_control_key,
    sha256_file,
)

INVENTORY_SCHEMA = "webharbor-code-interaction-inventory-v1"
ANALYZER_TOOL = CODE_ANALYZER_TOOL
ROUTE_MAP_TOOL = "webharbor-isolated-flask-route-map-v1"
SOURCE_SUFFIXES = {".py", ".html", ".htm", ".jinja", ".jinja2", ".js", ".mjs", ".cjs"}
TEMPLATE_SUFFIXES = {".html", ".htm", ".jinja", ".jinja2"}
SCRIPT_SUFFIXES = {".js", ".mjs", ".cjs"}
IGNORED_DIRS = {"instance", "__pycache__", ".pytest_cache"}
ARCHIVE_DIRS = {
    "evidence",
    "snapshots",
    "source_snapshots",
    "captures",
    "logs",
    "vendor",
}
ACTIVE_ROLES = {"button", "link", "menuitem", "tab"}
INTERNAL_RULES = {"/_health"}


def _record_id(kind: str, file: str, line: int, discriminator: str) -> str:
    value = f"{kind}\0{file}\0{line}\0{discriminator}".encode()
    return f"src-{hashlib.sha256(value).hexdigest()[:20]}"


def _literal(node: ast.AST | None) -> Any | None:
    try:
        return ast.literal_eval(node) if node is not None else None
    except (ValueError, TypeError, SyntaxError):
        return None


def _target(value: str | None, source_route: str | None = None) -> dict[str, Any]:
    raw = (value or "").strip()
    result: dict[str, Any] = {
        "literal_target": raw or None,
        "normalized_target": None,
        "target_kind": "none" if not raw else "dynamic",
    }
    if not raw:
        return result
    url_for = re.fullmatch(
        r"\{\{\s*url_for\(\s*(['\"])([A-Za-z0-9_.-]+)\1(?:\s*,[^)]*)?\)\s*\}\}",
        raw,
    )
    if url_for:
        result.update({"target_kind": "endpoint", "endpoint": url_for.group(2)})
        return result
    if any(token in raw for token in ("{{", "}}", "{%", "%}")):
        return result
    if raw.startswith("#"):
        result.update({"target_kind": "fragment", "normalized_target": raw})
        return result
    parsed = urlsplit(raw)
    if parsed.scheme in {"http", "https"} or raw.startswith("//"):
        result.update({"target_kind": "external", "normalized_target": raw})
        return result
    if parsed.scheme:
        result.update({"target_kind": "external", "normalized_target": raw})
        return result
    if source_route:
        base_path = urlsplit(source_route).path or "/"
        normalized = urljoin(base_path, raw)
    else:
        normalized = raw if raw.startswith("/") else "/" + raw
    parts = urlsplit(normalized)
    path = parts.path or "/"
    result.update({
        "target_kind": "local",
        "normalized_target": urlunsplit(("", "", path, parts.query, "")),
    })
    return result


def _tree_identity(root: Path, paths: list[Path]) -> dict[str, Any]:
    digest = hashlib.sha256()
    entries = []
    total = 0
    for path in sorted(paths):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        path_bytes = relative.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        entries.append({"path": relative, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        total += len(payload)
    return {
        "algorithm": "webharbor-selected-files-v1",
        "sha256": digest.hexdigest(),
        "files": len(entries),
        "bytes": total,
        "entries": entries,
    }


def _is_excluded_source_path(path: Path, site_root: Path) -> tuple[bool, str | None]:
    relative = path.relative_to(site_root)
    parts = {part.casefold() for part in relative.parts}
    if parts & IGNORED_DIRS:
        return True, "runtime_or_tooling_directory"
    if "upstream" in parts:
        return True, "archived_upstream_capture"
    if parts & ARCHIVE_DIRS:
        return True, "archived_or_vendor_source"
    if path.name.casefold().endswith((".min.js", ".min.mjs", ".min.cjs")):
        return True, "minified_bundle_without_loaded_proof"
    return False, None


def source_closure(site_root: Path) -> dict[str, Any]:
    """Classify executable/served source without activating capture archives.

    Python application code is always retained.  Templates and scripts are
    retained as candidate-owned source records, while controls from templates
    that are not linked from a rendered route are marked inactive.  Files under
    archived upstream/evidence/vendor paths are excluded entirely.  A compressed
    replay bundle is included only when the application references it directly,
    proving that its bytes are runtime-served rather than an unreferenced cache.
    """
    site_root = site_root.resolve()
    all_paths = sorted(path for path in site_root.rglob("*") if path.is_file())
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    python_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in all_paths
        if path.suffix == ".py" and not _is_excluded_source_path(path, site_root)[0]
    )
    reference_text = python_text + "\n" + "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in all_paths
        if path.suffix.lower() in TEMPLATE_SUFFIXES
        and not _is_excluded_source_path(path, site_root)[0]
    )
    for path in all_paths:
        relative = path.relative_to(site_root).as_posix()
        excluded_flag, reason = _is_excluded_source_path(path, site_root)
        suffix = path.suffix.lower()
        is_bundle = suffix in {".gz", ".br"} and path.name.casefold().endswith((".json.gz", ".json.br"))
        candidate_source = suffix in SOURCE_SUFFIXES or is_bundle
        if not candidate_source:
            continue
        if excluded_flag:
            excluded.append({"path": relative, "reason": reason, "sha256": sha256_file(path), "bytes": path.stat().st_size})
            continue
        if is_bundle:
            referenced = path.name in python_text or relative in python_text
            if not referenced:
                excluded.append({"path": relative, "reason": "unreferenced_external_cache", "sha256": sha256_file(path), "bytes": path.stat().st_size})
                continue
            included.append({"path": relative, "reason": "application_runtime_bundle_reference", "sha256": sha256_file(path), "bytes": path.stat().st_size, "active": True})
            continue
        referenced = (
            path.suffix == ".py"
            or relative in reference_text
            or path.name in reference_text
            or f"/static/{path.relative_to(site_root / 'static').as_posix()}" in reference_text
            if "static" in path.parts
            else path.suffix.lower() in TEMPLATE_SUFFIXES
        )
        reason = "actually_loaded_candidate_asset" if referenced and "static" in path.parts else "candidate_source"
        included.append({
            "path": relative,
            "reason": reason,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "active": bool(referenced),
        })
    return {
        "algorithm": "webharbor-executable-source-closure-v1",
        "included": included,
        "excluded": excluded,
        "included_paths": [item["path"] for item in included],
        "excluded_paths": [item["path"] for item in excluded],
    }


def _selected_source_paths(site_root: Path) -> list[Path]:
    closure = source_closure(site_root)
    allowed = set(closure["included_paths"])
    return [site_root / relative for relative in sorted(allowed) if Path(relative).suffix.lower() in SOURCE_SUFFIXES]


def _seed_paths(site_root: Path) -> list[Path]:
    seed_root = site_root / "instance_seed"
    return list(seed_root.rglob("*.db")) if seed_root.is_dir() else []


def _runtime_bundle_documents(site_root: Path, closure: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Load only application-referenced replay pages and their asset proofs."""
    documents: list[dict] = []
    loaded_assets: list[dict] = []
    for item in closure.get("included", []):
        relative = item["path"]
        if not relative.casefold().endswith(".json.gz"):
            continue
        path = site_root / relative
        try:
            with gzip.open(path, "rb") as handle:
                bundle = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(bundle, dict) or not isinstance(bundle.get("pages"), list):
            continue
        asset_by_key = {
            str(asset.get("asset_key")): asset
            for asset in bundle.get("assets", [])
            if isinstance(asset, dict) and asset.get("asset_key")
        }
        for page in bundle["pages"]:
            if not isinstance(page, dict) or not isinstance(page.get("body"), str):
                continue
            route = canonical_query(str(page.get("path") or "/")) or "/"
            body = page["body"]
            page_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            source_path = f"{relative}#pages/{route}"
            documents.append({
                "source_path": source_path,
                "route": route,
                "sha256": page_sha,
                "bundle_path": relative,
                "bytes": len(body.encode("utf-8")),
            })
            for match in re.finditer(r"(?:src|href|poster|data-src)=[\"']([^\"']+)[\"']", body, re.IGNORECASE):
                value = match.group(1)
                key_match = re.search(r"/__webharbor_asset/([^?#\"']+)", value)
                if not key_match or key_match.group(1) not in asset_by_key:
                    continue
                asset = asset_by_key[key_match.group(1)]
                loaded_assets.append({
                    "asset_key": key_match.group(1),
                    "source_path": source_path,
                    "route": route,
                    "source_url": asset.get("source_url"),
                    "content_type": asset.get("content_type"),
                    "source_sha256": asset.get("source_sha256"),
                    "reachability": "captured-rendered-page-reference",
                })
    return documents, sorted(
        {json.dumps(item, sort_keys=True): item for item in loaded_assets}.values(),
        key=lambda item: (item["route"], item["asset_key"]),
    )


def canonical_control_families(records: list[dict], *, route_key: str = "source_routes") -> list[dict]:
    """Group repeated controls by semantic target while retaining every member."""
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for record in records:
        routes = record.get(route_key) or record.get("source_path") or record.get("route") or "/"
        route = routes[0] if isinstance(routes, list) and routes else str(routes)
        key = semantic_control_key(record, route=route)
        family = grouped.get(key)
        if family is None:
            digest = hashlib.sha256("\0".join(key).encode("utf-8")).hexdigest()[:20]
            family = {
                "family_id": f"control-family-{digest}",
                "semantic_key": list(key),
                "control_type": record.get("control_type"),
                "accessible_name": record.get("accessible_name"),
                "route": canonical_query(route) or "/",
                "raw_observation_count": 0,
                "members": [],
                "unique_targets": [],
            }
            grouped[key] = family
        family["raw_observation_count"] += int(record.get("raw_observation_count") or 1)
        family["members"].append(record)
        target = record.get("normalized_target") or record.get("href") or record.get("literal_target")
        if target and target not in family["unique_targets"]:
            family["unique_targets"].append(target)
    result = []
    for family in grouped.values():
        family["members"] = sorted(
            family["members"],
            key=lambda item: (
                str(item.get("source_path") or item.get("route") or ""),
                str(item.get("selector") or ""),
                str(item.get("id") or item.get("audit_id") or ""),
            ),
        )
        family["unique_targets"] = sorted(family["unique_targets"])
        family["representative"] = family["members"][0]
        result.append(family)
    return sorted(result, key=lambda item: item["family_id"])


class PythonInventory(ast.NodeVisitor):
    def __init__(self, relative: str, text: str):
        self.relative = relative
        self.text = text
        self.function: str | None = None
        self.server_declarations: list[dict] = []
        self.interactions: list[dict] = []
        self.template_calls: list[dict] = []
        self.unresolved: list[dict] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.function
        self.function = node.name
        for decorator in node.decorator_list:
            self._route_decorator(decorator, node.name)
        self.generic_visit(node)
        self.function = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def _route_decorator(self, node: ast.AST, endpoint: str) -> None:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return
        kind = node.func.attr
        if kind not in {"route", "get", "post", "put", "patch", "delete"}:
            return
        raw = _literal(node.args[0]) if node.args else None
        methods: list[str]
        if kind == "route":
            methods_value = next((_literal(keyword.value) for keyword in node.keywords if keyword.arg == "methods"), None)
            methods = [str(item).upper() for item in methods_value] if isinstance(methods_value, (list, tuple)) else ["GET"]
        else:
            methods = [kind.upper()]
        target = _target(raw if isinstance(raw, str) else None)
        record = {
            "id": _record_id("python_route_decorator", self.relative, node.lineno, endpoint),
            "file": self.relative,
            "line": node.lineno,
            "syntax_kind": "python_flask_route_decorator",
            **target,
            "route_rule": raw if isinstance(raw, str) else None,
            "endpoint": endpoint,
            "methods": sorted(set(methods)),
            "method": ",".join(sorted(set(methods))),
            "event": "request",
            "confidence": "high" if isinstance(raw, str) else "unresolved",
            "function": endpoint,
        }
        self.server_declarations.append(record)
        if not isinstance(raw, str):
            self.unresolved.append({
                **record,
                "reason": "dynamic Flask route decorator target",
                "affects_active_surface": True,
            })

    def visit_Call(self, node: ast.Call) -> None:
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in {"url_for", "redirect", "render_template"}:
            argument = node.args[0] if node.args else None
            raw = _literal(argument)
            endpoint = None
            if name == "redirect" and isinstance(argument, ast.Call):
                nested_name = argument.func.id if isinstance(argument.func, ast.Name) else (
                    argument.func.attr if isinstance(argument.func, ast.Attribute) else None
                )
                nested = _literal(argument.args[0]) if nested_name == "url_for" and argument.args else None
                if isinstance(nested, str):
                    endpoint = nested
            syntax = {
                "url_for": "python_url_for",
                "redirect": "python_redirect",
                "render_template": "python_render_template",
            }[name]
            target = _target(raw if isinstance(raw, str) else None)
            if endpoint:
                target = {
                    "literal_target": f"url_for:{endpoint}",
                    "normalized_target": None,
                    "target_kind": "endpoint",
                    "endpoint": endpoint,
                }
            record = {
                "id": _record_id(syntax, self.relative, node.lineno, str(raw)),
                "file": self.relative,
                "line": node.lineno,
                "syntax_kind": syntax,
                **target,
                "method": "GET" if name != "render_template" else None,
                "event": "render" if name == "render_template" else "navigate",
                "confidence": "high" if isinstance(raw, str) or endpoint else "unresolved",
                "function": self.function,
            }
            if name == "url_for" and isinstance(raw, str):
                record.update({"target_kind": "endpoint", "endpoint": raw, "normalized_target": None})
            if name == "render_template" and isinstance(raw, str):
                record.update({"target_kind": "template", "normalized_target": f"templates/{raw}"})
            self.interactions.append(record)
            if name == "render_template":
                self.template_calls.append(record)
            if not isinstance(raw, str) and not endpoint:
                self.unresolved.append({
                    **record,
                    "reason": f"dynamic {name} target",
                    "affects_active_surface": name != "render_template",
                })
        self.generic_visit(node)


def _css_attribute_value(value: str) -> str:
    """Escape a value used inside a CSS quoted attribute selector."""
    # CSS strings only require escaping backslash, quote, and line breaks here;
    # preserving other Unicode makes emitted selectors readable and stable.
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\0D ")
        .replace("\n", "\\0A ")
        .replace("\f", "\\0C ")
    )


def _css_identifier(value: str) -> str:
    escaped = []
    for index, char in enumerate(str(value)):
        if (char.isalpha() or char in "_-" or (char.isdigit() and index > 0)):
            escaped.append(char)
        else:
            escaped.append(f"\\{ord(char):X} ")
    return "".join(escaped)


def _selector(tag: str, attrs: dict[str, str | None]) -> str | None:
    """Derive a deterministic identity selector for safe literal attributes."""
    tag = tag.lower()
    if attrs.get("id"):
        return "#" + _css_identifier(str(attrs["id"]))
    if attrs.get("data-testid"):
        return f'{tag}[data-testid="{_css_attribute_value(str(attrs["data-testid"]))}"]'
    if attrs.get("name"):
        return f'{tag}[name="{_css_attribute_value(str(attrs["name"]))}"]'
    # Literal targets are stable source provenance even when an author did not
    # add an id/name.  Jinja/dynamic values are intentionally excluded.
    if tag == "a" and attrs.get("href") and not any(token in str(attrs["href"]) for token in ("{{", "}}", "{%", "%}")):
        return f'a[href="{_css_attribute_value(str(attrs["href"]))}"]'
    if tag == "form" and attrs.get("action") and not any(token in str(attrs["action"]) for token in ("{{", "}}", "{%", "%}")):
        return f'form[action="{_css_attribute_value(str(attrs["action"]))}"]'
    for attribute in ("aria-label", "title", "placeholder", "value"):
        value = attrs.get(attribute)
        if value and not any(token in str(value) for token in ("{{", "}}", "{%", "%}")):
            return f'{tag}[{attribute}="{_css_attribute_value(str(value))}"]'
    return None


def _control_type(tag: str, attrs: dict[str, str | None]) -> str:
    role = (attrs.get("role") or "").lower()
    input_type = (attrs.get("type") or "").lower()
    if role == "tab":
        return "tab"
    if tag == "form":
        return "form"
    if tag == "select":
        return "filter"
    if tag in {"input", "textarea"}:
        return "search" if input_type == "search" else "input"
    if tag == "summary" or "aria-controls" in attrs:
        return "menu" if (attrs.get("aria-haspopup") or "").lower() == "menu" else "disclosure"
    if tag == "a" or role == "link":
        if (attrs.get("target") or "").lower() == "_blank":
            return "popup"
        if "download" in attrs:
            return "download"
        return "link"
    if tag == "button" or role in {"button", "menuitem"}:
        return "button"
    return "state_change"


class TemplateInventory(HTMLParser):
    def __init__(self, relative: str, text: str):
        super().__init__(convert_charrefs=True)
        self.relative = relative
        self.text = text
        self.controls: list[dict] = []
        self.interactions: list[dict] = []
        self.unresolved: list[dict] = []
        self._open_controls: list[dict] = []
        self._script_line: int | None = None
        self._script: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {name.lower(): value for name, value in attrs_list}
        tag = tag.lower()
        line = self.getpos()[0]
        if tag == "script" and not attrs.get("src"):
            self._script_line = line
            self._script = []
        role = (attrs.get("role") or "").lower()
        active = (
            (tag == "a" and "href" in attrs)
            or tag in {"button", "form", "summary", "select", "textarea"}
            or (tag == "input" and (attrs.get("type") or "").lower() != "hidden")
            or role in ACTIVE_ROLES
            or any(name in attrs for name in ("onclick", "data-action", "aria-controls"))
            or ("tabindex" in attrs and attrs.get("tabindex") != "-1")
        )
        if not active:
            return
        raw_target = attrs.get("href") if tag == "a" else attrs.get("action") if tag == "form" else None
        if raw_target is None and attrs.get("onclick"):
            match = re.search(
                r"(?:window\.)?(?:location(?:\.href)?\s*=|open\s*\()\s*(['\"])(.*?)\1",
                attrs["onclick"] or "",
            )
            raw_target = match.group(2) if match else None
        target = _target(raw_target)
        semantics = [name for name in ("href", "action", "onclick", "data-action", "aria-controls", "tabindex", "role") if name in attrs]
        if tag in {"button", "input", "select", "textarea", "form", "summary"}:
            semantics.append(tag)
        record = {
            "id": _record_id(f"html_{tag}", self.relative, line, str(attrs.get("id") or attrs.get("name") or raw_target)),
            "file": self.relative,
            "line": line,
            "syntax_kind": f"html_{tag}_control",
            "tag": tag,
            "element_id": attrs.get("id"),
            "selector": _selector(tag, attrs),
            "identity_selector": _selector(tag, attrs),
            "runtime_selector": None,
            "accessible_name": str(attrs.get("aria-label") or attrs.get("title") or attrs.get("placeholder") or attrs.get("value") or attrs.get("name") or "").strip(),
            "control_type": _control_type(tag, attrs),
            "active_semantics": sorted(set(semantics)),
            "method": (attrs.get("method") or "GET").upper() if tag == "form" else "GET" if tag == "a" else None,
            "event": "submit" if tag == "form" else "navigate" if tag == "a" else "click" if tag in {"button", "summary"} or role in ACTIVE_ROLES else "input",
            "confidence": "high" if _selector(tag, attrs) else "medium",
            "disabled_literal": "disabled" in attrs or (attrs.get("aria-disabled") or "").lower() == "true",
            "hidden_literal": "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true" or "display:none" in (attrs.get("style") or "").replace(" ", "").lower(),
            **target,
            "_text": [],
        }
        self.controls.append(record)
        self._open_controls.append(record)
        if target["target_kind"] == "dynamic" and raw_target:
            self.unresolved.append({
                **{key: value for key, value in record.items() if key != "_text"},
                "reason": "dynamic HTML/Jinja active target",
                "affects_active_surface": True,
            })
        if attrs.get("onclick"):
            self.interactions.extend(scan_javascript(
                attrs["onclick"] or "",
                self.relative,
                line_offset=line - 1,
                forced_selector=record["selector"],
                syntax_prefix="html_onclick",
            ))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self._close_control(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._script_line is not None:
            self.interactions.extend(scan_javascript(
                "".join(self._script),
                self.relative,
                line_offset=self._script_line,
                syntax_prefix="inline_javascript",
            ))
            self._script_line = None
            self._script = []
        self._close_control(tag)

    def _close_control(self, tag: str) -> None:
        for index in range(len(self._open_controls) - 1, -1, -1):
            record = self._open_controls[index]
            if record["tag"] != tag:
                continue
            self._open_controls.pop(index)
            if not record["accessible_name"]:
                record["accessible_name"] = " ".join("".join(record["_text"]).split())
            record.pop("_text", None)
            break

    def handle_data(self, data: str) -> None:
        if self._script_line is not None:
            self._script.append(data)
        for record in self._open_controls:
            record["_text"].append(data)

    def close(self) -> None:
        super().close()
        for record in self.controls:
            if "_text" in record:
                if not record["accessible_name"]:
                    record["accessible_name"] = " ".join("".join(record["_text"]).split())
                record.pop("_text", None)
            if record.get("tag") in {"input", "textarea", "select"} and record.get("element_id"):
                label_match = re.search(
                    rf"<label[^>]*for=[\"']{re.escape(str(record['element_id']))}[\"'][^>]*>(.*?)</label>",
                    self.text,
                    re.IGNORECASE | re.DOTALL,
                )
                if label_match:
                    label_text = re.sub(r"<[^>]+>", " ", label_match.group(1))
                    label_text = " ".join(label_text.split())
                    if label_text:
                        record["accessible_name"] = label_text


def scan_javascript(
    text: str,
    relative: str,
    *,
    line_offset: int = 0,
    forced_selector: str | None = None,
    syntax_prefix: str = "javascript",
) -> list[dict]:
    records: list[dict] = []
    handlers: list[tuple[int, str | None, str]] = []
    direct_patterns = [
        (re.compile(r"document\.getElementById\(\s*(['\"])(.*?)\1\s*\)\.addEventListener\(\s*(['\"])(.*?)\3"), lambda match: "#" + match.group(2), 4),
        (re.compile(r"document\.querySelector\(\s*(['\"])(.*?)\1\s*\)\.addEventListener\(\s*(['\"])(.*?)\3"), lambda match: match.group(2), 4),
    ]
    for pattern, selector_getter, event_group in direct_patterns:
        for match in pattern.finditer(text):
            handlers.append((match.start(), selector_getter(match), match.group(event_group)))
    variables = {}
    for match in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*document\.(getElementById|querySelector)\(\s*(['\"])(.*?)\3\s*\)", text):
        variables[match.group(1)] = "#" + match.group(4) if match.group(2) == "getElementById" else match.group(4)
    for match in re.finditer(r"\b(\w+)\.addEventListener\(\s*(['\"])(.*?)\2", text):
        if match.group(1) in variables:
            handlers.append((match.start(), variables[match.group(1)], match.group(3)))
    for match in re.finditer(r"(?:document|window)\.addEventListener\(\s*(['\"])(click|submit|input|change)\1", text):
        tail = text[match.start():match.start() + 1000]
        closest = re.search(r"\.closest\(\s*(['\"])(.*?)\1", tail)
        handlers.append((match.start(), closest.group(2) if closest else None, match.group(2)))
    if forced_selector:
        handlers.append((0, forced_selector, "click"))
    handlers.sort()
    for position, selector, event in handlers:
        line = line_offset + text.count("\n", 0, position) + 1
        records.append({
            "id": _record_id(f"{syntax_prefix}_handler", relative, line, f"{selector}:{event}"),
            "file": relative,
            "line": line,
            "syntax_kind": f"{syntax_prefix}_event_handler",
            "selector": selector,
            "literal_target": None,
            "normalized_target": None,
            "target_kind": "none",
            "method": None,
            "event": event,
            "confidence": "high" if selector else "medium",
        })

    target_patterns = [
        ("location_navigation", re.compile(r"(?:window\.)?location(?:\.href)?\s*=\s*([^;\n]+)"), "navigate", "GET"),
        ("location_navigation", re.compile(r"(?:window\.)?location\.(?:assign|replace)\(\s*([^,\n)]+)"), "navigate", "GET"),
        ("window_open", re.compile(r"window\.open\(\s*([^,\n)]+)"), "popup", "GET"),
        ("fetch", re.compile(r"fetch\(\s*([^,\n)]+)(?:,\s*\{([^}]*)\})?"), "fetch", None),
    ]
    for kind, pattern, event, default_method in target_patterns:
        for match in pattern.finditer(text):
            expression = match.group(1).strip()
            literal_match = re.fullmatch(r"(['\"])(.*?)\1", expression)
            literal = literal_match.group(2) if literal_match else None
            target = _target(literal)
            preceding = [item for item in handlers if item[0] <= match.start()]
            selector = preceding[-1][1] if preceding else forced_selector
            method = default_method
            if kind == "fetch" and len(match.groups()) > 1 and match.group(2):
                method_match = re.search(r"method\s*:\s*(['\"])(.*?)\1", match.group(2), re.IGNORECASE)
                method = method_match.group(2).upper() if method_match else "GET"
            line = line_offset + text.count("\n", 0, match.start()) + 1
            records.append({
                "id": _record_id(f"{syntax_prefix}_{kind}", relative, line, expression),
                "file": relative,
                "line": line,
                "syntax_kind": f"{syntax_prefix}_{kind}",
                "selector": selector,
                **target,
                "expression": expression,
                "method": method,
                "event": event,
                "confidence": "high" if literal is not None else "unresolved",
                "unresolved_reason": None if literal is not None else "dynamic JavaScript action target",
            })
    return records


def _template_relationships(relative: str, text: str) -> tuple[list[dict], list[dict]]:
    relationships = []
    unresolved = []
    for match in re.finditer(r"\{%\s*(include|extends)\s+(.+?)\s*%\}", text):
        expression = match.group(2).strip()
        literal = re.fullmatch(r"(['\"])(.*?)\1", expression)
        line = text.count("\n", 0, match.start()) + 1
        record = {
            "id": _record_id(f"jinja_{match.group(1)}", relative, line, expression),
            "file": relative,
            "line": line,
            "syntax_kind": f"jinja_{match.group(1)}",
            "template": literal.group(2) if literal else None,
            "literal_target": literal.group(2) if literal else None,
            "normalized_target": f"templates/{literal.group(2)}" if literal else None,
            "target_kind": "template" if literal else "dynamic",
            "method": None,
            "event": "render",
            "confidence": "high" if literal else "unresolved",
        }
        relationships.append(record)
        if not literal:
            unresolved.append({**record, "reason": "dynamic Jinja template relationship", "affects_active_surface": True})
    return relationships, unresolved


def _route_worker(site_copy: Path) -> int:
    try:
        os.chdir(site_copy)
        sys.path.insert(0, str(site_copy))
        module_path = next(
            (
                site_copy / name
                for name in ("app.py", "wsgi.py", "application.py", "main.py")
                if (site_copy / name).is_file()
            ),
            None,
        )
        if module_path is None:
            raise RuntimeError("candidate does not expose app.py, wsgi.py, application.py, or main.py")
        spec = __import__("importlib.util").util.spec_from_file_location("webharbor_isolated_candidate", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load app.py")
        module = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = next(
            (
                getattr(module, name, None)
                for name in ("app", "application", "flask_app")
                if hasattr(getattr(module, name, None), "url_map")
            ),
            None,
        )
        factory = next(
            (
                getattr(module, name, None)
                for name in ("create_app", "create_application", "make_app")
                if callable(getattr(module, name, None))
            ),
            None,
        )
        if app is None and factory is not None:
            try:
                app = factory()
            except TypeError:
                # A few conventional factories require a named configuration;
                # use a deterministic testing value without guessing runtime
                # credentials or external services.
                app = factory("testing")
        if app is None or not hasattr(app, "url_map"):
            raise RuntimeError("candidate does not expose a Flask app or supported factory")
        routes = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda item: (item.rule, item.endpoint)):
            methods = sorted(set(rule.methods or set()) - {"HEAD", "OPTIONS"})
            excluded_reason = None
            if rule.endpoint == "static" or rule.rule.startswith("/static/"):
                excluded_reason = "framework_static_route"
            elif rule.rule in INTERNAL_RULES:
                excluded_reason = "internal_health_route"
            routes.append({
                "rule": rule.rule,
                "endpoint": rule.endpoint,
                "methods": methods,
                "public_get": "GET" in methods and excluded_reason is None,
                "excluded_reason": excluded_reason,
            })
        print(json.dumps({"status": "passed", "routes": routes}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI emits a machine-readable failure
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2


def isolated_route_map(
    site_root: Path,
    timeout_seconds: int = 30,
    dependency_path: Path | None = None,
) -> dict[str, Any]:
    source_before = _tree_identity(site_root, _selected_source_paths(site_root))
    seed_before = _tree_identity(site_root, _seed_paths(site_root))
    failures: list[str] = []
    symlinks = [path.relative_to(site_root).as_posix() for path in site_root.rglob("*") if path.is_symlink()]
    if symlinks:
        failures.append("isolated introspection rejects candidate symlinks: " + ", ".join(sorted(symlinks)))
    worker: dict[str, Any] = {"status": "failed", "routes": []}
    copy_source_before = copy_source_after = None
    copy_seed_before = copy_seed_after = None
    if not failures:
        with tempfile.TemporaryDirectory(prefix="webharbor-route-map-") as temporary:
            copy_root = Path(temporary) / "site"
            shutil.copytree(
                site_root,
                copy_root,
                ignore=shutil.ignore_patterns("instance", "__pycache__", ".pytest_cache"),
            )
            runtime = copy_root / "instance"
            runtime.mkdir(exist_ok=True)
            for seed in _seed_paths(copy_root):
                shutil.copy2(seed, runtime / seed.name)
            copy_source_before = _tree_identity(copy_root, _selected_source_paths(copy_root))
            copy_seed_before = _tree_identity(copy_root, _seed_paths(copy_root))
            environment = os.environ.copy()
            environment.update({"PYTHONDONTWRITEBYTECODE": "1", "WEBHARBOR_ROUTE_INTROSPECTION": "1"})
            dependency = dependency_path or (
                Path(os.environ["WEBHARBOR_BUILDER_DEPENDENCY_PATH"])
                if os.environ.get("WEBHARBOR_BUILDER_DEPENDENCY_PATH")
                else None
            )
            if dependency:
                existing = environment.get("PYTHONPATH", "")
                environment["PYTHONPATH"] = str(dependency.resolve()) + (os.pathsep + existing if existing else "")
            try:
                completed = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--route-worker", str(copy_root)],
                    cwd=copy_root,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=max(1, timeout_seconds),
                    check=False,
                )
                lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
                if lines:
                    worker = json.loads(lines[-1])
                else:
                    worker = {"status": "failed", "routes": [], "error": completed.stderr[-2000:] or "route worker emitted no JSON"}
                if completed.returncode != 0 and worker.get("status") != "failed":
                    worker = {"status": "failed", "routes": [], "error": f"route worker exited {completed.returncode}"}
            except subprocess.TimeoutExpired:
                worker = {"status": "failed", "routes": [], "error": f"route worker exceeded {timeout_seconds} seconds"}
            copy_source_after = _tree_identity(copy_root, _selected_source_paths(copy_root))
            copy_seed_after = _tree_identity(copy_root, _seed_paths(copy_root))
            if copy_source_before != copy_source_after:
                failures.append("isolated import mutated copied candidate source")
            if copy_seed_before != copy_seed_after:
                failures.append("isolated import mutated copied canonical seed bytes")
    source_after = _tree_identity(site_root, _selected_source_paths(site_root))
    seed_after = _tree_identity(site_root, _seed_paths(site_root))
    if source_before != source_after:
        failures.append("isolated route introspection mutated original candidate source")
    if seed_before != seed_after:
        failures.append("isolated route introspection mutated original canonical seed bytes")
    if worker.get("status") != "passed":
        failures.append(str(worker.get("error") or "isolated Flask route introspection failed"))
    routes = sorted(worker.get("routes") or [], key=lambda item: (item["rule"], item["endpoint"]))
    for route in routes:
        route["normalized_rule"] = canonical_query(route.get("rule")) or "/"
    return {
        "tool": ROUTE_MAP_TOOL,
        "status": "passed" if not failures else "failed",
        "timeout_seconds": timeout_seconds,
        "copy_strategy": "selected_site_temporary_copy_with_seed_restored_runtime",
        "source_hashes": {"before": source_before, "after": source_after, "unchanged": source_before == source_after},
        "seed_hashes": {"before": seed_before, "after": seed_after, "unchanged": seed_before == seed_after},
        "isolated_copy_hashes": {
            "source_before": copy_source_before,
            "source_after": copy_source_after,
            "seed_before": copy_seed_before,
            "seed_after": copy_seed_after,
            "unchanged": copy_source_before == copy_source_after and copy_seed_before == copy_seed_after,
        },
        "routes": routes,
        "normalization": {
            "algorithm": "webharbor-flask-route-normalization-v1",
            "trailing_slash_equivalent": True,
            "converters": ["string", "int", "float", "path", "uuid", "any"],
        },
        "public_get_routes": [item for item in routes if item["public_get"]],
        "excluded_routes": [item for item in routes if item.get("excluded_reason")],
        "failures": sorted(set(failures)),
    }


def _resolve_endpoint_targets(records: list[dict], route_map: dict) -> None:
    endpoints: dict[str, list[dict]] = {}
    for route in route_map.get("routes") or []:
        endpoints.setdefault(route["endpoint"], []).append(route)
    for record in records:
        endpoint = record.get("endpoint")
        if not endpoint:
            continue
        candidates = endpoints.get(endpoint, [])
        static = [item for item in candidates if "<" not in item["rule"]]
        if len(static) == 1:
            record.update({"normalized_target": static[0]["rule"], "target_kind": "local", "confidence": "high"})


def inventory_site(
    site_root: Path,
    *,
    site: str,
    timeout_seconds: int = 30,
    selected_root: str | None = None,
    dependency_path: Path | None = None,
) -> dict[str, Any]:
    site_root = site_root.resolve()
    closure = source_closure(site_root)
    runtime_documents, runtime_assets = _runtime_bundle_documents(site_root, closure)
    route_map = isolated_route_map(site_root, timeout_seconds, dependency_path)
    files = []
    server_declarations: list[dict] = []
    interactions: list[dict] = []
    controls: list[dict] = []
    relationships: list[dict] = []
    unresolved: list[dict] = []
    python_routes_by_function: dict[str, list[dict]] = {}
    python_templates: list[dict] = []

    for path in sorted(_selected_source_paths(site_root)):
        relative = path.relative_to(site_root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        files.append({
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "language": "python" if path.suffix == ".py" else "javascript" if path.suffix.lower() in SCRIPT_SUFFIXES else "template",
            "closure_status": "candidate_app_code" if path.suffix == ".py" else "candidate_owned_source",
        })
        if path.suffix == ".py":
            try:
                parsed = ast.parse(text, filename=relative)
            except SyntaxError as exc:
                unresolved.append({
                    "id": _record_id("python_parse_error", relative, exc.lineno or 1, str(exc)),
                    "file": relative,
                    "line": exc.lineno or 1,
                    "syntax_kind": "python_parse_error",
                    "literal_target": None,
                    "normalized_target": None,
                    "target_kind": "dynamic",
                    "method": None,
                    "event": "parse",
                    "confidence": "unresolved",
                    "reason": str(exc),
                    "affects_active_surface": True,
                })
                continue
            visitor = PythonInventory(relative, text)
            visitor.visit(parsed)
            server_declarations.extend(visitor.server_declarations)
            interactions.extend(visitor.interactions)
            python_templates.extend(visitor.template_calls)
            unresolved.extend(visitor.unresolved)
            for route in visitor.server_declarations:
                python_routes_by_function.setdefault(route["function"], []).append(route)
        elif path.suffix.lower() in TEMPLATE_SUFFIXES:
            parser = TemplateInventory(relative, text)
            parser.feed(text)
            parser.close()
            controls.extend(parser.controls)
            interactions.extend(parser.interactions)
            unresolved.extend(parser.unresolved)
            found, dynamic = _template_relationships(relative, text)
            relationships.extend(found)
            unresolved.extend(dynamic)
        elif path.suffix.lower() in SCRIPT_SUFFIXES:
            interactions.extend(scan_javascript(text, relative))

    # A replay bundle is an active served page only when the application opens
    # that exact file. Parse its rendered HTML as runtime source evidence, while
    # keeping archived ``external_cache/upstream`` pages excluded above.
    for document in runtime_documents:
        bundle_path = site_root / document["bundle_path"]
        try:
            with gzip.open(bundle_path, "rb") as handle:
                bundle = json.loads(handle.read().decode("utf-8"))
            page = next(
                item for item in bundle.get("pages", [])
                if canonical_query(str(item.get("path") or "/")) == document["route"]
            )
        except (OSError, ValueError, UnicodeDecodeError, StopIteration):
            continue
        relative = document["source_path"]
        parser = TemplateInventory(relative, page.get("body", ""))
        parser.feed(page.get("body", ""))
        parser.close()
        for control in parser.controls:
            control["source_routes"] = [document["route"]]
            control["active_surface"] = True
            target = _target(control.get("literal_target"), document["route"])
            control["normalized_target"] = target.get("normalized_target")
            control["normalized_targets"] = [target["normalized_target"]] if target.get("normalized_target") else []
        controls.extend(parser.controls)
        interactions.extend(parser.interactions)
        for interaction in parser.interactions:
            interaction["source_routes"] = [document["route"]]
            interaction["active_surface"] = True

    for call in python_templates:
        template = call.get("literal_target")
        routes = python_routes_by_function.get(call.get("function") or "", [])
        relationship = {
            **call,
            "template": template,
            "normalized_target": f"templates/{template}" if template else None,
            "source_routes": sorted({route["route_rule"] for route in routes if route.get("route_rule")}),
        }
        relationships.append(relationship)

    template_routes: dict[str, set[str]] = {}
    for relationship in relationships:
        target = relationship.get("normalized_target")
        if relationship.get("syntax_kind") == "python_render_template" and target:
            template_routes.setdefault(target, set()).update(relationship.get("source_routes") or [])
    changed = True
    while changed:
        changed = False
        for relationship in relationships:
            if relationship.get("syntax_kind") not in {"jinja_include", "jinja_extends"}:
                continue
            source = relationship["file"]
            target = relationship.get("normalized_target")
            inherited = template_routes.get(source, set())
            if target and inherited - template_routes.setdefault(target, set()):
                template_routes[target].update(inherited)
                changed = True
    for control in controls:
        routes = sorted(template_routes.get(control["file"], set()))
        control["source_routes"] = routes
        control["active_surface"] = bool(routes) or control["file"].startswith("static/external_cache/replay_bundle.json.gz#")
        normalized_targets = sorted({
            _target(control.get("literal_target"), route).get("normalized_target")
            for route in routes or [None]
            if _target(control.get("literal_target"), route).get("normalized_target")
        })
        if len(normalized_targets) == 1:
            control["normalized_target"] = normalized_targets[0]
        control["normalized_targets"] = normalized_targets
        control["control_identity"] = canonical_control_identity(
            control,
            route=routes[0] if routes else control.get("source_path"),
        )
        if not control["active_surface"]:
            unresolved.append({
                **control,
                "reason": "template control is outside the executable/served source closure",
                "affects_active_surface": False,
            })
        if control.get("selector") is None:
            unresolved.append({
                **control,
                "reason": "active source control has no deterministic selector",
                "affects_active_surface": True,
            })

    # Mark semantic duplicates explicitly.  A selector such as
    # ``a[href="/fr/"]`` is valid provenance but is not by itself unique; the
    # triangulator must reject two controls with the same complete identity.
    identity_counts: dict[tuple[str, ...], int] = {}
    for control in controls:
        if not control.get("active_surface", True):
            continue
        identity = control.get("control_identity") or canonical_control_identity(control)
        key = tuple(str(identity.get(field, "")) for field in (
            "source_route", "control_type", "tag", "accessible_name",
            "normalized_target", "method", "event", "aria_controls", "target",
        ))
        identity_counts[key] = identity_counts.get(key, 0) + 1
    for control in controls:
        identity = control.get("control_identity") or canonical_control_identity(control)
        key = tuple(str(identity.get(field, "")) for field in (
            "source_route", "control_type", "tag", "accessible_name",
            "normalized_target", "method", "event", "aria_controls", "target",
        ))
        control["identity_ambiguous"] = identity_counts.get(key, 0) > 1

    _resolve_endpoint_targets(interactions, route_map)
    dynamic_interactions = [
        {
            **record,
            "reason": record["unresolved_reason"],
            "affects_active_surface": True,
        }
        for record in interactions
        if record.get("unresolved_reason")
    ]
    unresolved.extend(dynamic_interactions)
    failures = list(route_map["failures"])
    for item in closure.get("included", []):
        if item["path"].casefold().endswith(".json.gz"):
            files.append({
                "path": item["path"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
                "language": "runtime_bundle",
                "closure_status": item["reason"],
            })
    source_families = canonical_control_families([item for item in controls if item.get("active_surface", True)])
    inventory = {
        "schema_version": 1,
        "inventory": INVENTORY_SCHEMA,
        "analyzer": ANALYZER_TOOL,
        "analyzer_implementation_sha256": analyzer_implementation_sha256(),
        "status": "complete" if not failures else "failed",
        "site": site,
        "selected_root": selected_root or f"sites/{site}",
        "source_closure": closure,
        "runtime_documents": runtime_documents,
        "runtime_loaded_assets": runtime_assets,
        "source_identity": route_map["source_hashes"]["after"],
        "seed_identity": route_map["seed_hashes"]["after"],
        "files": files,
        "server_declarations": sorted(server_declarations, key=lambda item: (item["file"], item["line"], item["id"])),
        "template_relationships": sorted(relationships, key=lambda item: (item["file"], item["line"], item["id"])),
        "controls": sorted(controls, key=lambda item: (item["file"], item["line"], item["id"])),
        "canonical_control_families": source_families,
        "interactions": sorted(interactions, key=lambda item: (item["file"], item["line"], item["id"])),
        "unresolved": sorted(
            {item["id"]: item for item in unresolved}.values(),
            key=lambda item: (item["file"], item["line"], item["id"]),
        ),
        "actual_route_map": route_map,
        "failures": sorted(set(failures)),
    }
    inventory["payload_sha256"] = hashlib.sha256(canonical_json(inventory)).hexdigest()
    return inventory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--output", type=Path, help="Defaults to mirror_factory/evidence/<site>/code-interaction-inventory.json.")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--route-worker", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--route-worker":
        return _route_worker(Path(sys.argv[2]).resolve())
    args = parse_args()
    if args.route_worker:
        return _route_worker(args.route_worker.resolve())
    workspace = args.workspace.resolve()
    site_root = workspace / "sites" / args.site
    if not site_root.is_dir():
        print(json.dumps({"status": "failed", "error": f"missing site: {site_root}"}, sort_keys=True))
        return 3
    output = args.output.resolve() if args.output else workspace / "mirror_factory" / "evidence" / args.site / "code-interaction-inventory.json"
    inventory = inventory_site(site_root, site=args.site, timeout_seconds=max(1, args.timeout_seconds))
    atomic_json(output, inventory)
    print(json.dumps({
        "status": inventory["status"],
        "output": output.as_posix(),
        "analyzer": ANALYZER_TOOL,
        "controls": len(inventory["controls"]),
        "server_routes": len(inventory["actual_route_map"]["routes"]),
        "unresolved": len(inventory["unresolved"]),
        "failures": len(inventory["failures"]),
        "payload_sha256": inventory["payload_sha256"],
    }, sort_keys=True))
    return 0 if inventory["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
