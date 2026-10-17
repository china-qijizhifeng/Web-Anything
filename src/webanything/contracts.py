"""Deterministic discovery-package to Site-Contract compiler."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ContractCompileError(ValueError):
    """Raised when discovery evidence is insufficient or malformed."""


ARCHETYPES = {
    "landing", "listing", "detail", "search_results", "form", "reader",
    "download", "dashboard", "documentation", "policy", "status", "modal_host",
}
CONTROL_TYPES = {
    "link", "button", "card", "menu", "disclosure", "tab", "form", "search",
    "filter", "pagination", "popup", "download", "state_change", "mutation", "input",
}
OUTCOME_KINDS = {"navigation", "popup", "state_change", "input_state", "download", "mutation"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_route(record: dict[str, Any]) -> str:
    raw = record.get("route") or record.get("canonical_url") or record.get("final_url") or record.get("url")
    if not isinstance(raw, str) or not raw.strip():
        raise ContractCompileError("each discovered page must declare route or URL")
    if raw.startswith("/"):
        parsed = urlsplit(raw)
    else:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ContractCompileError(f"invalid discovered page URL: {raw}")
    if parsed.fragment:
        raise ContractCompileError(f"route fragments are not contract routes: {raw}")
    return (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")


def source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_record(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ContractCompileError(f"{label} must be an evidence source object")
    path = value.get("path")
    sha256 = value.get("sha256")
    if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise ContractCompileError(f"{label}.path must be a workspace-relative evidence path")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256) or sha256 == "0" * 64:
        raise ContractCompileError(f"{label}.sha256 must be a real lowercase SHA-256")
    return {"path": path, "sha256": sha256}


def _provenance(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in {"captured", "source_grounded_synthetic"}:
        raise ContractCompileError(f"{label} must declare captured or source_grounded_synthetic provenance")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ContractCompileError(f"{label}.sources must be nonempty")
    result: dict[str, Any] = {
        "kind": value["kind"],
        "sources": [_source_record(item, f"{label}.sources[{index}]") for index, item in enumerate(sources)],
    }
    if value["kind"] == "source_grounded_synthetic":
        basis = value.get("synthetic_basis")
        if not isinstance(basis, str) or not basis:
            raise ContractCompileError(f"{label}.synthetic_basis is required")
        result["synthetic_basis"] = basis
    return result


def _field_map(value: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ContractCompileError(f"{label} must be an object")
    result: dict[str, dict[str, Any]] = {}
    for name in ("title", "context", "main", "return"):
        field = value.get(name)
        if not isinstance(field, dict) or not isinstance(field.get("selector"), str) or not field["selector"]:
            raise ContractCompileError(f"{label}.{name}.selector must be source-grounded")
        rendered: dict[str, Any] = {"selector": field["selector"]}
        if "min_chars" in field:
            if not isinstance(field["min_chars"], int) or field["min_chars"] < 0:
                raise ContractCompileError(f"{label}.{name}.min_chars must be nonnegative")
            rendered["min_chars"] = field["min_chars"]
        if name == "return":
            expected_path = field.get("expected_path")
            if not isinstance(expected_path, str) or not expected_path.startswith("/") or "#" in expected_path:
                raise ContractCompileError(f"{label}.return.expected_path must be a local route")
            rendered["expected_path"] = expected_path
        result[name] = rendered
    return result


def _outcome(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in OUTCOME_KINDS:
        raise ContractCompileError(f"{label}.outcome must be source-grounded")
    result = dict(value)
    kind = result["kind"]
    if kind in {"navigation", "popup"}:
        expected = result.get("expected_path")
        if not isinstance(expected, str) or not expected.startswith("/") or "#" in expected:
            raise ContractCompileError(f"{label}.outcome.expected_path must be a local route")
    if kind in {"state_change", "input_state", "mutation"}:
        assertions = result.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise ContractCompileError(f"{label}.outcome.assertions must be source-grounded")
    if kind == "mutation" and (
        not isinstance(result.get("sqlite"), dict)
        or not isinstance(result.get("reset_evidence"), dict)
    ):
        raise ContractCompileError(f"{label}.mutation requires captured SQLite and reset evidence")
    return result


def compile_contract(
    discovery: dict[str, Any],
    *,
    site: str,
    profile: str,
    viewport_width: int,
    viewport_height: int,
    source_path: Path | None = None,
) -> dict[str, Any]:
    if discovery.get("schema") != "webanything-discovery-package-v1":
        raise ContractCompileError("unsupported discovery package schema")
    if profile not in {"task-driven", "immersive", "authenticated"}:
        raise ContractCompileError(f"unsupported profile: {profile}")
    if not re.fullmatch(r"[a-z0-9_]+", site):
        raise ContractCompileError(f"invalid site id: {site}")
    if viewport_width < 320 or viewport_height < 320:
        raise ContractCompileError("viewport dimensions must be at least 320 pixels")
    pages = discovery.get("pages")
    workflows = discovery.get("workflows")
    controls = discovery.get("controls")
    if not isinstance(pages, list) or not pages:
        raise ContractCompileError("discovery package must contain at least one page")
    if not isinstance(workflows, list) or not isinstance(controls, list):
        raise ContractCompileError("discovery workflows and controls must be lists")

    source = discovery.get("source")
    if not isinstance(source, dict):
        raise ContractCompileError("discovery source must be an object")
    entry_url = source.get("entry_url")
    parsed_entry = urlsplit(str(entry_url or ""))
    if parsed_entry.scheme not in {"http", "https"} or not parsed_entry.hostname:
        raise ContractCompileError("discovery source.entry_url must be absolute HTTP(S)")
    allowed_origins = source.get("allowed_origins")
    if not isinstance(allowed_origins, list) or not allowed_origins:
        raise ContractCompileError("discovery source.allowed_origins must be nonempty")
    namespace_provenance = _provenance(discovery.get("provenance"), "discovery.provenance")

    routes: list[dict[str, Any]] = []
    route_inventory: list[dict[str, Any]] = []
    route_paths: set[str] = set()
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise ContractCompileError(f"pages[{index}] must be an object")
        route = canonical_route(page)
        if route in route_paths:
            raise ContractCompileError(f"duplicate discovered route: {route}")
        route_paths.add(route)
        route_id = str(page.get("id") or f"route-{index:04d}")
        archetype = page.get("archetype")
        if archetype not in ARCHETYPES:
            raise ContractCompileError(f"pages[{index}].archetype is unsupported")
        provenance = _provenance(page.get("provenance"), f"pages[{index}].provenance")
        minimum_fields = _field_map(page.get("minimum_fields"), f"pages[{index}].minimum_fields")
        public_route = {
            "id": route_id,
            "route": route,
            "path": route,
            "archetype": archetype,
            "title": page.get("title"),
            "critical": bool(page.get("critical", route == "/")),
            "source_evidence": provenance,
        }
        routes.append(public_route)
        verifier_route: dict[str, Any] = {
            "id": route_id,
            "path": route,
            "family": str(page.get("family") or archetype),
            "archetype": archetype,
            "critical": public_route["critical"],
            "provenance": provenance,
            "minimum_fields": minimum_fields,
        }
        alias_of = page.get("alias_of")
        if alias_of is not None:
            canonical_selector = page.get("canonical_selector")
            if not isinstance(alias_of, str) or alias_of not in route_paths | {
                canonical_route(item) for item in pages if isinstance(item, dict)
            }:
                raise ContractCompileError(f"pages[{index}].alias_of is undeclared")
            if not isinstance(canonical_selector, str) or not canonical_selector:
                raise ContractCompileError(f"pages[{index}].canonical_selector is required")
            verifier_route.update({"alias_of": alias_of, "canonical_selector": canonical_selector})
        route_inventory.append(verifier_route)
    if "/" not in {item["route"].split("?", 1)[0] for item in routes}:
        raise ContractCompileError("discovery package must include the entry route /")

    public_controls = [dict(item) for item in controls if isinstance(item, dict)]
    control_inventory: list[dict[str, Any]] = []
    control_ids: set[str] = set()
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            raise ContractCompileError(f"controls[{index}] must be an object")
        obligated = bool(control.get("required", True)) or profile == "immersive"
        if not obligated:
            continue
        control_id = str(control.get("id") or f"control-{index:04d}")
        if control_id in control_ids:
            raise ContractCompileError(f"duplicate required control id: {control_id}")
        source_route = control.get("route") or control.get("source_path")
        if not isinstance(source_route, str) or source_route not in route_paths:
            raise ContractCompileError(f"controls[{index}].route must name a discovered route")
        selector = control.get("selector")
        label = control.get("label") or control.get("accessible_name")
        control_type = control.get("type") or control.get("control_type")
        if not isinstance(selector, str) or not selector:
            raise ContractCompileError(f"controls[{index}].selector must be source-grounded")
        if not isinstance(label, str) or not label.strip():
            raise ContractCompileError(f"controls[{index}].label must be source-grounded")
        if control_type not in CONTROL_TYPES:
            raise ContractCompileError(f"controls[{index}].type is unsupported")
        outcome = _outcome(control.get("outcome") or control.get("expected_outcome"), f"controls[{index}]")
        rendered = {
            "id": control_id,
            "source_path": source_route,
            "selector": selector,
            "identity_selector": str(control.get("identity_selector") or selector),
            "runtime_selector": str(control.get("runtime_selector") or selector),
            "accessible_name": label.strip(),
            "control_type": control_type,
            "critical": bool(control.get("critical", False)),
            "reveal": list(control.get("reveal") or []),
            "outcome": outcome,
        }
        control_inventory.append(rendered)
        control_ids.add(control_id)
    if not control_inventory:
        raise ContractCompileError("discovery evidence contains no required, verifiable controls")

    workflow_records: list[dict[str, Any]] = []
    seen_workflows: set[str] = set()
    for index, workflow in enumerate(workflows):
        if not isinstance(workflow, dict):
            raise ContractCompileError(f"workflows[{index}] must be an object")
        workflow_id = str(workflow.get("id") or f"workflow-{index:04d}")
        route_refs = workflow.get("routes")
        control_refs = workflow.get("controls")
        if not isinstance(route_refs, list) or not route_refs or not all(item in route_paths for item in route_refs):
            raise ContractCompileError(f"workflows[{index}].routes must name discovered routes")
        retained_controls = [item for item in control_refs or [] if item in control_ids]
        if not retained_controls:
            continue
        if workflow_id in seen_workflows:
            raise ContractCompileError(f"duplicate workflow id: {workflow_id}")
        seen_workflows.add(workflow_id)
        workflow_records.append({
            "id": workflow_id,
            "routes": list(dict.fromkeys(route_refs)),
            "controls": list(dict.fromkeys(retained_controls)),
        })
    if not workflow_records:
        raise ContractCompileError("discovery evidence contains no required, verifiable workflows")

    route_graph = []
    for control in control_inventory:
        outcome = control["outcome"]
        target = outcome.get("expected_path")
        if outcome["kind"] in {"navigation", "popup"} and target in route_paths:
            route_graph.append({"from": control["source_path"], "to": target, "control_id": control["id"]})

    hard_gates = {
        "runtime": True,
        "visual": True,
        "interaction": True,
        "state_transition": True,
        "seed_reset": True,
        "candidate_immutability_during_verification": True,
        "unexpected_external_requests": 0,
        "privacy_scan": True,
    }
    if profile == "immersive":
        hard_gates.update({
            "active_control_resolution_percent": 100,
            "critical_workflow_semantics_percent": 100,
            "hard_source_dom_route_gaps": 0,
            "generic_fallbacks": 0,
            "defensive_scope_pages": 0,
        })

    captured_at = discovery.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at:
        raise ContractCompileError("discovery captured_at is required for deterministic compilation")
    source_origin = f"{parsed_entry.scheme}://{parsed_entry.netloc}"
    contract_source = {
        "entry_url": entry_url,
        "allowed_origins": allowed_origins,
        "discovery_package": source_path.name if source_path else None,
        "discovery_sha256": source_sha256(source_path) if source_path else None,
    }
    synthetic_policy = discovery.get("synthetic_content_policy")
    if not isinstance(synthetic_policy, dict):
        synthetic_policy = {
            "allowed": False,
            "requires_source_grounded_provenance": True,
            "private_user_data_allowed": False,
        }
    return {
        "schema": "webanything-site-contract-v1",
        "schema_version": 1,
        "contract": "webharbor-immersive-interaction-contract-v1",
        "site": site,
        "profile": profile,
        "compiled_at": captured_at,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "source": contract_source,
        "routes": routes,
        "workflows": workflows,
        "interaction_contract": {"controls": public_controls},
        "discovery_snapshot_assertions": discovery.get("assertions") or [],
        "legacy_seed_regression_assertions": [],
        "synthetic_content_policy": synthetic_policy,
        "release_gates": hard_gates,
        "crawl": {"max_pages": len(route_inventory), "max_depth": 2, "start_routes": ["/"]},
        "namespaces": [{
            "kind": "primary",
            "source_origin": source_origin,
            "local_prefix": "/",
            "provenance": namespace_provenance,
        }],
        "route_inventory": route_inventory,
        "control_inventory": control_inventory,
        "critical_workflows": workflow_records,
        "route_graph": route_graph,
    }


def load_and_compile(
    discovery_path: Path,
    *,
    site: str,
    profile: str,
    viewport_width: int,
    viewport_height: int,
) -> dict[str, Any]:
    try:
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractCompileError(f"invalid discovery package: {discovery_path}: {exc}") from exc
    if not isinstance(discovery, dict):
        raise ContractCompileError("discovery package must be a JSON object")
    return compile_contract(
        discovery,
        site=site,
        profile=profile,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        source_path=discovery_path,
    )
