"""Contract and evidence helpers for immersive interaction closure.

This module deliberately contains no browser automation.  Keeping contract
validation, candidate identity, gateway URL handling, and repair-package
generation pure makes the fail-closed rules easy to test and reuse from the SDK
continuation loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from fingerprint import site_implementation_fingerprint

CONTRACT_SCHEMA = "webharbor-immersive-interaction-contract-v1"
CLOSURE_SCHEMA = "webharbor-standalone-interaction-closure-v2"
REPAIR_SCHEMA = "webharbor-builder-repair-package-v1"
CONTRACT_TOOL = "webharbor-immersive-contract-normalizer-v1"
VERIFIER_TOOL = "webharbor-standalone-interaction-verifier-v1"
CANDIDATE_ALGORITHM = "webharbor-immersive-candidate-v2"
CODE_ANALYZER_TOOL = "webharbor-immersive-code-analyzer-v1"

ARCHETYPES = {
    "landing",
    "listing",
    "detail",
    "search_results",
    "form",
    "reader",
    "download",
    "dashboard",
    "documentation",
    "policy",
    "status",
    "modal_host",
}
CONTROL_TYPES = {
    "link",
    "button",
    "card",
    "menu",
    "disclosure",
    "tab",
    "form",
    "search",
    "filter",
    "pagination",
    "popup",
    "download",
    "state_change",
    "mutation",
    "input",
}
OUTCOME_KINDS = {
    "navigation",
    "popup",
    "state_change",
    "input_state",
    "download",
    "mutation",
}
PROVENANCE_KINDS = {"captured", "source_grounded_synthetic"}
FIELD_NAMES = ("title", "context", "main", "return")
GATEWAY_RE = re.compile(r"^(/p/[0-9]+)(?:/|$)")


class ContractError(RuntimeError):
    """Raised when an immersive declaration cannot be accepted."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_route_path(route: str | None) -> str | None:
    """Normalize a route path for Flask rule matching and repeat observations."""
    if not route:
        return None
    path = urlsplit(route).path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if path != "/":
        path = path.rstrip("/") or "/"
    return path


def canonical_query(route: str | None) -> str | None:
    """Return a deterministic route key while preserving query semantics."""
    if not route:
        return None
    parsed = urlsplit(route)
    path = normalize_route_path(parsed.path) or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return f"{path}?{query}" if query else path


