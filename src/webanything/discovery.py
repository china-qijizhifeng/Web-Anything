"""Emit a deterministic discovery package from bounded upstream evidence.

This module never discovers requirements from the generated Flask app. Its
only semantic inputs are the capture manifest, the hash-pinned raw upstream
HTML, and the reconstruction contract produced from that capture. Candidate
code is therefore not allowed to define its own verification contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .contracts import compile_contract


class DiscoveryError(ValueError):
    """Capture evidence cannot support a bounded discovery package."""


def canonical_origin(entry_url: str) -> str:
    parsed = urlsplit(entry_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DiscoveryError(f"entry URL must be absolute HTTP(S): {entry_url}")
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = parsed.hostname.casefold()
    if parsed.port is not None and parsed.port != default_port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, "", "", ""))


def canonical_url(entry_url: str, raw: str) -> str:
    """Resolve a same-host URL and pin it to the entry URL's origin."""
    absolute = urljoin(entry_url, raw)
    parsed = urlsplit(absolute)
    expected = urlsplit(entry_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DiscoveryError(f"captured URL is not HTTP(S): {raw}")
    if (
        not expected.hostname
        or parsed.hostname.casefold().removeprefix("www.")
        != expected.hostname.casefold().removeprefix("www.")
    ):
        raise DiscoveryError(f"captured URL is outside the canonical host: {absolute}")
    parsed_default = 443 if parsed.scheme == "https" else 80
    expected_default = 443 if expected.scheme == "https" else 80
    if expected.port is not None:
        if parsed.port not in {None, expected.port}:
            raise DiscoveryError(f"captured URL uses an out-of-scope port: {absolute}")
    elif parsed.port not in {None, parsed_default}:
        raise DiscoveryError(f"captured URL uses an out-of-scope port: {absolute}")
    netloc = expected.hostname.casefold()
    if expected.port is not None and expected.port != expected_default:
        netloc = f"{netloc}:{expected.port}"
    return urlunsplit((expected.scheme.casefold(), netloc, parsed.path or "/", parsed.query, ""))


def _route(entry_url: str, raw: str) -> str:
    parsed = urlsplit(canonical_url(entry_url, raw))
    return (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        if required:
            raise DiscoveryError(f"invalid required JSON evidence {path}: {exc}") from exc
        return None
    if not isinstance(value, dict):
        if required:
            raise DiscoveryError(f"required JSON evidence must be an object: {path}")
        return None
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _css_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class _PageParser(HTMLParser):
    """Small structural extractor; it does not execute candidate JavaScript."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._title_depth = 0
        self._open_controls: list[dict[str, Any]] = []
        self.controls: list[dict[str, Any]] = []
        self.tags: Counter[str] = Counter()
        self.classes: Counter[str] = Counter()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        self.tags[tag] += 1
        for token in values.get("class", "").split():
            self.classes[token] += 1
        if tag == "title":
            self._title_depth += 1
        if tag in {"a", "button", "form"}:
            target_key = "href" if tag == "a" else "action" if tag == "form" else ""
            self._open_controls.append({
                "tag": tag,
                "attrs": values,
                "raw_target": values.get(target_key),
                "line": self.getpos()[0],
                "text": [],
            })
        elif tag == "input":
            self.controls.append({
                "tag": tag,
                "attrs": values,
                "raw_target": None,
                "line": self.getpos()[0],
                "text": values.get("aria-label") or values.get("placeholder") or values.get("value") or values.get("name") or "",
            })

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self.title_parts.append(data)
        for control in self._open_controls:
            control["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        for index in range(len(self._open_controls) - 1, -1, -1):
            control = self._open_controls[index]
            if control["tag"] != tag:
                continue
            self._open_controls.pop(index)
            control["text"] = re.sub(r"\s+", " ", "".join(control["text"])).strip()
            self.controls.append(control)
            break

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()


def _provenance(workspace: Path, path: Path, expected_sha256: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise DiscoveryError(f"missing captured evidence file: {path}")
    actual = _sha256(path)
    if expected_sha256 and actual != expected_sha256:
        raise DiscoveryError(
            f"captured evidence hash mismatch for {path}: expected {expected_sha256}, got {actual}"
        )
    return {
        "kind": "captured",
        "sources": [{"path": path.relative_to(workspace).as_posix(), "sha256": actual}],
    }


def _page_archetype(parser: _PageParser, route: str) -> tuple[str, str]:
    if parser.tags["main"]:
        return ("landing" if route == "/" else "detail", "main")
    for token, archetype in (
        ("search-results", "search_results"),
        ("results", "listing"),
        ("quote", "listing"),
        ("author-details", "detail"),
        ("article", "reader"),
        ("content", "detail"),
    ):
        if parser.classes[token]:
            return ("landing" if route == "/" else archetype, f".{token}")
    return ("landing" if route == "/" else "detail", "body")


def _selector_for_control(control: dict[str, Any]) -> str | None:
    tag = control["tag"]
    attrs = control["attrs"]
    identifier = attrs.get("id")
    if identifier and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", identifier):
        return f"#{identifier}"
    if tag == "a" and attrs.get("href"):
        return f'a[href="{_css_string(attrs["href"])}"]'
    if tag == "form" and attrs.get("action"):
        return f'form[action="{_css_string(attrs["action"])}"]'
    name = attrs.get("name")
    if name:
        return f'{tag}[name="{_css_string(name)}"]'
    return None


def _control_label(control: dict[str, Any]) -> str:
    attrs = control["attrs"]
    return re.sub(
        r"\s+",
        " ",
        str(control.get("text") or attrs.get("aria-label") or attrs.get("title") or ""),
    ).strip()


def _limitation(code: str, message: str, **evidence: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"code": code, "message": message}
    if evidence:
        value["evidence"] = evidence
    return value


def emit_contract_artifacts(
    workspace: Path,
    *,
    site: str,
    entry_url: str,
    profile: str = "task-driven",
    viewport_width: int = 1440,
    viewport_height: int = 1000,
    discovery_output: Path | None = None,
    contract_output: Path | None = None,
) -> dict[str, Any]:
    """Emit a discovery package and compiled Site Contract into stable paths."""
    workspace = workspace.resolve()
    site_path = workspace / "sites" / site
    evidence_dir = workspace / "mirror_factory" / "evidence" / site
    upstream_path = evidence_dir / "upstream.json"
    mirror_path = site_path / "mirror_contract.json"
    if not site_path.is_dir():
        raise DiscoveryError(f"missing reconstructed site: {site_path}")
    upstream = _read_json(upstream_path, required=True) or {}
    mirror = _read_json(mirror_path, required=True) or {}
    scope = upstream.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("upstream_url"), str):
        raise DiscoveryError("upstream evidence has no canonical scope.upstream_url")
    canonical_entry = canonical_url(entry_url, scope["upstream_url"])
    if canonical_entry != canonical_url(entry_url, entry_url):
        raise DiscoveryError("requested entry URL does not match captured canonical entry")
    upstream_provenance = _provenance(workspace, upstream_path)

    raw_root = site_path / "static" / "external_cache" / "upstream"
    parsed_pages: list[dict[str, Any]] = []
    parsers: dict[str, _PageParser] = {}
    route_to_page: dict[str, dict[str, Any]] = {}
    limitations: list[dict[str, Any]] = []
    page_records = upstream.get("pages")
    if not isinstance(page_records, list) or not page_records:
        raise DiscoveryError("upstream evidence contains no captured pages")
    for index, record in enumerate(page_records):
        if not isinstance(record, dict):
            raise DiscoveryError(f"upstream page {index} is not an object")
        raw_url = record.get("final_url") or record.get("url")
        local_path = record.get("local_path")
        if not isinstance(raw_url, str) or not isinstance(local_path, str):
            raise DiscoveryError(f"upstream page {index} lacks URL or local_path")
        evidence_path = (raw_root / local_path).resolve()
        try:
            evidence_path.relative_to(raw_root.resolve())
        except ValueError as exc:
            raise DiscoveryError(f"captured page path escapes evidence root: {local_path}") from exc
        provenance = _provenance(workspace, evidence_path, record.get("sha256"))
        parser = _PageParser()
        parser.feed(evidence_path.read_text(encoding="utf-8", errors="strict"))
        route = _route(entry_url, raw_url)
        if route in route_to_page:
            continue
        archetype, main_selector = _page_archetype(parser, route)
        page = {
            "id": f"route-{len(parsed_pages):04d}",
            "route": route,
            "url": canonical_url(entry_url, raw_url),
            "title": parser.title,
            "archetype": archetype,
            "family": archetype,
            "critical": route == "/",
            "minimum_fields": {
                "title": {"selector": "h1", "min_chars": 1},
                "context": {"selector": "body", "min_chars": 1},
                "main": {"selector": main_selector, "min_chars": 1},
            },
            "provenance": provenance,
            "capture": {
                "requested_url": canonical_url(entry_url, str(record.get("url") or raw_url)),
                "final_url": canonical_url(entry_url, raw_url),
                "status": record.get("status"),
            },
        }
        parsed_pages.append(page)
        parsers[route] = parser
        route_to_page[route] = page

        requested_route = _route(entry_url, str(record.get("url") or raw_url))
        if requested_route != route and requested_route not in route_to_page:
            limitations.append(_limitation(
                "captured_redirect_alias",
                "capture observed a request/final-route alias without DOM canonical-link proof",
                requested_route=requested_route,
                final_route=route,
            ))

    if "/" not in route_to_page:
        raise DiscoveryError("capture evidence cannot support canonical route /")
    captured_routes = set(route_to_page)

    discovered_controls: list[dict[str, Any]] = []
    critical_workflows: list[dict[str, Any]] = []
    for source_route, parser in sorted(parsers.items()):
        candidates: list[dict[str, Any]] = []
        for raw_control in parser.controls:
            selector = _selector_for_control(raw_control)
            label = _control_label(raw_control)
            raw_target = raw_control.get("raw_target")
            if not selector or not label:
                limitations.append(_limitation(
                    "uncontracted_control",
                    "captured control lacks a deterministic selector or accessible name",
                    route=source_route,
                    line=raw_control.get("line"),
                    tag=raw_control.get("tag"),
                ))
                continue
            target_route = None
            if isinstance(raw_target, str) and raw_target:
                try:
                    target_route = _route(entry_url, urljoin(canonical_url(entry_url, source_route), raw_target))
                except DiscoveryError:
                    limitations.append(_limitation(
                        "external_control_target",
                        "captured control targets a different origin",
                        route=source_route,
                        line=raw_control.get("line"),
                        target=raw_target,
                    ))
                    continue
            candidates.append({
                "raw": raw_control,
                "selector": selector,
                "label": label,
                "target_route": target_route,
            })
        selector_counts = Counter(item["selector"] for item in candidates)
        seen_semantics: set[tuple[str, str, str]] = set()
        for item in candidates:
            raw_control = item["raw"]
            target_route = item["target_route"]
            semantics = (
                source_route,
                item["selector"],
                item["label"].casefold(),
                str(target_route or ""),
            )
            if semantics in seen_semantics:
                continue
            seen_semantics.add(semantics)
            selector_unique = selector_counts[item["selector"]] == 1
            target_captured = isinstance(target_route, str) and target_route in captured_routes
            required = bool(selector_unique and target_captured)
            if target_route and not target_captured:
                limitations.append(_limitation(
                    "uncaptured_control_target",
                    "captured same-site control points outside the bounded page capture",
                    route=source_route,
                    target=target_route,
                    selector=item["selector"],
                ))
            if not selector_unique:
                limitations.append(_limitation(
                    "non_unique_control_selector",
                    "captured controls share the same evidence-derived selector",
                    route=source_route,
                    selector=item["selector"],
                    count=selector_counts[item["selector"]],
                ))
            digest = hashlib.sha256(
                json.dumps(semantics, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:16]
            control_id = f"control-{digest}"
            evidence_source = route_to_page[source_route]["provenance"]["sources"][0]
            control = {
                "id": control_id,
                "route": source_route,
                "type": "link" if raw_control["tag"] == "a" else "form" if raw_control["tag"] == "form" else "button",
                "label": item["label"],
                "selector": item["selector"],
                "identity_selector": item["selector"],
                "runtime_selector": item["selector"],
                "target": raw_control.get("raw_target"),
                "expected_path": target_route,
                "outcome": {"kind": "navigation", "expected_path": target_route} if target_route else None,
                "required": required,
                "critical": required and source_route == "/",
                "evidence": {**evidence_source, "line": raw_control.get("line")},
            }
            discovered_controls.append(control)
            if required:
                critical_workflows.append({
                    "id": f"workflow-{control_id}",
                    "routes": list(dict.fromkeys([source_route, target_route])),
                    "controls": [control_id],
                    "provenance": {"kind": "captured", "sources": [evidence_source]},
                })

    required_by_route: dict[str, list[dict[str, Any]]] = {}
    for control in discovered_controls:
        if control["required"] and control.get("expected_path"):
            required_by_route.setdefault(control["route"], []).append(control)
    for page in parsed_pages:
        choices = required_by_route.get(page["route"], [])
        preferred = next((item for item in choices if item["expected_path"] == "/"), None)
        selected = preferred or (choices[0] if choices else None)
        if selected is None and page.get("alias_of"):
            choices = required_by_route.get(page["alias_of"], [])
            preferred = next((item for item in choices if item["expected_path"] == "/"), None)
            selected = preferred or (choices[0] if choices else None)
        if selected is None:
            raise DiscoveryError(f"captured route has no source-grounded return control: {page['route']}")
        page["minimum_fields"]["return"] = {
            "selector": selected["selector"],
            "expected_path": selected["expected_path"],
            "min_chars": 1,
        }

    if not critical_workflows:
        raise DiscoveryError("capture evidence contains no verifiable local interaction workflow")

    resources: list[dict[str, Any]] = []
    for record in upstream.get("assets") or []:
        if not isinstance(record, dict) or not isinstance(record.get("local_path"), str):
            continue
        path = (raw_root / record["local_path"]).resolve()
        try:
            path.relative_to(raw_root.resolve())
        except ValueError as exc:
            raise DiscoveryError(f"captured asset path escapes evidence root: {record['local_path']}") from exc
        if not path.is_file():
            limitations.append(_limitation("missing_captured_resource", "capture manifest resource is absent", path=record["local_path"]))
            continue
        actual = _sha256(path)
        if record.get("sha256") and actual != record["sha256"]:
            raise DiscoveryError(f"captured asset hash mismatch: {record['local_path']}")
        resources.append({
            "path": path.relative_to(workspace).as_posix(),
            "sha256": actual,
            "content_type": record.get("content_type"),
            "source_url": record.get("final_url") or record.get("url"),
        })

    for field in ("failures", "asset_failures"):
        for item in upstream.get(field) or []:
            if isinstance(item, dict):
                limitations.append(_limitation(
                    f"upstream_{field.rstrip('s')}",
                    str(item.get("error") or "upstream capture failure"),
                    kind=item.get("kind"),
                    url=item.get("url"),
                ))
    interaction = mirror.get("interaction") if isinstance(mirror.get("interaction"), dict) else {}
    for kind, values in (
        ("blocking_reconstruction_gap", interaction.get("blocking_gaps") or []),
        ("disclosed_reconstruction_gap", interaction.get("disclosed_nonblocking_gaps") or []),
    ):
        for message in values:
            if isinstance(message, str) and message:
                limitations.append(_limitation(kind, message))

    existing = _read_json(evidence_dir / "discovery-package.json")
    captured_at = upstream.get("generated_at")
    if not isinstance(captured_at, str) or not captured_at:
        captured_at = existing.get("captured_at") if existing else None
    if not isinstance(captured_at, str) or not captured_at:
        raise DiscoveryError("capture evidence has no stable generated_at timestamp")
    package = {
        "schema": "webanything-discovery-package-v1",
        "source": {"entry_url": canonical_entry, "allowed_origins": [canonical_origin(canonical_entry)]},
        "captured_at": captured_at,
        "provenance": upstream_provenance,
        "pages": parsed_pages,
        "workflows": critical_workflows,
        "controls": discovered_controls,
        "resources": resources,
        "limitations": sorted(
            {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in limitations}.values(),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        ),
        "private_session_material_included": False,
    }
    package_path = evidence_dir / "discovery-package.json"
    _atomic_json(package_path, package)
    contract = compile_contract(
        package,
        site=site,
        profile=profile,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        source_path=package_path,
    )
    contract_path = site_path / "site-contract.json"
    _atomic_json(contract_path, contract)
    published_discovery = package_path
    published_contract = contract_path
    if discovery_output is not None:
        published_discovery = discovery_output.expanduser().resolve()
        if published_discovery != package_path:
            _atomic_json(published_discovery, package)
    if contract_output is not None:
        published_contract = contract_output.expanduser().resolve()
        if published_contract != contract_path:
            _atomic_json(published_contract, contract)
    return {
        "schema": "webanything-build-artifacts-v1",
        "discovery_package": {"path": published_discovery.as_posix(), "sha256": _sha256(published_discovery), "bytes": published_discovery.stat().st_size},
        "site_contract": {"path": published_contract.as_posix(), "sha256": _sha256(published_contract), "bytes": published_contract.stat().st_size},
        "workspace_discovery_package": {"path": package_path.as_posix(), "sha256": _sha256(package_path)},
        "workspace_site_contract": {"path": contract_path.as_posix(), "sha256": _sha256(contract_path)},
        "routes": len(contract.get("routes") or []),
        "controls": len(contract.get("control_inventory") or []),
        "workflows": len(contract.get("critical_workflows") or []),
    }
