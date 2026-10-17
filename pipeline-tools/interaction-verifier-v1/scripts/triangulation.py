"""Join candidate source, contract, rendered controls, and Flask routes."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from contract import (
    canonical_json,
    canonical_control_identity,
    canonical_control_identity_key,
    control_identity_compatible,
    normalize_route_path,
    semantic_control_key,
)
from inventory import ANALYZER_TOOL, INVENTORY_SCHEMA

DIFF_SCHEMA = "webharbor-immersive-interaction-diff-v1"
GAP_CATEGORIES = (
    "source_active_control_missing_from_contract",
    "declared_control_without_source_basis",
    "rendered_active_control_missing_from_source_or_contract",
    "source_control_hidden_disabled_unhit_testable_or_absent",
    "local_target_without_actual_route",
    "actual_public_get_route_omitted_from_declared_or_reachable_graph",
    "code_browser_route_query_or_method_mismatch",
    "unexpected_external_target",
    "unresolved_dynamic_action_or_target",
    "duplicate_or_generic_fallback_relationship",
)


def route_path(route: str | None) -> str | None:
    return normalize_route_path(route)


def route_equivalent(source_rule: str | None, concrete: str | None) -> bool:
    if not source_rule or not concrete:
        return False
    path = route_path(concrete)
    if "<" not in source_rule:
        return route_path(source_rule) == path
    source_path = route_path(source_rule) or source_rule
    source_parts = [part for part in source_path.strip("/").split("/") if part]
    concrete_parts = [part for part in (path or "/").strip("/").split("/") if part]
    if len(source_parts) != len(concrete_parts) and not any(
        part.startswith("<path:") for part in source_parts
    ):
        return False
    pattern_parts: list[str] = []
    for part in source_parts:
        if part.startswith("<path:") and part.endswith(">"):
            pattern_parts.append(".+")
        elif part.startswith("<") and part.endswith(">"):
            pattern_parts.append("[^/]+")
        else:
            pattern_parts.append(re.escape(part))
    pattern = "/" + "/".join(pattern_parts)
    return re.fullmatch(pattern, path or "/") is not None


def canonicalize_controls(records: list[dict]) -> list[dict]:
    """Return one representative per semantic family with raw provenance."""
    grouped: dict[tuple[str, ...], dict] = {}
    for record in records:
        routes = record.get("source_routes") or record.get("source_path") or record.get("route") or "/"
        route = routes[0] if isinstance(routes, list) and routes else str(routes)
        key = semantic_control_key(record, route=route)
        item = grouped.get(key)
        if item is None:
            item = dict(record)
            digest = hashlib.sha256("\0".join(key).encode("utf-8")).hexdigest()[:20]
            item["family_id"] = f"control-family-{digest}"
            item["raw_observation_count"] = 0
            item["raw_provenance"] = []
            grouped[key] = item
        item["raw_observation_count"] += int(record.get("raw_observation_count") or 1)
        item["raw_provenance"].append({
            "id": record.get("id") or record.get("audit_id"),
            "route": record.get("source_path") or record.get("route"),
            "selector": record.get("selector"),
            "target": record.get("normalized_target") or record.get("href") or record.get("literal_target"),
        })
        for field in ("source_routes", "normalized_targets"):
            values = set(item.get(field) or [])
            values.update(record.get(field) or [])
            if values:
                item[field] = sorted(values)
    result = []
    for item in grouped.values():
        item["raw_provenance"] = sorted(
            {json.dumps(value, sort_keys=True): value for value in item["raw_provenance"]}.values(),
            key=lambda value: json.dumps(value, sort_keys=True),
        )[:64]
        result.append(item)
    return sorted(result, key=lambda item: item.get("family_id", ""))


def _context(contract: dict, route: str | None) -> dict[str, str]:
    path = route_path(route)
    declaration = next(
        (item for item in contract["route_inventory"] if route_path(item["path"]) == path),
        None,
    )
    return {
        "route_family": (declaration or {}).get("family", "undeclared"),
        "page_archetype": (declaration or {}).get("archetype", "undeclared"),
    }


def _source_location(record: dict | None) -> dict | None:
    if not record:
        return None
    return {
        "file": record.get("file"),
        "line": record.get("line"),
        "syntax_kind": record.get("syntax_kind"),
        "source_id": record.get("id"),
    }


def _gap(
    category: str,
    message: str,
    contract: dict,
    *,
    route: str | None = None,
    source: dict | None = None,
    control: dict | None = None,
    selector: str | None = None,
    evidence: Any | None = None,
) -> dict:
    item = {
        "code": category,
        "category": category,
        "message": message,
        "route": route,
        **_context(contract, route),
        "control_id": (control or {}).get("id"),
        "control_type": (control or source or {}).get("control_type", "route"),
        "selector": selector or (control or source or {}).get("selector"),
        "source_location": _source_location(source),
    }
    if evidence not in (None, {}, []):
        item["evidence"] = evidence
    return item


def _contract_for_source(source: dict, contract: dict) -> list[dict]:
    matches = []
    for control in contract["control_inventory"]:
        if not any(route_equivalent(rule, control["source_path"]) for rule in source.get("source_routes") or []):
            continue
        if any(control_identity_compatible(
            source, control, left_route=rule, right_route=control.get("source_path")
        ) for rule in source.get("source_routes") or []):
            matches.append(control)
    return matches


def _source_for_contract(control: dict, sources: list[dict]) -> list[dict]:
    matches = []
    for source in sources:
        if any(control_identity_compatible(
            source, control, left_route=rule, right_route=control.get("source_path")
        ) for rule in source.get("source_routes") or []):
            matches.append(source)
    return matches


def _actual_route(target: str, route_map: dict) -> dict | None:
    return next(
        (item for item in route_map.get("routes") or [] if route_equivalent(item["rule"], target)),
        None,
    )


def _method_supported(target: str, method: str | None, route_map: dict) -> bool:
    actual = _actual_route(target, route_map)
    return bool(actual and (method or "GET").upper() in actual["methods"])


def _sort_unique(items: list[dict]) -> list[dict]:
    values = {json.dumps(item, sort_keys=True, separators=(",", ":")): item for item in items}
    return sorted(
        values.values(),
        key=lambda item: (
            str(item.get("route") or ""),
            str((item.get("source_location") or {}).get("file") or ""),
            int((item.get("source_location") or {}).get("line") or 0),
            str(item.get("control_id") or ""),
            str(item.get("selector") or ""),
            str(item.get("code") or ""),
        ),
    )


def build_interaction_diff(
    *,
    site: str,
    inventory: dict,
    contract: dict,
    contract_sha256: str,
    discovered_controls: list[dict],
    control_results: list[dict],
    observed_edges: list[dict],
    source_dom_states: list[dict],
    existing_failures: list[dict],
) -> dict:
    categories: dict[str, list[dict]] = {name: [] for name in GAP_CATEGORIES}
    source_contract = contract.get("_source_contract_view") or contract
    raw_sources = [item for item in inventory["controls"] if item.get("active_surface", True)]
    sources = canonicalize_controls(raw_sources)
    discovered = canonicalize_controls(discovered_controls)
    route_map = inventory["actual_route_map"]
    control_results_by_id = {item["id"]: item for item in control_results}

    source_contract_joins = []
    raw_identity_counts: dict[tuple[str, ...], int] = {}
    for raw in raw_sources:
        route = (raw.get("source_routes") or raw.get("source_path") or "/")
        route = route[0] if isinstance(route, list) and route else route
        key = canonical_control_identity_key(raw, route=str(route))
        raw_identity_counts[key] = raw_identity_counts.get(key, 0) + 1
    for source in sources:
        matches = _contract_for_source(source, source_contract)
        source_contract_joins.append({
            "source_id": source["id"],
            "contract_control_ids": sorted(item["id"] for item in matches),
            "control_identity": source.get("control_identity") or canonical_control_identity(
                source, route=(source.get("source_routes") or ["/"])[0]
            ),
        })
        if not matches:
            route = (source.get("source_routes") or [None])[0]
            categories["source_active_control_missing_from_contract"].append(_gap(
                "source_active_control_missing_from_contract",
                "active source control is absent from the declared interaction contract",
                contract,
                route=route,
                source=source,
            ))
        if source.get("raw_observation_count", 0) > 1:
            categories["duplicate_or_generic_fallback_relationship"].append(_gap(
                "duplicate_or_generic_fallback_relationship",
                "multiple source controls share the same complete semantic identity",
                contract,
                route=(source.get("source_routes") or [None])[0],
                source=source,
                evidence={"raw_observation_count": source.get("raw_observation_count")},
            ))

    for control in source_contract["control_inventory"]:
        matches = _source_for_contract(control, sources)
        if not matches:
            categories["declared_control_without_source_basis"].append(_gap(
                "declared_control_without_source_basis",
                "declared control has no statically grounded source control",
                contract,
                route=control["source_path"],
                control=control,
            ))

    contract_identity_groups: dict[tuple[str, ...], list[dict]] = {}
    for control in source_contract["control_inventory"]:
        key = canonical_control_identity_key(control, route=control.get("source_path"))
        contract_identity_groups.setdefault(key, []).append(control)
    for key, members in contract_identity_groups.items():
        if len(members) > 1:
            categories["duplicate_or_generic_fallback_relationship"].append(_gap(
                "duplicate_or_generic_fallback_relationship",
                "multiple declared controls have the same complete semantic identity",
                contract,
                route=members[0].get("source_path"),
                control=members[0],
                evidence={"control_ids": sorted(item.get("id") for item in members)},
            ))

    source_browser_joins = []
    for rendered in discovered:
        route = rendered["source_path"]
        source_matches = [
            source for source in sources
            if any(control_identity_compatible(
                source, rendered, left_route=rule, right_route=route
            ) for rule in source.get("source_routes") or [])
        ]
        contract_matches = [
            control for control in contract["control_inventory"]
            if route_equivalent(control["source_path"], route)
            and control_identity_compatible(control, rendered,
                                            left_route=control.get("source_path"),
                                            right_route=route)
        ]
        source_browser_joins.append({
            "route": route,
            "selector": rendered.get("selector"),
            "identity_selector": rendered.get("identity_selector") or rendered.get("selector"),
            "runtime_selector": rendered.get("runtime_selector"),
            "control_identity": rendered.get("control_identity") or canonical_control_identity(rendered, route=route),
            "source_ids": sorted(item["id"] for item in source_matches),
            "contract_control_ids": sorted(item["id"] for item in contract_matches),
        })
        if not source_matches or not contract_matches:
            categories["rendered_active_control_missing_from_source_or_contract"].append(_gap(
                "rendered_active_control_missing_from_source_or_contract",
                "rendered active control lacks source and contract triangulation",
                contract,
                route=route,
                source=source_matches[0] if source_matches else None,
                control=contract_matches[0] if contract_matches else None,
                selector=rendered.get("selector"),
                evidence={
                    "accessible_name": rendered.get("accessible_name"),
                    "source_basis": bool(source_matches),
                    "contract_basis": bool(contract_matches),
                },
            ))
        if rendered.get("raw_observation_count", 0) > 1:
            categories["duplicate_or_generic_fallback_relationship"].append(_gap(
                "duplicate_or_generic_fallback_relationship",
                "multiple rendered controls share the same complete semantic identity",
                contract,
                route=route,
                source=source_matches[0] if source_matches else None,
                control=contract_matches[0] if contract_matches else None,
                selector=rendered.get("selector"),
                evidence={"raw_observation_count": rendered.get("raw_observation_count")},
            ))

    states_by_source: dict[str, list[dict]] = {}
    for state in source_dom_states:
        states_by_source.setdefault(state["source_id"], []).append(state)
    for source in sources:
        states = states_by_source.get(source["id"], [])
        acceptable = any(
            state.get("count") == 1
            and state.get("visible")
            and not state.get("disabled")
            and state.get("hit_testable")
            for state in states
        )
        if source.get("hidden_literal") or source.get("disabled_literal") or not acceptable:
            route = states[0].get("route") if states else (source.get("source_routes") or [None])[0]
            categories["source_control_hidden_disabled_unhit_testable_or_absent"].append(_gap(
                "source_control_hidden_disabled_unhit_testable_or_absent",
                "source active control is hidden, disabled, unhit-testable, ambiguous, or absent in rendered DOM",
                contract,
                route=route,
                source=source,
                evidence={"states": states, "hidden_literal": source.get("hidden_literal"), "disabled_literal": source.get("disabled_literal")},
            ))

    local_targets = []
    for source in sources:
        for target in source.get("normalized_targets") or ([source.get("normalized_target")] if source.get("normalized_target") else []):
            if source.get("target_kind") == "local":
                local_targets.append((target, source.get("method") or "GET", source, None, (source.get("source_routes") or [None])[0]))
    for interaction in inventory["interactions"]:
        if (
            interaction.get("active_surface", True)
            and
            interaction.get("syntax_kind") != "python_render_template"
            and interaction.get("target_kind") == "local"
            and interaction.get("normalized_target")
        ):
            local_targets.append((interaction["normalized_target"], interaction.get("method") or "GET", interaction, None, None))
    for control in contract["control_inventory"]:
        outcome = control.get("outcome") or {}
        if outcome.get("kind") in {"navigation", "popup"}:
            local_targets.append((outcome["expected_path"], "GET", None, control, control["source_path"]))
    for edge in observed_edges:
        if edge.get("to"):
            local_targets.append((edge["to"], "GET", None, None, edge["from"]))
    target_route_joins = []
    for target, method, source, control, route in local_targets:
        actual = _actual_route(target, route_map)
        target_route_joins.append({
            "target": target,
            "method": method,
            "actual_rule": actual.get("rule") if actual else None,
            "actual_methods": actual.get("methods") if actual else [],
        })
        if actual is None:
            categories["local_target_without_actual_route"].append(_gap(
                "local_target_without_actual_route",
                "local interaction target has no actual Flask route",
                contract,
                route=route or target,
                source=source,
                control=control,
                evidence={"target": target, "method": method},
            ))
        elif method and method.upper() not in actual["methods"]:
            categories["code_browser_route_query_or_method_mismatch"].append(_gap(
                "code_browser_route_query_or_method_mismatch",
                "source or contract method is not accepted by the actual Flask route",
                contract,
                route=route or target,
                source=source,
                control=control,
                evidence={"target": target, "expected_method": method.upper(), "actual_methods": actual["methods"]},
            ))

    graph_routes = set(contract["crawl"]["start_routes"])
    graph_routes.update(item["path"] for item in contract["route_inventory"])
    for edge in contract["route_graph"]:
        graph_routes.update((edge["from"], edge["to"]))
    for actual in route_map.get("public_get_routes") or []:
        if not any(route_equivalent(actual["rule"], declared) for declared in graph_routes):
            categories["actual_public_get_route_omitted_from_declared_or_reachable_graph"].append(_gap(
                "actual_public_get_route_omitted_from_declared_or_reachable_graph",
                "actual public GET route is omitted from the declared and reachable graph",
                contract,
                route=actual["rule"],
                evidence={"endpoint": actual["endpoint"], "methods": actual["methods"]},
            ))

    for source in sources:
        source_targets = source.get("normalized_targets") or ([source.get("normalized_target")] if source.get("normalized_target") else [])
        for control in _contract_for_source(source, source_contract):
            expected = (control.get("outcome") or {}).get("expected_path")
            form_query_expansion = (
                source.get("tag") == "form"
                and any(
                    route_path(target) == route_path(expected)
                    and not urlsplit(target).query
                    for target in source_targets
                )
            )
            if expected and source_targets and expected not in source_targets and not form_query_expansion:
                categories["code_browser_route_query_or_method_mismatch"].append(_gap(
                    "code_browser_route_query_or_method_mismatch",
                    "source target and declared exact path/query differ",
                    contract,
                    route=control["source_path"],
                    source=source,
                    control=control,
                    evidence={"source_targets": source_targets, "contract_target": expected},
                ))
    for edge in observed_edges:
        controls = [
            item for item in source_contract["control_inventory"]
            if item["source_path"] == edge["from"] and item["selector"] == edge["selector"]
        ]
        for control in controls:
            expected = (control.get("outcome") or {}).get("expected_path")
            if expected and edge.get("to") != expected:
                categories["code_browser_route_query_or_method_mismatch"].append(_gap(
                    "code_browser_route_query_or_method_mismatch",
                    "browser target and declared exact path/query differ",
                    contract,
                    route=edge["from"],
                    control=control,
                    evidence={"browser_target": edge.get("to"), "contract_target": expected},
                ))

    for record in [*sources, *[item for item in inventory["interactions"] if item.get("active_surface", True)]]:
        if record.get("target_kind") == "external":
            route = (record.get("source_routes") or [None])[0]
            categories["unexpected_external_target"].append(_gap(
                "unexpected_external_target",
                "active candidate source targets an external destination",
                contract,
                route=route,
                source=record,
                evidence={"target": record.get("normalized_target"), "event": record.get("event")},
            ))

    proved_dynamic = []
    for record in inventory["unresolved"]:
        if not record.get("affects_active_surface"):
            continue
        selector = record.get("selector")
        source_routes = record.get("source_routes") or []
        if selector and not source_routes:
            related = [source for source in sources if source.get("selector") == selector]
            source_routes = sorted({route for source in related for route in source.get("source_routes") or []})
        controls = [
            control for control in source_contract["control_inventory"]
            if selector and control["selector"] == selector
            and any(route_equivalent(rule, control["source_path"]) for rule in source_routes)
        ]
        passed = [control for control in controls if control_results_by_id.get(control["id"], {}).get("passed")]
        if passed:
            proved_dynamic.append({"source_id": record["id"], "control_ids": sorted(item["id"] for item in passed)})
            continue
        route = source_routes[0] if source_routes else None
        categories["unresolved_dynamic_action_or_target"].append(_gap(
            "unresolved_dynamic_action_or_target",
            "dynamic active source semantics lack a matching declaration and browser proof",
            contract,
            route=route,
            source=record,
            control=controls[0] if controls else None,
            evidence={"reason": record.get("reason"), "confidence": record.get("confidence")},
        ))
    for message in inventory.get("failures") or []:
        categories["unresolved_dynamic_action_or_target"].append(_gap(
            "unresolved_dynamic_action_or_target",
            "isolated source or route-map analysis failed closed",
            contract,
            evidence={"reason": message},
        ))

    for prior in existing_failures:
        if prior.get("code") not in {"generic_fallback_duplicate", "alias_mapping_unproved"}:
            continue
        categories["duplicate_or_generic_fallback_relationship"].append(_gap(
            "duplicate_or_generic_fallback_relationship",
            prior.get("message") or "r1 duplicate or generic fallback relationship",
            contract,
            route=prior.get("route"),
            selector=prior.get("selector"),
            evidence={"r1_failure": prior},
        ))

    categories = {name: _sort_unique(categories[name]) for name in GAP_CATEGORIES}
    hard_gaps = _sort_unique([item for name in GAP_CATEGORIES for item in categories[name]])
    diff = {
        "schema_version": 1,
        "diff": DIFF_SCHEMA,
        "analyzer": ANALYZER_TOOL,
        "status": "passed" if not hard_gaps else "failed",
        "site": site,
        "inventory_identity": {
            "schema": INVENTORY_SCHEMA,
            "analyzer": ANALYZER_TOOL,
            "analyzer_implementation_sha256": inventory["analyzer_implementation_sha256"],
            "payload_sha256": inventory["payload_sha256"],
            "path": "code-interaction-inventory.json",
        },
        "contract_identity": {
            "sha256": contract_sha256,
            "routes": len(contract["route_inventory"]),
            "controls": len(contract["control_inventory"]),
            "source_controls": sum(not item.get("derived") for item in contract["control_inventory"]),
            "derived_controls": sum(bool(item.get("derived")) for item in contract["control_inventory"]),
        },
        "browser_identity": {"controls": len(discovered_controls), "outcomes": len(control_results), "observed_edges": len(observed_edges)},
        "route_map_identity": {
            "tool": route_map["tool"],
            "status": route_map["status"],
            "source_sha256": route_map["source_hashes"]["after"]["sha256"],
            "seed_sha256": route_map["seed_hashes"]["after"]["sha256"],
            "routes": len(route_map["routes"]),
            "public_get_routes": len(route_map["public_get_routes"]),
            "source_unchanged": route_map["source_hashes"]["unchanged"],
            "seed_unchanged": route_map["seed_hashes"]["unchanged"],
        },
        "joins": {
            "source_to_contract": sorted(source_contract_joins, key=lambda item: item["source_id"]),
            "source_contract_to_browser": sorted(source_browser_joins, key=lambda item: (item["route"], item["selector"])),
            "local_targets_to_routes": sorted(
                {json.dumps(item, sort_keys=True): item for item in target_route_joins}.values(),
                key=lambda item: (item["target"], item["method"]),
            ),
            "proved_dynamic_targets": sorted(proved_dynamic, key=lambda item: item["source_id"]),
        },
        "gap_categories": categories,
        "hard_gap_count": len(hard_gaps),
        "hard_gaps": hard_gaps,
    }
    diff["payload_sha256"] = hashlib.sha256(canonical_json(diff)).hexdigest()
    return diff