def normalize_accessible_name(value: Any) -> str:
    """Normalize rendered/source accessible names for semantic joins."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def normalize_target_semantics(
    value: Any,
    *,
    source_route: str | None = None,
) -> str:
    """Normalize a literal target while preserving sorted query semantics.

    Browser records may carry a logical target in ``normalized_target`` while
    source records generally carry a literal relative href/action.  Resolving
    both against the source route gives one representation without relying on a
    generated runtime selector.  Dynamic/Jinja values intentionally normalize
    to the empty string and therefore cannot be guessed into a join.
    """
    raw = value
    if isinstance(raw, dict):
        raw = raw.get("normalized_target") or raw.get("href") or raw.get("literal_target")
    raw_text = str(raw or "").strip()
    if not raw_text or any(token in raw_text for token in ("{{", "}}", "{%", "%}")):
        return ""
    parsed = urlsplit(raw_text)
    if parsed.scheme or raw_text.startswith("//"):
        # External targets are still semantic evidence; preserve origin while
        # sorting query parameters (``canonical_query`` is local-route-only).
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, "")) or raw_text
    if source_route and not raw_text.startswith("/"):
        base_path = urlsplit(source_route).path or "/"
        raw_text = urljoin(base_path, raw_text)
    return canonical_query(raw_text) or "/"


def canonical_control_identity(
    control: dict[str, Any],
    *,
    route: str | None = None,
) -> dict[str, Any]:
    """Return the canonical protocol identity shared by all three surfaces.

    ``identity_selector`` is deliberately corroborating provenance only.  The
    semantic fields are sufficient to join a source, contract, and browser
    observation even when browser execution uses an injected audit selector.
    """
    source_route = route or control.get("source_path") or control.get("route") or "/"
    source_route = canonical_query(str(source_route)) or "/"
    control_type = str(control.get("control_type") or control.get("tag") or "control").casefold()
    tag = str(control.get("tag") or "").casefold()
    name = normalize_accessible_name(control.get("accessible_name"))
    target_value = (
        control.get("normalized_target")
        or control.get("href")
        or control.get("action")
        or control.get("literal_target")
    )
    # Contract outcomes are the only target available on old declarations.
    if (
        not target_value
        and control_type in {"link", "card", "popup", "download", "form"}
        and isinstance(control.get("outcome"), dict)
    ):
        target_value = control["outcome"].get("expected_path")
    normalized_target = normalize_target_semantics(target_value, source_route=source_route)
    method = str(control.get("method") or "GET").upper()
    outcome = control.get("outcome") if isinstance(control.get("outcome"), dict) else {}
    event_value = control.get("event") or outcome.get("action")
    if not event_value:
        event_value = {
            "link": "navigate",
            "card": "navigate",
            "popup": "navigate",
            "download": "navigate",
            "form": "submit",
            "button": "click",
            "menu": "click",
            "disclosure": "click",
            "tab": "click",
        }.get(control_type, "input")
    event = str(event_value).casefold()
    aria_controls = str(control.get("aria_controls") or control.get("aria-controls") or "")
    target_window = str(control.get("target") or ("_blank" if control_type == "popup" else "")).casefold()
    identity_selector = str(
        control.get("identity_selector")
        or control.get("selector")
        or ""
    )
    identity_selector = re.sub(
        r"\[data-webharbor-audit-id\s*=\s*[^\]]+\]", "[audit]", identity_selector
    )
    return {
        "source_route": source_route,
        "control_type": control_type,
        "tag": tag,
        "accessible_name": name,
        "normalized_target": normalized_target,
        "method": method,
        "event": event,
        "aria_controls": aria_controls,
        "target": target_window,
        "identity_selector": identity_selector,
    }


def canonical_control_identity_key(
    control: dict[str, Any], *, route: str | None = None
) -> tuple[str, ...]:
    identity = canonical_control_identity(control, route=route)
    # Selector is intentionally excluded from the join key.  It is retained in
    # the identity object as corroborating provenance, while duplicate semantic
    # controls are handled explicitly by triangulation.
    return tuple(
        str(identity[field])
        for field in (
            "source_route",
            "control_type",
            "tag",
            "accessible_name",
            "normalized_target",
            "method",
            "event",
            "aria_controls",
            "target",
        )
    )


def control_identity_compatible(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_route: str | None = None,
    right_route: str | None = None,
) -> bool:
    """Compare identities while preserving legacy card/link declarations.

    Older contracts often classified an anchor nested in a card as ``card``
    while static HTML inventory classified the anchor as ``link``.  The
    semantic target/name/route still bind the control exactly; only this
    historical taxonomy alias is tolerated.
    """
    a = canonical_control_identity(left, route=left_route)
    b = canonical_control_identity(right, route=right_route)
    if a["source_route"] != b["source_route"]:
        left_route_parts = urlsplit(a["source_route"])
        right_route_parts = urlsplit(b["source_route"])
        if left_route_parts.path != right_route_parts.path or (left_route_parts.query and right_route_parts.query):
            return False
    for field in ("accessible_name", "method", "target"):
        if a[field] != b[field]:
            return False
    if a["event"] != b["event"] and {a["event"], b["event"]} not in ({"input", "fill"}, {"click", "change"}, {"input", "click"}):
        return False
    if a["aria_controls"] and b["aria_controls"] and a["aria_controls"] != b["aria_controls"]:
        return False
    if a["normalized_target"] != b["normalized_target"]:
        # A form's action is the path component; submitted query parameters
        # are declared by its preparation steps/expected outcome.  Preserve
        # strict query matching for links and other controls.
        dynamic_left = not a["normalized_target"] and any(token in str(left.get("literal_target") or "") for token in ("{{", "}}", "{%", "%}"))
        dynamic_right = not b["normalized_target"] and any(token in str(right.get("literal_target") or "") for token in ("{{", "}}", "{%", "%}"))
        same_selector = a["identity_selector"] and a["identity_selector"] == b["identity_selector"]
        if not (same_selector or dynamic_left or dynamic_right or ({a["control_type"], b["control_type"]} & {"form", "search"})):
            return False
        if not (dynamic_left or dynamic_right) and urlsplit(a["normalized_target"]).path != urlsplit(b["normalized_target"]).path:
            return False
        if (dynamic_left or dynamic_right) and not same_selector:
            return False
    if a["tag"] and b["tag"] and a["tag"] != b["tag"]:
        return False
    if a["control_type"] == b["control_type"]:
        return True
    return (
        {a["control_type"], b["control_type"]} <= {"link", "card", "popup", "download"}
        or {a["control_type"], b["control_type"]} <= {"button", "state_change"}
        or {a["control_type"], b["control_type"]} <= {"input", "search"}
    )


def semantic_control_key(control: dict[str, Any], *, route: str | None = None) -> tuple[str, ...]:
    """Backward-compatible alias for the canonical semantic identity key."""
    return canonical_control_identity_key(control, route=route)


def canonical_failure_key(failure: dict[str, Any]) -> tuple[str, ...]:
    """Normalize a failure identity while retaining distinct target evidence."""
    evidence = failure.get("evidence") or {}
    target = evidence.get("target") or evidence.get("browser_target") or evidence.get("contract_target") or ""
    selector = re.sub(
        r"\[data-webharbor-audit-id\s*=\s*[^\]]+\]",
        "[audit]",
        str(failure.get("selector") or ""),
    )
    location = failure.get("source_location") or {}
    return (
        str(failure.get("code") or ""),
        canonical_query(str(failure.get("route") or "")) or "",
        selector,
        str(failure.get("control_id") or ""),
        str(location.get("file") or ""),
        str(location.get("line") or ""),
        canonical_query(str(target)) or str(target),
    )


def deduplicate_failures(failures: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse repeat observations without dropping bounded provenance."""
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in failures:
        key = canonical_failure_key(item)
        current = grouped.get(key)
        if current is None:
            current = dict(item)
            current["raw_observation_count"] = 0
            current["raw_provenance"] = []
            grouped[key] = current
        current["raw_observation_count"] += int(item.get("raw_observation_count") or 1)
        current["raw_provenance"].extend(item.get("raw_provenance") or [{
            "route": item.get("route"),
            "selector": item.get("selector"),
            "control_id": item.get("control_id"),
        }])
    result = []
    for item in grouped.values():
        item["raw_provenance"] = sorted(
            {json.dumps(value, sort_keys=True): value for value in item["raw_provenance"]}.values(),
            key=lambda value: json.dumps(value, sort_keys=True),
        )[:64]
        result.append(item)
    result.sort(key=canonical_failure_key)
    return result, len(failures)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analyzer_implementation_sha256() -> str:
    """Hash both modules that derive and join the source interaction surface."""
    digest = hashlib.sha256()
    scripts = Path(__file__).resolve().parent
    for name in ("inventory.py", "triangulation.py"):
        path = scripts / name
        payload = path.read_bytes()
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _valid_route_key(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and "#" not in value


def _validate_provenance(
    value: object,
    label: str,
    failures: list[str],
) -> None:
    if not isinstance(value, dict):
        failures.append(f"{label}.provenance must be an object")
        return
    kind = value.get("kind")
    if kind not in PROVENANCE_KINDS:
        failures.append(
            f"{label}.provenance.kind must be captured or source_grounded_synthetic"
        )
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        failures.append(f"{label}.provenance.sources must be a nonempty list")
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                failures.append(f"{label}.provenance.sources[{index}] must be an object")
                continue
            _require(
                isinstance(source.get("path"), str) and bool(source["path"]),
                f"{label}.provenance.sources[{index}].path is required",
                failures,
            )
            _require(
                isinstance(source.get("sha256"), str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", source["sha256"])),
                f"{label}.provenance.sources[{index}].sha256 must be lowercase SHA-256",
                failures,
            )
    if kind == "source_grounded_synthetic":
        _require(
            isinstance(value.get("synthetic_basis"), str)
            and bool(value["synthetic_basis"].strip()),
            f"{label}.provenance.synthetic_basis is required for synthetic data",
            failures,
        )


def validate_contract(contract: dict) -> list[str]:
    """Return deterministic validation failures for a version-one contract."""
    failures: list[str] = []
    _require(contract.get("schema_version") == 1, "schema_version must be 1", failures)
    _require(contract.get("contract") == CONTRACT_SCHEMA, f"contract must be {CONTRACT_SCHEMA}", failures)
    _require(contract.get("profile") == "immersive", "profile must be immersive", failures)
    _require(isinstance(contract.get("site"), str) and bool(contract.get("site")), "site is required", failures)

    viewport = contract.get("viewport")
    if not isinstance(viewport, dict):
        failures.append("viewport must be an object")
    else:
        for field in ("width", "height"):
            _require(
                isinstance(viewport.get(field), int) and viewport[field] > 0,
                f"viewport.{field} must be a positive integer",
                failures,
            )

    crawl = contract.get("crawl")
    if not isinstance(crawl, dict):
        failures.append("crawl must be an object")
    else:
        for field in ("max_pages", "max_depth"):
            _require(
                isinstance(crawl.get(field), int) and crawl[field] >= (1 if field == "max_pages" else 0),
                f"crawl.{field} has an invalid bound",
                failures,
            )
        starts = crawl.get("start_routes")
        _require(
            isinstance(starts, list)
            and bool(starts)
            and all(_valid_route_key(item) for item in starts),
            "crawl.start_routes must be a nonempty list of root-relative routes",
            failures,
        )
        if isinstance(starts, list):
            _require("/" in starts, "crawl.start_routes must include /", failures)
            if isinstance(crawl.get("max_pages"), int):
                _require(
                    len(set(starts)) <= crawl["max_pages"],
                    "crawl.start_routes exceeds crawl.max_pages",
                    failures,
                )

    namespaces = contract.get("namespaces")
    _require(isinstance(namespaces, list), "namespaces must be a list", failures)
    if isinstance(namespaces, list):
        for index, namespace in enumerate(namespaces):
            label = f"namespaces[{index}]"
            if not isinstance(namespace, dict):
                failures.append(f"{label} must be an object")
                continue
            _require(
                namespace.get("kind") in {"primary", "official_supplemental"},
                f"{label}.kind must be primary or official_supplemental",
                failures,
            )
            _require(
                isinstance(namespace.get("source_origin"), str)
                and urlsplit(namespace["source_origin"]).scheme in {"http", "https"},
                f"{label}.source_origin must be HTTP(S)",
                failures,
            )
            _require(
                _valid_route_key(namespace.get("local_prefix")),
                f"{label}.local_prefix must be root-relative",
                failures,
            )
            _validate_provenance(namespace.get("provenance"), label, failures)

    routes = contract.get("route_inventory")
    _require(isinstance(routes, list) and bool(routes), "route_inventory must be nonempty", failures)
    route_ids: set[str] = set()
    route_paths: set[str] = set()
    if isinstance(routes, list):
        for index, route in enumerate(routes):
            label = f"route_inventory[{index}]"
            if not isinstance(route, dict):
                failures.append(f"{label} must be an object")
                continue
            route_id = route.get("id")
            path = route.get("path")
            _require(isinstance(route_id, str) and bool(route_id), f"{label}.id is required", failures)
            _require(route_id not in route_ids, f"{label}.id must be unique", failures)
            if isinstance(route_id, str):
                route_ids.add(route_id)
            _require(_valid_route_key(path), f"{label}.path must be root-relative", failures)
            _require(path not in route_paths, f"{label}.path must be unique", failures)
            if isinstance(path, str):
                route_paths.add(path)
            _require(
                isinstance(route.get("family"), str) and bool(route["family"]),
                f"{label}.family is required",
                failures,
            )
            _require(route.get("archetype") in ARCHETYPES, f"{label}.archetype is unsupported", failures)
            _require(isinstance(route.get("critical"), bool), f"{label}.critical must be boolean", failures)
            _validate_provenance(route.get("provenance"), label, failures)
            fields = route.get("minimum_fields")
            if not isinstance(fields, dict):
                failures.append(f"{label}.minimum_fields must be an object")
            else:
                for field in FIELD_NAMES:
                    spec = fields.get(field)
                    if not isinstance(spec, dict):
                        failures.append(f"{label}.minimum_fields.{field} must be an object")
                        continue
                    _require(
                        isinstance(spec.get("selector"), str) and bool(spec["selector"]),
                        f"{label}.minimum_fields.{field}.selector is required",
                        failures,
                    )
                    if "min_chars" in spec:
                        _require(
                            isinstance(spec["min_chars"], int) and spec["min_chars"] >= 0,
                            f"{label}.minimum_fields.{field}.min_chars must be nonnegative",
                            failures,
                        )
                    if field == "return":
                        _require(
                            _valid_route_key(spec.get("expected_path")),
                            f"{label}.minimum_fields.return.expected_path is required",
                            failures,
                        )
            alias_of = route.get("alias_of")
            if alias_of is not None:
                _require(
                    isinstance(alias_of, str) and alias_of != path,
                    f"{label}.alias_of must name another route",
                    failures,
                )
                _require(
                    isinstance(route.get("canonical_selector"), str)
                    and bool(route["canonical_selector"]),
                    f"{label}.canonical_selector is required for an alias",
                    failures,
                )

    controls = contract.get("control_inventory")
    _require(isinstance(controls, list) and bool(controls), "control_inventory must be nonempty", failures)
    control_ids: set[str] = set()
    if isinstance(controls, list):
        for index, control in enumerate(controls):
            label = f"control_inventory[{index}]"
            if not isinstance(control, dict):
                failures.append(f"{label} must be an object")
                continue
            control_id = control.get("id")
            _require(isinstance(control_id, str) and bool(control_id), f"{label}.id is required", failures)
            _require(control_id not in control_ids, f"{label}.id must be unique", failures)
            if isinstance(control_id, str):
                control_ids.add(control_id)
            _require(
                _valid_route_key(control.get("source_path")),
                f"{label}.source_path must be root-relative",
                failures,
            )
            _require(
                control.get("source_path") in route_paths,
                f"{label}.source_path must exist in route_inventory",
                failures,
            )
            _require(
                isinstance(control.get("selector"), str) and bool(control["selector"]),
                f"{label}.selector is required",
                failures,
            )
            for identity_field in ("identity_selector", "runtime_selector"):
                if identity_field in control:
                    _require(
                        isinstance(control.get(identity_field), str) and bool(control[identity_field]),
                        f"{label}.{identity_field} must be a nonempty string when supplied",
                        failures,
                    )
            if "control_identity" in control:
                _require(
                    isinstance(control.get("control_identity"), dict),
                    f"{label}.control_identity must be an object when supplied",
                    failures,
                )
            _require(
                isinstance(control.get("accessible_name"), str)
                and bool(control["accessible_name"].strip()),
                f"{label}.accessible_name is required",
                failures,
            )
            _require(control.get("control_type") in CONTROL_TYPES, f"{label}.control_type is unsupported", failures)
            _require(isinstance(control.get("critical"), bool), f"{label}.critical must be boolean", failures)
            reveal = control.get("reveal", [])
            _require(
                isinstance(reveal, list) and all(isinstance(item, str) and item for item in reveal),
                f"{label}.reveal must be a selector list",
                failures,
            )
            outcome = control.get("outcome")
            if not isinstance(outcome, dict):
                failures.append(f"{label}.outcome must be an object")
                continue
            kind = outcome.get("kind")
            _require(kind in OUTCOME_KINDS, f"{label}.outcome.kind is unsupported", failures)
            if kind in {"navigation", "popup"}:
                _require(
                    _valid_route_key(outcome.get("expected_path")),
                    f"{label}.outcome.expected_path is required",
                    failures,
                )
            if kind in {"state_change", "input_state", "mutation"}:
                assertions = outcome.get("assertions")
                _require(
                    isinstance(assertions, list) and bool(assertions),
                    f"{label}.outcome.assertions must be nonempty",
                    failures,
                )
            if kind == "mutation":
                sqlite = outcome.get("sqlite")
                _require(isinstance(sqlite, dict), f"{label}.outcome.sqlite is required", failures)
                _require(
                    isinstance(outcome.get("reset_evidence"), dict),
                    f"{label}.outcome.reset_evidence is required",
                    failures,
                )

    for route in routes if isinstance(routes, list) else []:
        alias_of = route.get("alias_of") if isinstance(route, dict) else None
        if alias_of is not None and alias_of not in route_paths:
            failures.append(f"route {route.get('path')} aliases an undeclared route {alias_of}")

    graph = contract.get("route_graph")
    _require(isinstance(graph, list), "route_graph must be a list", failures)
    if isinstance(graph, list):
        for index, edge in enumerate(graph):
            label = f"route_graph[{index}]"
            if not isinstance(edge, dict):
                failures.append(f"{label} must be an object")
                continue
            _require(edge.get("from") in route_paths, f"{label}.from is undeclared", failures)
            _require(edge.get("to") in route_paths, f"{label}.to is undeclared", failures)
            _require(edge.get("control_id") in control_ids, f"{label}.control_id is undeclared", failures)

    workflows = contract.get("critical_workflows")
    _require(
        isinstance(workflows, list) and bool(workflows),
        "critical_workflows must be nonempty",
        failures,
    )
    workflow_ids: set[str] = set()
    if isinstance(workflows, list):
        for index, workflow in enumerate(workflows):
            label = f"critical_workflows[{index}]"
            if not isinstance(workflow, dict):
                failures.append(f"{label} must be an object")
                continue
            workflow_id = workflow.get("id")
            _require(isinstance(workflow_id, str) and bool(workflow_id), f"{label}.id is required", failures)
            _require(workflow_id not in workflow_ids, f"{label}.id must be unique", failures)
            if isinstance(workflow_id, str):
                workflow_ids.add(workflow_id)
            route_refs = workflow.get("routes")
            control_refs = workflow.get("controls")
            _require(
                isinstance(route_refs, list)
                and bool(route_refs)
                and all(route in route_paths for route in route_refs),
                f"{label}.routes must name declared routes",
                failures,
            )
            _require(
                isinstance(control_refs, list)
                and bool(control_refs)
                and all(control in control_ids for control in control_refs),
                f"{label}.controls must name declared controls",
                failures,
            )

    return sorted(set(failures))


def load_contract(path: Path, expected_site: str | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"invalid interaction contract {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("interaction contract root must be an object")
    # The public WebAnything compiler emits a schema-v1 Site Contract and, for
    # verifier interoperability, embeds the richer interaction-contract view.
    # Adapt that view locally before applying the hardened verifier validator;
    # no routes or controls are inferred here.
    if value.get("schema") == "webanything-site-contract-v1":
        value = dict(value)
        value.setdefault("schema_version", 1)
        value.setdefault("contract", CONTRACT_SCHEMA)
        value["profile"] = "immersive"
    failures = validate_contract(value)
    if expected_site and value.get("site") != expected_site:
        failures.append(
            f"contract site {value.get('site')!r} does not match --site {expected_site!r}"
        )
    if failures:
        raise ContractError("; ".join(sorted(set(failures))))
    return value


def provenance_file_failures(contract: dict, workspace: Path) -> list[str]:
    """Verify every declared provenance file against its workspace SHA-256."""
    failures: list[str] = []
    declarations = [
        *(contract.get("namespaces") or []),
        *(contract.get("route_inventory") or []),
        *(contract.get("synthetic_records") or []),
    ]
    checked: set[tuple[str, str]] = set()
    root = workspace.resolve()
    for declaration in declarations:
        provenance = declaration.get("provenance") if isinstance(declaration, dict) else None
        for source in (provenance or {}).get("sources", []):
            identity = (source["path"], source["sha256"])
            if identity in checked:
                continue
            checked.add(identity)
            candidate = (root / source["path"]).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                failures.append(f"provenance path escapes workspace: {source['path']}")
                continue
            actual = sha256_file(candidate)
            if actual != source["sha256"]:
                failures.append(
                    f"provenance hash mismatch for {source['path']}: "
                    f"expected {source['sha256']}, got {actual}"
                )
    return sorted(failures)


@dataclass(frozen=True)
class LocalSurface:
    """Map logical mirror routes to direct or ``/p/<port>/`` gateway URLs."""

    origin: str
    root_url: str
    gateway_prefix: str

    @classmethod
    def from_base_url(
        cls,
        base_url: str,
        gateway_prefix: str | None = None,
    ) -> LocalSurface:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ContractError("base URL must be an absolute HTTP(S) URL")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"
        inferred = GATEWAY_RE.match(path)
        prefix = gateway_prefix if gateway_prefix is not None else (
            inferred.group(1) if inferred else ""
        )
        if prefix:
            if not re.fullmatch(r"/p/[0-9]+", prefix.rstrip("/")):
                raise ContractError("gateway prefix must have the form /p/<port>/")
            prefix = prefix.rstrip("/")
        root_url = origin + prefix + "/"
        return cls(origin=origin, root_url=root_url, gateway_prefix=prefix)

    def to_url(self, logical_path: str) -> str:
        if not _valid_route_key(logical_path):
            raise ContractError(f"logical route is not root-relative: {logical_path!r}")
        parts = urlsplit(logical_path)
        path = self.gateway_prefix + (parts.path or "/")
        return urlunsplit((urlsplit(self.origin).scheme, urlsplit(self.origin).netloc, path, parts.query, ""))

    def logical_path(self, absolute_url: str) -> str | None:
        parsed = urlsplit(absolute_url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            return None
        path = parsed.path or "/"
        if self.gateway_prefix:
            if path == self.gateway_prefix:
                path = "/"
            elif path.startswith(self.gateway_prefix + "/"):
                path = path[len(self.gateway_prefix):]
            else:
                return None
        logical = path or "/"
        return f"{logical}?{parsed.query}" if parsed.query else logical

    def resolve(self, current_url: str, raw: str) -> tuple[str, str | None]:
        absolute = urljoin(current_url, raw)
        parsed = urlsplit(absolute)
        absolute = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        return absolute, self.logical_path(absolute)

    def request_allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return parsed.scheme in {"data", "blob", "about"}
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            return False
        return self.logical_path(url) is not None


def candidate_fingerprint(
    site_root: Path,
    source_contract: dict,
    *,
    code_inventory_sha256: str,
    interaction_diff_sha256: str,
    analyzer: str,
) -> dict:
    implementation = site_implementation_fingerprint(site_root)
    contract_sha256 = sha256_bytes(canonical_json(source_contract))
    identity = {
        "site_implementation": implementation,
        "source_contract_sha256": contract_sha256,
        "code_inventory_sha256": code_inventory_sha256,
        "interaction_diff_sha256": interaction_diff_sha256,
        "analyzer": analyzer,
        "analyzer_implementation_sha256": analyzer_implementation_sha256(),
        "verifier": VERIFIER_TOOL,
    }
    return {
        "algorithm": CANDIDATE_ALGORITHM,
        "sha256": sha256_bytes(canonical_json(identity)),
        **identity,
    }


def contract_artifact(
    source_contract: dict,
    source_path: Path,
    fingerprint: dict,
) -> dict:
    return {
        **source_contract,
        "tool": CONTRACT_TOOL,
        "status": "declared",
        "source_contract": {
            "path": source_path.as_posix(),
            "sha256": sha256_bytes(canonical_json(source_contract)),
        },
        "candidate_fingerprint": fingerprint,
    }


def derive_site_contract_surface(
    source_contract: dict[str, Any],
    *,
    route_results: list[dict[str, Any]],
    control_families: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a run-local coverage view for every route/control family.

    This is evidence, not an automatic declaration of successful behavior.
    Critical workflows remain separately gated by their exact route/control IDs.
    """
    declared_routes = {
        canonical_query(item.get("path")) or "/": item
        for item in source_contract.get("route_inventory", [])
    }
    observed_routes = {
        canonical_query(item.get("requested_path")) or "/": item
        for item in route_results
    }
    routes = []
    for path in sorted(set(declared_routes) | set(observed_routes)):
        declared = declared_routes.get(path) or {}
        observed = observed_routes.get(path) or {}
        routes.append({
            "path": path,
            "family": declared.get("family", "observed-undeclared"),
            "archetype": declared.get("archetype", observed.get("archetype", "undeclared")),
            "declared": bool(declared),
            "observed": bool(observed),
            "passed": bool(observed.get("passed")) if observed else False,
        })
    families = []
    for family in control_families:
        representative = family.get("representative") or {}
        families.append({
            "family_id": family.get("family_id"),
            "route": family.get("route"),
            "control_type": family.get("control_type"),
            "accessible_name": family.get("accessible_name"),
            "raw_observation_count": family.get("raw_observation_count", 0),
            "unique_targets": family.get("unique_targets", []),
            "declared_control_ids": sorted({
                control.get("id")
                for control in source_contract.get("control_inventory", [])
                if control.get("selector") == representative.get("selector")
                and canonical_query(control.get("source_path")) == canonical_query(family.get("route"))
            } - {None}),
        })
    return {
        "algorithm": "webharbor-run-local-site-contract-derivation-v1",
        "route_archetypes": routes,
        "control_families": sorted(families, key=lambda item: item.get("family_id") or ""),
        "critical_workflow_ids": [item.get("id") for item in source_contract.get("critical_workflows", [])],
        "coverage": {
            "route_family_count": len({item["family"] for item in routes}),
            "route_count": len(routes),
            "control_family_count": len(families),
            "declared_control_family_count": sum(bool(item["declared_control_ids"]) for item in families),
        },
    }


def derive_browser_control_contracts(
    source_contract: dict[str, Any],
    canonical_control_families: list[dict[str, Any]],
    observed_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create a run-local contract for every canonical browser family.

    The checked-in Site Contract remains the source-owned declaration.  This
    derived view is deliberately stricter: it gives triangulation a stable
    identity for every browser-observed family, but a derived control is not
    considered proved unless an exact source declaration already supplied a
    browser proof.  Unproved families therefore remain hard repair evidence.
    """
    declared = source_contract.get("control_inventory") or []
    derived: list[dict[str, Any]] = []
    for family in canonical_control_families:
        representative = family.get("representative") or family
        route = canonical_query(
            representative.get("source_path")
            or family.get("route")
            or (representative.get("source_routes") or ["/"])[0]
        ) or "/"
        selector = representative.get("selector")
        if not selector:
            audit_id = representative.get("audit_id")
            if audit_id:
                selector = f"[data-webharbor-audit-id='{audit_id}']"
        if not selector:
            # Browser discovery normally always supplies a stable selector.
            # Keep an unresolvable family in evidence rather than silently
            # dropping it from the derived surface.
            selector = "[data-webharbor-derived-unresolvable='true']"
        accessible_name = str(representative.get("accessible_name") or "")
        source_basis_ids = sorted({
            str(control["id"])
            for control in declared
            if canonical_query(control.get("source_path")) == route
            and control.get("selector") == selector
        })
        edge = next(
            (
                item for item in observed_edges
                if canonical_query(item.get("from")) == route
                and item.get("selector") == selector
            ),
            None,
        )
        target = edge.get("to") if edge else None
        if edge and target and not str(target).startswith("#"):
            if edge.get("kind") == "popup":
                outcome: dict[str, Any] = {"kind": "popup", "expected_path": target}
            elif edge.get("kind") == "download":
                outcome = {"kind": "download"}
            else:
                outcome = {"kind": "navigation", "expected_path": target}
        else:
            expanded = representative.get("aria_expanded")
            if expanded is not None:
                expected = "false" if str(expanded).casefold() == "true" else "true"
                assertion = {
                    "kind": "attribute",
                    "selector": selector,
                    "name": "aria-expanded",
                    "value": expected,
                }
            else:
                # There is no source-grounded state assertion for this family.
                # The sentinel is intentionally impossible and keeps the
                # family fail-closed until a later Builder proof supplies one.
                assertion = {
                    "kind": "attribute",
                    "selector": selector,
                    "name": "data-webharbor-derived-proof",
                    "value": "true",
                }
            outcome = {"kind": "state_change", "assertions": [assertion]}
        derived.append({
            "id": f"derived-{family.get('family_id') or 'unknown'}",
            "source_path": route,
            "selector": selector,
            "accessible_name": accessible_name,
            "control_type": representative.get("control_type") or "state_change",
            "critical": False,
            "reveal": list(representative.get("revealed_by") or []),
            "outcome": outcome,
            "derived": True,
            "family_id": family.get("family_id"),
            "raw_observation_count": family.get("raw_observation_count", 0),
            "raw_provenance": family.get("raw_provenance") or [],
            "source_basis_ids": source_basis_ids,
        })
    return sorted(derived, key=lambda item: item["id"])


def closure_is_current(
    closure: dict | None,
    site_root: Path,
    source_contract: dict,
) -> bool:
    if not isinstance(closure, dict):
        return False
    fingerprint = closure.get("candidate_fingerprint") or {}
    current_implementation = site_implementation_fingerprint(site_root)
    return (
        closure.get("closure") == CLOSURE_SCHEMA
        and closure.get("tool") == VERIFIER_TOOL
        and closure.get("status") == "passed"
        and fingerprint.get("algorithm") == CANDIDATE_ALGORITHM
        and fingerprint.get("site_implementation") == current_implementation
        and fingerprint.get("source_contract_sha256")
        == sha256_bytes(canonical_json(source_contract))
        and fingerprint.get("code_inventory_sha256")
        == (closure.get("code_inventory") or {}).get("payload_sha256")
        and fingerprint.get("interaction_diff_sha256")
        == (closure.get("interaction_diff") or {}).get("payload_sha256")
        and fingerprint.get("analyzer")
        == (closure.get("code_inventory") or {}).get("analyzer")
        and fingerprint.get("analyzer") == CODE_ANALYZER_TOOL
        and fingerprint.get("analyzer_implementation_sha256")
        == analyzer_implementation_sha256()
        and fingerprint.get("verifier") == VERIFIER_TOOL
    )


def build_repair_package(
    *,
    site: str,
    fingerprint: dict,
    failures: list[dict],
) -> dict:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for failure in failures:
        key = (
            str(failure.get("route_family") or "unknown"),
            str(failure.get("page_archetype") or "unknown"),
            str(failure.get("control_type") or "route"),
        )
        groups.setdefault(key, []).append(failure)
    rendered = []
    for (route_family, archetype, control_type), items in sorted(groups.items()):
        rendered.append({
            "route_family": route_family,
            "page_archetype": archetype,
            "control_type": control_type,
            "failure_codes": sorted({str(item.get("code")) for item in items}),
            "items": sorted(
                items,
                key=lambda item: (
                    str(item.get("route") or ""),
                    str((item.get("source_location") or {}).get("file") or ""),
                    int((item.get("source_location") or {}).get("line") or 0),
                    str(item.get("control_id") or ""),
                    str(item.get("selector") or ""),
                    str(item.get("code") or ""),
                ),
            ),
        })
    return {
        "schema_version": 1,
        "repair_package": REPAIR_SCHEMA,
        "site": site,
        "status": "repair_required" if failures else "empty",
        "candidate_fingerprint": fingerprint,
        "failure_count": len(failures),
        "group_count": len(rendered),
        "groups": rendered,
    }
