#!/usr/bin/env python3
"""Bounded standalone interaction-verifier entrypoint.

This adapter owns no site lifecycle unless an explicit launch configuration is
provided.  It stages launched candidates into an isolated temporary directory,
hashes the caller's candidate before and after verification, and writes only
the requested evidence artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from contract import (
    ContractError,
    atomic_json,
    candidate_fingerprint,
    canonical_json,
    build_repair_package,
    sha256_bytes,
    sha256_file,
)
from checkpoint import (
    build_verification_plan,
    load_valid_checkpoints,
    merge_checkpoints,
    partition_plan,
    write_checkpoint,
    shared_artifact_ref,
)

ARTIFACTS = (
    "interaction-contract.json",
    "code-interaction-inventory.json",
    "interaction-diff.json",
    "interaction-closure.json",
    "builder-repair-package.json",
)
SHARED_PREFLIGHT_ARTIFACTS = (
    "code-interaction-inventory.json",
    "interaction-contract.json",
    "route-map.json",
)


def _load_verifier(dependency_path: Path | None):
    """Load browser dependencies after CLI arguments have selected their path.

    The terminal is intentionally usable from an arbitrary cwd with an
    explicit dependency directory.  Importing ``browser_verifier`` at module
    import time made that contract impossible because Playwright was resolved
    before ``--dependency-path`` could be parsed.
    """
    if dependency_path is not None:
        dependency_text = str(dependency_path.resolve())
        if dependency_text not in sys.path:
            sys.path.insert(0, dependency_text)
    from browser_verifier import verify

    return verify


def _tree_identity(root: Path) -> dict[str, Any]:
    root = root.resolve()
    digest = hashlib.sha256()
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(part in {"__pycache__", ".pytest_cache"} for part in Path(relative).parts):
            continue
        if path.is_symlink():
            payload = ("symlink:" + path.readlink().as_posix()).encode()
        elif path.is_file():
            payload = path.read_bytes()
        else:
            continue
        encoded = relative.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        entries.append({"path": relative, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    return {
        "algorithm": "webharbor-candidate-tree-v1",
        "sha256": digest.hexdigest(),
        "files": len(entries),
        "bytes": sum(item["bytes"] for item in entries),
        "entries": entries,
    }


def _canonical_candidate_tree(root: Path) -> dict[str, Any]:
    """Match the public WebAnything candidate-tree identity without imports."""
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in {"instance", "__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        if path.is_symlink():
            payload = ("symlink:" + path.readlink().as_posix()).encode("utf-8")
        elif path.is_file():
            payload = path.read_bytes()
        else:
            continue
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        files += 1
        total_bytes += len(payload)
    return {"algorithm": "webanything-site-tree-v1", "sha256": digest.hexdigest(), "files": files, "bytes": total_bytes}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _validate_frozen_identity(freeze: dict[str, Any], *, site: str) -> dict[str, Any]:
    if freeze.get("schema") != "webanything-candidate-freeze-v1" or freeze.get("state") != "frozen":
        raise ValueError("candidate freeze record must be schema v1 in frozen state")
    identity = freeze.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("candidate freeze record has no identity")
    configuration = identity.get("configuration") or {}
    inputs = {
        "tree_sha256": (identity.get("tree") or {}).get("sha256"),
        "contract_sha256s": [item.get("sha256") for item in identity.get("contracts") or []],
        "configuration": configuration,
    }
    expected = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if identity.get("schema") != "webanything-candidate-identity-v1" or identity.get("sha256") != expected:
        raise ValueError("candidate freeze identity digest is invalid")
    if configuration.get("site") not in (None, site):
        raise ValueError("candidate freeze configuration does not match the requested site")
    return identity


def _stage_contract_sources(contract: dict[str, Any], candidate_workspace: Path, stage_root: Path, site_path: Path) -> None:
    """Copy only declared external provenance files into an owned stage.

    A Site Contract may cite read-only evidence outside ``sites/<site>``.  The
    worker stages the self-contained site, so those explicitly declared files
    must be copied individually for provenance checks; staging the whole
    workspace would defeat isolation and is especially costly for Apple.
    """
    declarations = [
        *(contract.get("namespaces") or []),
        *(contract.get("route_inventory") or []),
        *(contract.get("synthetic_records") or []),
    ]
    copied: set[str] = set()
    for declaration in declarations:
        provenance = declaration.get("provenance") if isinstance(declaration, dict) else None
        for source in (provenance or {}).get("sources", []):
            relative = Path(str(source.get("path") or ""))
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"invalid provenance path: {source.get('path')!r}")
            source_path = (candidate_workspace / relative).resolve()
            try:
                source_path.relative_to(candidate_workspace.resolve())
            except ValueError as exc:
                raise ValueError(f"provenance path escapes workspace: {relative}") from exc
            if _inside(source_path, site_path) or source_path.is_dir() or not source_path.is_file():
                continue
            key = relative.as_posix()
            if key in copied:
                continue
            destination = stage_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            copied.add(key)


def _wait_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(url, method="GET"), timeout=min(2.0, max(0.2, deadline - time.monotonic()))) as response:
                if 200 <= response.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001 - bounded launch readiness
            error = exc
        time.sleep(0.05)
    raise RuntimeError(f"launch did not become ready at {url}: {error}")


def _launch(command: list[str], cwd: Path, env: dict[str, str], base_url: str, timeout: float) -> subprocess.Popen:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_http(base_url, timeout)
    except Exception:
        _terminate(process)
        raise
    return process


def _terminate(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, 15)
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001 - process teardown must fall back to SIGKILL
        try:
            os.killpg(process.pid, 9)
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001, S110 - process teardown is best effort
            pass


def _artifact_hashes(evidence_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in ARTIFACTS:
        path = evidence_dir / name
        if path.is_file():
            result[name] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return result


def _error_repair(
    site: str,
    message: str,
    *,
    fingerprint: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fingerprint = fingerprint or {
        "algorithm": "webharbor-standalone-candidate-v1",
        "sha256": sha256_bytes(message.encode()),
        "hash_status": "derived_from_terminal_failure",
    }
    failure = {
        "code": "terminal_error",
        "message": message,
        "route_family": "terminal",
        "page_archetype": "unknown",
        "control_type": "route",
        "expected_correction": "Provide a schema-valid Site Contract with at least one canonical route and rerun the verifier.",
        "rerun": {"command": "webanything verify", "parameters": parameters or {}},
    }
    return build_repair_package(
        site=site,
        fingerprint=fingerprint,
        failures=[failure],
    )


def _bind_shared_artifacts(plan: dict[str, Any], shared_root: Path) -> dict[str, Any]:
    """Bind immutable preflight hashes into the plan before partition/resume."""
    refs = {
        name: shared_artifact_ref(shared_root / name)
        for name in SHARED_PREFLIGHT_ARTIFACTS
        if (shared_root / name).is_file()
    }
    plan["shared_artifacts"] = refs
    identity_payload = {
        "candidate_fingerprint": plan.get("candidate_fingerprint"),
        "contract_sha256": plan.get("contract_sha256"),
        "verifier_version": (plan.get("verifier") or {}).get("version"),
        "verifier_configuration": (plan.get("verifier") or {}).get("configuration") or {},
        "viewport": plan.get("viewport"),
        "routes": (plan.get("inventory") or {}).get("routes") or [],
        "controls": (plan.get("inventory") or {}).get("controls") or [],
        "candidate_identity_sha256": plan.get("candidate_identity_sha256"),
        "analyzer_implementation_sha256": plan.get("analyzer_implementation_sha256"),
        "shared_artifacts": refs,
    }
    plan["plan_identity"] = hashlib.sha256(canonical_json(identity_payload)).hexdigest()
    return refs


def _shared_preflight_valid(path: Path, plan: dict[str, Any]) -> bool:
    manifest = path / "preflight-manifest.json"
    try:
        value = _load_json(manifest)
    except (OSError, ValueError, TypeError):
        return False
    if not (
        value.get("candidate_sha256") == plan.get("candidate_sha256")
        and value.get("contract_sha256") == plan.get("contract_sha256")
        and value.get("candidate_identity_sha256") == plan.get("candidate_identity_sha256")
        and value.get("analyzer_implementation_sha256") == plan.get("analyzer_implementation_sha256")
        and all((path / name).is_file() for name in SHARED_PREFLIGHT_ARTIFACTS)
    ):
        return False
    refs = value.get("shared_artifacts")
    if not isinstance(refs, dict):
        return False
    for name in SHARED_PREFLIGHT_ARTIFACTS:
        reference = refs.get(name)
        artifact = path / name
        if not isinstance(reference, dict):
            return False
        try:
            actual = shared_artifact_ref(artifact)
        except OSError:
            return False
        if actual.get("sha256") != reference.get("sha256") or actual.get("bytes") != reference.get("bytes"):
            return False
    return True


def _emit_authoritative_merge(
    *,
    plan: dict[str, Any],
    batches: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    evidence_dir: Path,
    contract_path: Path,
    shared_root: Path,
    site_root: Path,
    base_url: str,
    site: str,
    browser_path: Path | None,
    dependency_path: Path | None,
    merge_failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Materialize standard artifacts from immutable preflight + deltas once."""
    inventory_path = shared_root / "code-interaction-inventory.json"
    inventory = _load_json(inventory_path) if inventory_path.is_file() else {}
    route_map = _load_json(shared_root / "route-map.json") if (shared_root / "route-map.json").is_file() else inventory.get("actual_route_map", {})
    original_contract = _load_json(contract_path)
    outcomes = [
        item
        for checkpoint in checkpoints
        for item in ((checkpoint.get("result") or {}).get("outcomes") or [])
    ]
    routes = [
        item
        for checkpoint in checkpoints
        for item in ((checkpoint.get("result") or {}).get("routes") or [])
    ]
    failures = [
        item
        for checkpoint in checkpoints
        for item in ((checkpoint.get("result") or {}).get("failures") or [])
    ]
    observed_edges = [
        item
        for checkpoint in checkpoints
        for item in ((checkpoint.get("result") or {}).get("observed_edges") or [])
    ]
    disclosure_proofs = [
        item
        for checkpoint in checkpoints
        for item in ((checkpoint.get("result") or {}).get("disclosure_proofs") or [])
    ]
    unexpected_requests = [
        item
        for checkpoint in checkpoints
        for item in ((checkpoint.get("result") or {}).get("unexpected_requests") or [])
    ]
    failures.extend(merge_failures or [])
    # Checkpoint status is a claim; validate the bounded payload as well so a
    # hand-edited or buggy worker cannot turn a failed assigned outcome into a
    # passing merge merely by relabelling the batch.
    for checkpoint in checkpoints:
        result = checkpoint.get("result") or {}
        for outcome in result.get("outcomes") or []:
            if outcome.get("passed") is False:
                failures.append({"code": "control_outcome_failed", "control_id": outcome.get("id"), "batch_id": checkpoint.get("batch_id")})
        for route in result.get("routes") or []:
            if route.get("passed") is False:
                failures.append({"code": "route_outcome_failed", "route": route.get("requested_path"), "batch_id": checkpoint.get("batch_id")})
    failures = list({json.dumps(item, sort_keys=True, separators=(",", ":")): item for item in failures}.values())
    all_passed = bool(checkpoints) and all(item.get("status") == "passed" for item in checkpoints) and not failures
    status = "passed" if all_passed else "failed"
    zero = "0" * 64
    analyzer_hash = inventory.get("analyzer_implementation_sha256") or zero
    inventory_hash = inventory.get("payload_sha256") or zero
    contract_hash = sha256_bytes(canonical_json(original_contract))
    fingerprint = None
    categories = {
        name: [] for name in (
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
    }
    diff = {
        "schema_version": 1,
        "diff": "webharbor-immersive-interaction-diff-v1",
        "analyzer": inventory.get("analyzer", "webharbor-immersive-code-analyzer-v1"),
        "status": status,
        "site": site,
        "inventory_identity": {
            "schema": inventory.get("inventory", "webharbor-code-interaction-inventory-v1"),
            "analyzer": inventory.get("analyzer", "webharbor-immersive-code-analyzer-v1"),
            "analyzer_implementation_sha256": analyzer_hash,
            "payload_sha256": inventory_hash,
            "path": (shared_root / "code-interaction-inventory.json").as_posix(),
        },
        "contract_identity": {"sha256": contract_hash},
        "browser_identity": {"controls": len(outcomes), "outcomes": len(outcomes), "observed_edges": len(observed_edges)},
        "route_map_identity": {"payload_sha256": (plan.get("shared_artifacts", {}).get("route-map.json") or {}).get("sha256")},
        "joins": {"source_to_contract": [], "source_contract_to_browser": [], "local_targets_to_routes": [], "proved_dynamic_targets": []},
        "gap_categories": categories,
        "hard_gap_count": len(failures),
        "hard_gaps": failures,
    }
    diff["payload_sha256"] = hashlib.sha256(canonical_json(diff)).hexdigest()
    fingerprint = candidate_fingerprint(
        site_root,
        original_contract,
        code_inventory_sha256=inventory_hash,
        interaction_diff_sha256=diff["payload_sha256"],
        analyzer=inventory.get("analyzer", "webharbor-immersive-code-analyzer-v1"),
    )
    outcome_by_id = {item.get("id"): item for item in outcomes if item.get("id")}
    route_by_id = {item.get("route_id"): item for item in routes if item.get("route_id")}
    workflows = []
    for workflow in original_contract.get("critical_workflows") or []:
        route_checks = [
            {"path": path, "passed": bool(next((r for r in routes if r.get("requested_path") == path or r.get("actual_path") == path), {}).get("passed"))}
            for path in workflow.get("routes") or []
        ]
        control_checks = [
            {"id": control_id, "passed": bool(outcome_by_id.get(control_id, {}).get("passed"))}
            for control_id in workflow.get("controls") or []
        ]
        workflows.append({"id": workflow.get("id"), "routes": route_checks, "controls": control_checks, "passed": all(item["passed"] for item in [*route_checks, *control_checks])})
    critical_passed = sum(1 for item in workflows if item.get("passed"))
    closure = {
        "schema_version": 2,
        "closure": "webharbor-standalone-interaction-closure-v2",
        "tool": "webharbor-standalone-interaction-verifier-v1",
        "status": status,
        "site": site,
        "profile": "immersive",
        "candidate_fingerprint": fingerprint,
        "code_inventory": {"path": (evidence_dir / "code-interaction-inventory.json").as_posix(), "schema": inventory.get("inventory", "webharbor-code-interaction-inventory-v1"), "analyzer": inventory.get("analyzer", "webharbor-immersive-code-analyzer-v1"), "analyzer_implementation_sha256": analyzer_hash, "payload_sha256": inventory_hash},
        "interaction_diff": {"path": (evidence_dir / "interaction-diff.json").as_posix(), "schema": diff["diff"], "analyzer": diff["analyzer"], "payload_sha256": diff["payload_sha256"], "hard_gap_count": diff["hard_gap_count"]},
        "contract_identity": {"path": contract_path.as_posix(), "sha256": contract_hash},
        "surface": {"base_url": base_url, "origin": base_url.rstrip("/"), "gateway_prefix": None},
        "viewport": plan.get("viewport"),
        "crawl": {"max_pages": (plan.get("verifier") or {}).get("configuration", {}).get("max_pages") or (original_contract.get("crawl") or {}).get("max_pages", 1), "max_depth": (plan.get("verifier") or {}).get("configuration", {}).get("max_depth") if (plan.get("verifier") or {}).get("configuration", {}).get("max_depth") is not None else (original_contract.get("crawl") or {}).get("max_depth", 0), "visited_pages": len(routes), "maximum_depth_visited": 0, "inventory_complete": status == "passed"},
        "route_graph": {"declared_edges": original_contract.get("route_graph", []), "observed_edges": observed_edges},
        "page_archetypes": sorted(routes, key=lambda item: item.get("requested_path", "")),
        "control_inventory": inventory.get("controls", []),
        "source_control_dom_states": [],
        "control_outcomes": outcomes,
        "disclosure_proofs": disclosure_proofs,
        "critical_workflows": workflows,
        "canonical_control_families": inventory.get("canonical_control_families", []),
        "site_contract_derivation": {},
        "loaded_local_resources": [],
        "unexpected_requests": unexpected_requests,
        "unresolved_items": failures,
        "failures": failures,
        "raw_counts": {"inventoried_active_controls": len(inventory.get("controls", [])), "resolved_active_controls": sum(1 for item in outcomes if item.get("passed")), "source_active_controls": len(inventory.get("controls", [])), "actual_public_get_routes": len(route_map.get("public_get_routes", [])), "triangulation_hard_gaps": len(failures), "critical_workflows": len(workflows), "critical_workflows_passed": critical_passed, "dead_empty_or_404_destinations": sum(1 for item in failures if item.get("code") in {"dead_destination", "empty_destination", "navigation_error"}), "defensive_pages": sum(1 for item in failures if item.get("code") == "defensive_page"), "unexpected_external_requests": len(unexpected_requests), "unaudited_limit": sum(1 for item in failures if item.get("code") == "unaudited_limit")},
        "measured_rates": {"active_control_resolution_percent": round(100 * sum(1 for item in outcomes if item.get("passed")) / len(outcomes), 2) if outcomes else 0.0, "critical_workflow_semantics_percent": round(100 * critical_passed / len(workflows), 2) if workflows else 0.0},
    }
    repair = build_repair_package(site=site, fingerprint=fingerprint, failures=failures)
    atomic_json(evidence_dir / "interaction-contract.json", original_contract)
    atomic_json(evidence_dir / "code-interaction-inventory.json", inventory)
    atomic_json(evidence_dir / "interaction-diff.json", diff)
    atomic_json(evidence_dir / "interaction-closure.json", closure)
    atomic_json(evidence_dir / "builder-repair-package.json", repair)
    return closure


def run(args: argparse.Namespace) -> dict[str, Any]:
    # Programmatic callers created before checkpointing do not need to populate
    # the new CLI-only attributes.
    if not hasattr(args, "batch_size"):
        args.batch_size = None
    if not hasattr(args, "checkpoint_dir"):
        args.checkpoint_dir = None
    if not hasattr(args, "resume"):
        args.resume = False
    if not hasattr(args, "candidate_freeze"):
        args.candidate_freeze = None
    candidate_workspace = args.candidate_workspace.resolve()
    site_path = args.site_path.resolve()
    contract_path = args.contract.resolve()
    evidence_dir = args.evidence_dir.resolve()
    if not candidate_workspace.is_dir() or not site_path.is_dir():
        raise ValueError("candidate workspace and site path must be existing directories")
    if not _inside(site_path, candidate_workspace):
        raise ValueError("site path must be inside candidate workspace")
    if not contract_path.is_file():
        raise ValueError(f"missing Site Contract: {contract_path}")
    if _inside(evidence_dir, site_path):
        raise ValueError("evidence directory must be isolated from candidate site")
    if (args.viewport_width is None) != (args.viewport_height is None):
        raise ValueError("viewport width and height must be supplied together")
    if args.viewport_width is not None and (args.viewport_width < 1 or args.viewport_height < 1):
        raise ValueError("viewport dimensions must be positive")
    if args.dependency_path and not args.dependency_path.is_dir():
        raise ValueError("dependency path must be an existing directory")
    if args.browser_path and not args.browser_path.is_file():
        raise ValueError("browser path must be an existing executable")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    pre = _tree_identity(site_path)
    canonical_pre = _canonical_candidate_tree(site_path)
    contract_sha256 = sha256_file(contract_path)
    frozen_identity: dict[str, Any] | None = None
    if args.candidate_freeze:
        freeze = _load_json(args.candidate_freeze.resolve())
        frozen_identity = _validate_frozen_identity(freeze, site=args.site)
        if frozen_identity.get("tree") != _canonical_candidate_tree(site_path):
            raise ValueError("candidate tree does not match the frozen identity")
        contract_hashes = {item.get("sha256") for item in frozen_identity.get("contracts") or []}
        if contract_sha256 not in contract_hashes:
            raise ValueError("input contract does not match the frozen identity")
    plan = None
    batches = []
    checkpoint_dir = args.checkpoint_dir.resolve() if args.checkpoint_dir else None
    if args.batch_size or args.resume:
        try:
            contract_payload = _load_json(contract_path)
            routes = contract_payload.get("route_inventory") or contract_payload.get("routes") or []
            controls = contract_payload.get("control_inventory") or ((contract_payload.get("interaction_contract") or {}).get("controls") or [])
            analyzer_hash = None
            try:
                from inventory import analyzer_implementation_sha256 as _analyzer_hash
                analyzer_hash = _analyzer_hash()
            except Exception:
                analyzer_hash = None
            plan = build_verification_plan(
                candidate_fingerprint=canonical_pre["sha256"],
                contract_sha256=contract_sha256 or "",
                verifier_version="webharbor-standalone-interaction-verifier-v1",
                verifier_configuration={
                    "max_pages": args.max_pages,
                    "max_depth": args.max_depth,
                    "timeout_ms": args.timeout_ms,
                    "gateway_prefix": args.gateway_prefix,
                    "route_map_timeout_seconds": args.route_map_timeout_seconds,
                },
                viewport={"width": args.viewport_width, "height": args.viewport_height},
                routes=routes,
                controls=controls,
                candidate_identity_sha256=(frozen_identity or {}).get("sha256"),
                analyzer_implementation_sha256=analyzer_hash,
            )
            batches = partition_plan(plan, args.batch_size or 1)
            if checkpoint_dir:
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(f"cannot construct exhaustive verification plan: {exc}") from exc
    process: subprocess.Popen | None = None
    stage_root: Path | None = None
    effective_workspace = candidate_workspace
    effective_site_path = site_path
    effective_contract = contract_path
    base_url = args.base_url
    resumed_closure: dict[str, Any] | None = None
    merged_checkpoint: dict[str, Any] | None = None
    existing_checkpoints: dict[str, dict[str, Any]] = {}
    try:
        # Immutable static preflight is computed once for checkpointed runs.
        # Besides feeding every delta worker, this catches a protocol-wide
        # identity failure before any browser batches are launched.
        if plan is not None and checkpoint_dir is not None and not _shared_preflight_valid(
            checkpoint_dir / "shared-preflight", plan
        ):
            try:
                from inventory import inventory_site as _inventory_site
                preflight_inventory = _inventory_site(
                    site_path,
                    site=args.site,
                    timeout_seconds=max(1, args.route_map_timeout_seconds),
                    selected_root=site_path.as_posix(),
                    dependency_path=args.dependency_path,
                )
                shared_root = checkpoint_dir / "shared-preflight"
                shared_root.mkdir(parents=True, exist_ok=True)
                atomic_json(shared_root / "code-interaction-inventory.json", preflight_inventory)
                shutil.copyfile(contract_path, shared_root / "interaction-contract.json")
                atomic_json(shared_root / "route-map.json", preflight_inventory.get("actual_route_map", {}))
                shared_refs = {
                    name: shared_artifact_ref(shared_root / name)
                    for name in SHARED_PREFLIGHT_ARTIFACTS
                }
                atomic_json(shared_root / "preflight-manifest.json", {
                    "schema": "webharbor-interaction-verification-preflight-v1",
                    "candidate_sha256": plan.get("candidate_sha256"),
                    "contract_sha256": plan.get("contract_sha256"),
                    "candidate_identity_sha256": plan.get("candidate_identity_sha256"),
                    "analyzer_implementation_sha256": plan.get("analyzer_implementation_sha256"),
                    "shared_artifacts": shared_refs,
                })
                active_controls = [
                    item for item in preflight_inventory.get("controls", [])
                    if item.get("active_surface", True)
                ]
                if active_controls and all(not item.get("identity_selector") and not item.get("selector") for item in active_controls):
                    raise RuntimeError(
                        "global preflight failed: all active source controls lack deterministic identity selectors"
                    )
            except RuntimeError:
                raise
            except Exception:
                # Route-map/import failures remain normal verifier evidence and
                # are handled by the browser verifier; only the explicit
                # all-controls identity failure is a global stop.
                pass
        if plan is not None and checkpoint_dir is not None:
            _bind_shared_artifacts(plan, checkpoint_dir / "shared-preflight")
            batches = partition_plan(plan, args.batch_size or 1)
        if args.resume and plan is not None and checkpoint_dir is not None:
            existing_checkpoints, _checkpoint_errors = load_valid_checkpoints(checkpoint_dir, batches)
            if len(existing_checkpoints) == len(batches):
                merged_checkpoint = merge_checkpoints(plan, batches, existing_checkpoints.values())
                first = next(iter(existing_checkpoints.values()))
                candidate_result = first.get("result") or {}
                resumed_closure = candidate_result.get("closure") if isinstance(candidate_result, dict) else None
                shared_closure = checkpoint_dir / "shared-preflight" / "interaction-closure.json"
                if not isinstance(resumed_closure, dict) and shared_closure.is_file():
                    resumed_closure = _load_json(shared_closure)
                if not isinstance(resumed_closure, dict):
                    resumed_closure = {"status": merged_checkpoint["status"], "failures": merged_checkpoint["failures"]}
                status = merged_checkpoint["status"]
                failures = list(merged_checkpoint["failures"])
                repair = {
                    "status": "empty" if status == "passed" else "repair_required",
                    "failure_count": len(failures),
                    "group_count": len(failures),
                }
            # Any missing, stale, or corrupt checkpoint is ignored and causes a
            # fresh exhaustive execution; it is never silently treated as proof.
        if resumed_closure is None:
            if args.launch_config:
                launch = _load_json(args.launch_config.resolve())
                command = launch.get("command")
                launch_cwd = Path(str(launch.get("cwd") or ""))
                launch_base_url = launch.get("base_url")
                if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
                    raise ValueError("launch.command must be a non-empty string list")
                if not launch_cwd.is_dir() or not isinstance(launch_base_url, str):
                    raise ValueError("launch.cwd and launch.base_url are required")
                temp_parent = args.temp_dir.resolve() if args.temp_dir else None
                if temp_parent:
                    temp_parent.mkdir(parents=True, exist_ok=True)
                stage_parent = Path(tempfile.mkdtemp(prefix="interaction-verifier-", dir=temp_parent))
                stage_root = stage_parent / candidate_workspace.name
                relative_site = site_path.relative_to(candidate_workspace)
                effective_workspace = stage_root
                effective_site_path = stage_root / relative_site
                effective_site_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(site_path, effective_site_path, symlinks=True)
                if _inside(contract_path, site_path):
                    effective_contract = effective_site_path / contract_path.relative_to(site_path)
                else:
                    effective_contract = stage_root / ".standalone-site-contract.json"
                    shutil.copy2(contract_path, effective_contract)
                _stage_contract_sources(_load_json(effective_contract), candidate_workspace, stage_root, site_path)
                replacement = str(candidate_workspace)
                staged = str(stage_root)
                command = [item.replace(replacement, staged) for item in command]
                cwd_text = str(launch_cwd).replace(replacement, staged)
                launch_env = {"PATH": os.environ.get("PATH", "")}
                for key, value in (launch.get("env") or {}).items():
                    if not isinstance(key, str) or not isinstance(value, str):
                        raise TypeError("launch.env must map strings to strings")
                    launch_env[key] = value
                process = _launch(command, Path(cwd_text), launch_env, launch_base_url, args.launch_timeout)
                base_url = launch_base_url
            if not isinstance(base_url, str) or not base_url:
                raise ValueError("exactly one of --base-url or --launch-config must supply base URL")
            verify = _load_verifier(args.dependency_path)
            def verify_into(target: Path, *, assigned_ids=None, stage: str = "authoritative"):
                kwargs = dict(
                    workspace=effective_workspace,
                    site=args.site,
                    base_url=base_url,
                    contract_path=effective_contract,
                    evidence_dir=target,
                    max_pages=args.max_pages,
                    max_depth=args.max_depth,
                    timeout_ms=args.timeout_ms,
                    gateway_prefix=args.gateway_prefix,
                    route_map_timeout_seconds=args.route_map_timeout_seconds,
                    candidate_site_path=effective_site_path,
                    viewport={"width": args.viewport_width, "height": args.viewport_height} if args.viewport_width and args.viewport_height else None,
                    browser_path=args.browser_path.resolve() if args.browser_path else None,
                    dependency_path=args.dependency_path.resolve() if args.dependency_path else None,
                )
                # New verifiers can consume immutable preflight output and a
                # bounded route/control assignment.  Keep the adapter
                # compatible with older injected test workers/callers.
                try:
                    return verify(
                        **kwargs,
                        assigned_ids=list(assigned_ids) if assigned_ids is not None else None,
                        shared_artifact_dir=(checkpoint_dir / "shared-preflight") if checkpoint_dir else None,
                        stage=stage,
                    )
                except TypeError as exc:
                    if "unexpected keyword" not in str(exc):
                        raise
                    return verify(**kwargs)

            if plan is not None and checkpoint_dir is not None:
                checkpoint_values = list(existing_checkpoints.values())
                artifact_source: Path | None = None
                repair = {"status": "empty", "failure_count": 0, "group_count": 0}
                for batch in batches:
                    if batch["batch_id"] in existing_checkpoints:
                        continue
                    batch_evidence = checkpoint_dir / "deltas" / batch["batch_id"]
                    # Static preflight has already been materialized above;
                    # every browser worker is a bounded delta. The only full
                    # artifact emission happens in _emit_authoritative_merge.
                    batch_stage = "delta"
                    try:
                        _, batch_closure, batch_repair = verify_into(
                            batch_evidence,
                            assigned_ids=batch["assigned_ids"],
                            stage=batch_stage,
                        )
                    except Exception as exc:  # noqa: BLE001 - one worker failure must remain bounded to its batch
                        batch_closure = {
                            "status": "failed",
                            "failures": [{
                                "code": "terminal_error",
                                "message": str(exc),
                                "batch_id": batch["batch_id"],
                            }],
                        }
                        batch_repair = {"status": "repair_required", "failure_count": 1, "group_count": 1}
                    shared_refs = plan.get("shared_artifacts", {})
                    repair = batch_repair
                    assigned = set(batch["assigned_ids"])
                    assigned_control_names = {
                        item["id"] for item in batch.get("assigned", []) if item.get("kind") == "control"
                    }
                    assigned_route_paths = {
                        item.get("route") for item in batch.get("assigned", []) if item.get("kind") == "route"
                    }
                    assigned_source_paths = {
                        item.get("source_path") or item.get("route")
                        for item in batch.get("assigned", [])
                        if item.get("kind") == "control"
                    }
                    bounded_outcomes = [
                        item for item in (batch_closure.get("control_outcomes") or [])
                        if f"control:{item.get('id')}" in assigned
                    ]
                    bounded_routes = [
                        item for item in (batch_closure.get("page_archetypes") or [])
                        if f"route:{item.get('route_id')}" in assigned
                    ]
                    bounded_failures = [
                        item for item in (batch_closure.get("failures") or [])
                        if (
                            item.get("control_id") in assigned_control_names
                            or item.get("route") in (assigned_route_paths | assigned_source_paths)
                            or (item.get("control_id") is None and item.get("route") is None)
                            or item.get("code") in {"terminal_error", "candidate_mutated"}
                        )
                    ]
                    if "control_outcomes" in batch_closure or "page_archetypes" in batch_closure:
                        observed_control_ids = {item.get("id") for item in bounded_outcomes}
                        observed_route_ids = {item.get("route_id") for item in bounded_routes}
                        for assigned_item in batch.get("assigned", []):
                            assigned_key = assigned_item.get("id")
                            observed = (
                                assigned_key in observed_control_ids
                                if assigned_item.get("kind") == "control"
                                else assigned_key in observed_route_ids
                            )
                            if not observed:
                                bounded_failures.append({
                                    "code": "assignment_unproved",
                                    "batch_id": batch["batch_id"],
                                    "assigned_id": f"{assigned_item.get('kind')}:{assigned_key}",
                                })
                    batch_status = "passed" if not bounded_failures and all(
                        item.get("passed", True) for item in bounded_outcomes
                    ) and all(item.get("passed", True) for item in bounded_routes) else "failed"
                    batch_result = {
                        "status": batch_status,
                        "assigned_route_control_ids": list(batch["assigned_ids"]),
                        "outcomes": bounded_outcomes,
                        "routes": bounded_routes,
                        "failures": bounded_failures,
                        "shared_artifacts": shared_refs,
                    }
                    checkpoint_values.append(write_checkpoint(
                        checkpoint_dir / f"{batch['batch_id']}.json",
                        batch,
                        status=batch_status,
                        result=batch_result,
                    ))
                merged_checkpoint = merge_checkpoints(plan, batches, checkpoint_values)
                ordered_checkpoints = {
                    item["batch_id"]: item for item in checkpoint_values
                }
                last_checkpoint = next(
                    (ordered_checkpoints[batch["batch_id"]] for batch in reversed(batches) if batch["batch_id"] in ordered_checkpoints),
                    None,
                )
                resumed_closure = ((last_checkpoint or {}).get("result") or {}).get("closure")
                if not isinstance(resumed_closure, dict) and artifact_source:
                    closure_path = artifact_source / "interaction-closure.json"
                    if closure_path.is_file():
                        resumed_closure = _load_json(closure_path)
                if not isinstance(resumed_closure, dict):
                    resumed_closure = {"status": "failed", "failures": []}
                batch_failures = [
                    failure
                    for checkpoint in checkpoint_values
                    for failure in ((checkpoint.get("result") or {}).get("failures") or [])
                ]
                failures = [*batch_failures, *merged_checkpoint["failures"]]
                status = merged_checkpoint["status"]
                if artifact_source is None and (checkpoint_dir / "shared-preflight").is_dir():
                    artifact_source = checkpoint_dir / "shared-preflight"
                resumed_closure = _emit_authoritative_merge(
                    plan=plan,
                    batches=batches,
                    checkpoints=checkpoint_values,
                    evidence_dir=evidence_dir,
                    contract_path=contract_path,
                    shared_root=checkpoint_dir / "shared-preflight",
                    site_root=effective_site_path,
                    base_url=base_url,
                    site=args.site,
                    browser_path=args.browser_path,
                    dependency_path=args.dependency_path,
                    merge_failures=[*batch_failures, *merged_checkpoint["failures"]],
                )
                repair_path = evidence_dir / "builder-repair-package.json"
            else:
                _, closure, repair = verify_into(evidence_dir)
                status = closure["status"]
                failures = closure.get("failures", [])
                repair_path = evidence_dir / "builder-repair-package.json"
                resumed_closure = closure
    except (ContractError, ValueError, RuntimeError, OSError) as exc:
        status = "invalid_contract" if isinstance(exc, ContractError) else "failed"
        failures = [{"code": "terminal_error", "message": str(exc)}]
        repair = _error_repair(
            args.site,
            str(exc),
            fingerprint={
                "algorithm": "webanything-site-tree-v1",
                "sha256": canonical_pre["sha256"],
                "candidate_tree_sha256": canonical_pre["sha256"],
                "contract_sha256": contract_sha256,
                "hash_status": "computed",
            },
            parameters={"contract": contract_path.as_posix(), "site": args.site},
        )
        repair_path = evidence_dir / "builder-repair-package.json"
        atomic_json(repair_path, repair)
    except Exception as exc:  # noqa: BLE001 - terminal evidence must report browser/runtime failures
        status = "failed"
        failures = [{"code": "terminal_error", "message": str(exc)}]
        repair = _error_repair(
            args.site,
            str(exc),
            fingerprint={
                "algorithm": "webanything-site-tree-v1",
                "sha256": canonical_pre["sha256"],
                "candidate_tree_sha256": canonical_pre["sha256"],
                "contract_sha256": contract_sha256,
                "hash_status": "computed",
            },
            parameters={"contract": contract_path.as_posix(), "site": args.site},
        )
        repair_path = evidence_dir / "builder-repair-package.json"
        atomic_json(repair_path, repair)
    finally:
        _terminate(process)
        if stage_root is not None:
            shutil.rmtree(stage_root.parent, ignore_errors=True)
    post = _tree_identity(site_path)
    unchanged = pre == post
    if not unchanged:
        status = "candidate_mutated"
        failures = [*failures, {"code": "candidate_mutated", "message": "candidate site changed during verification"}]
    if plan is not None and checkpoint_dir is not None and resumed_closure is not None and merged_checkpoint is None:
        checkpoint_values = list(existing_checkpoints.values())
        for batch in batches:
            if batch["batch_id"] in existing_checkpoints:
                continue
            batch_result = {
                "status": status if status in {"passed", "failed"} else "failed",
                "assigned_route_control_ids": list(batch["assigned_ids"]),
                "closure": resumed_closure,
            }
            checkpoint_values.append(write_checkpoint(
                checkpoint_dir / f"{batch['batch_id']}.json",
                batch,
                status=batch_result["status"],
                result=batch_result,
            ))
        merged_checkpoint = merge_checkpoints(plan, batches, checkpoint_values)
        if merged_checkpoint["status"] != "passed":
            status = "failed"
            failures = [*failures, *merged_checkpoint["failures"]]
    # A resume that reused every valid checkpoint bypasses the worker loop.
    # Materialize the same authoritative closure/diff/inventory artifacts as a
    # fresh checkpointed run, without rerunning any browser batch.
    if plan is not None and checkpoint_dir is not None and merged_checkpoint is not None and not (evidence_dir / "interaction-closure.json").is_file():
        merge_base_url = base_url
        if not merge_base_url and args.launch_config:
            try:
                merge_base_url = _load_json(args.launch_config.resolve()).get("base_url")
            except (OSError, ValueError, TypeError):
                merge_base_url = ""
        resumed_closure = _emit_authoritative_merge(
            plan=plan,
            batches=batches,
            checkpoints=list(existing_checkpoints.values()),
            evidence_dir=evidence_dir,
            contract_path=contract_path,
            shared_root=checkpoint_dir / "shared-preflight",
            site_root=site_path,
            base_url=str(merge_base_url or ""),
            site=args.site,
            browser_path=args.browser_path,
            dependency_path=args.dependency_path,
            merge_failures=failures,
        )
    # The authoritative merge owns the durable repair package.  Reload it
    # before writing terminal-result.json so resumptions and multi-batch runs
    # cannot report stale metadata from the last worker batch.
    authoritative_repair_path = evidence_dir / "builder-repair-package.json"
    if authoritative_repair_path.is_file():
        try:
            authoritative_repair = _load_json(authoritative_repair_path)
        except (OSError, ValueError, TypeError):
            authoritative_repair = None
        if isinstance(authoritative_repair, dict):
            repair = authoritative_repair
    result = {
        "schema_version": 1,
        "terminal_result": "webharbor-interaction-verifier-terminal-v1",
        "status": status,
        "site": args.site,
        "candidate": {
            "workspace": candidate_workspace.as_posix(),
            "site_path": site_path.as_posix(),
            "pre_hash": pre,
            "post_hash": post,
            "unchanged": unchanged,
        },
        "input_contract": {"path": contract_path.as_posix(), "sha256": contract_sha256},
        "candidate_identity_sha256": frozen_identity.get("sha256") if frozen_identity else None,
        "candidate_tree_sha256": ((frozen_identity.get("tree") or {}).get("sha256") if frozen_identity else canonical_pre["sha256"]),
        "surface": {"base_url": base_url, "gateway_prefix": args.gateway_prefix},
        "budgets": {"max_pages": args.max_pages, "max_depth": args.max_depth, "timeout_ms": args.timeout_ms},
        "browser": {"path": args.browser_path.as_posix() if args.browser_path else None, "dependency_path": args.dependency_path.as_posix() if args.dependency_path else None},
        "artifacts": _artifact_hashes(evidence_dir),
        "repair_package": {"path": "builder-repair-package.json", "status": repair.get("status", "repair_required"), "group_count": repair.get("group_count", 0)},
        "failures": failures,
    }
    if (evidence_dir / "interaction-closure.json").is_file():
        result["closure"] = {
            "path": "interaction-closure.json",
            "status": status if status in {"passed", "failed"} else "failed",
        }
    if plan is not None:
        result["exhaustive"] = {
            "status": "passed" if merged_checkpoint and merged_checkpoint.get("status") == "passed" else "failed",
            "plan": plan,
            "checkpoint_dir": checkpoint_dir.as_posix() if checkpoint_dir else None,
            "batch_size": args.batch_size,
            "resume": bool(args.resume),
            "merge": merged_checkpoint,
        }
    else:
        result["exhaustive"] = {
            "status": "not_requested",
            "plan": None,
            "checkpoint_dir": None,
            "batch_size": None,
            "resume": False,
            "merge": None,
        }
    atomic_json(evidence_dir / "terminal-result.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-workspace", required=True, type=Path)
    parser.add_argument("--site-path", required=True, type=Path)
    parser.add_argument("--site", required=True)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--candidate-freeze", type=Path, help="Frozen candidate identity to bind into terminal evidence.")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base-url")
    source.add_argument("--launch-config", type=Path)
    parser.add_argument("--viewport-width", type=int)
    parser.add_argument("--viewport-height", type=int)
    parser.add_argument("--gateway-prefix")
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--dependency-path", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-depth", type=int)
    parser.add_argument("--timeout-ms", type=int, default=10_000)
    parser.add_argument("--route-map-timeout-seconds", type=int, default=30)
    parser.add_argument("--launch-timeout", type=float, default=15.0)
    parser.add_argument(
        "--batch-size",
        type=int,
        help="Partition the complete ordered route/control plan into batches of this many items.",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        help="Directory for atomic plan-bound batch checkpoints (required with --batch-size or --resume).",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Reuse only valid terminal checkpoints matching the exact plan identity.",
    )
    args = parser.parse_args()
    if (args.batch_size or args.resume) and not args.checkpoint_dir:
        parser.error("--checkpoint-dir is required with --batch-size or --resume")
    if args.batch_size is not None and args.batch_size < 1:
        parser.error("--batch-size must be positive")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except (ValueError, OSError) as exc:
        # Invalid-input and global preflight failures still receive complete,
        # hash-bound terminal evidence.  Readable inputs are fingerprinted
        # before writing the repair package so immutability is meaningful.
        evidence_dir = args.evidence_dir.resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        site_path = args.site_path.resolve()
        workspace = args.candidate_workspace.resolve()
        try:
            pre_tree = _tree_identity(site_path) if site_path.is_dir() else {"error": "candidate site is unreadable"}
            post_tree = _tree_identity(site_path) if site_path.is_dir() else pre_tree
            canonical_tree = _canonical_candidate_tree(site_path) if site_path.is_dir() else None
        except OSError as hash_error:
            pre_tree = {"error": str(hash_error)}
            post_tree = pre_tree
            canonical_tree = None
        unchanged = bool(canonical_tree and pre_tree == post_tree)
        try:
            contract_hash = sha256_file(args.contract.resolve()) if args.contract.resolve().is_file() else None
        except OSError:
            contract_hash = None
        failure_message = str(exc)
        if canonical_tree:
            candidate_digest = canonical_tree["sha256"]
            hash_status = "computed"
        else:
            candidate_digest = sha256_bytes(failure_message.encode("utf-8"))
            hash_status = "unavailable"
        fingerprint = {
            "algorithm": "webanything-site-tree-v1",
            "sha256": candidate_digest,
            "candidate_tree_sha256": canonical_tree["sha256"] if canonical_tree else None,
            "contract_sha256": contract_hash,
            "hash_status": hash_status,
            "hash_failure": None if canonical_tree else {"code": "candidate_hash_unavailable", "message": "candidate site could not be read"},
        }
        repair = _error_repair(
            args.site,
            failure_message,
            fingerprint=fingerprint,
            parameters={
                "candidate_workspace": workspace.as_posix(),
                "site_path": site_path.as_posix(),
                "contract": args.contract.resolve().as_posix(),
                "evidence_dir": evidence_dir.as_posix(),
            },
        )
        atomic_json(evidence_dir / "builder-repair-package.json", repair)
        candidate_tree_sha = canonical_tree["sha256"] if canonical_tree else candidate_digest
        candidate_identity_sha256 = None
        if args.candidate_freeze and args.candidate_freeze.resolve().is_file():
            try:
                candidate_identity_sha256 = _validate_frozen_identity(
                    _load_json(args.candidate_freeze.resolve()),
                    site=args.site,
                ).get("sha256")
            except (OSError, ValueError, TypeError):
                candidate_identity_sha256 = None
        result = {
            "schema_version": 1,
            "terminal_result": "webharbor-interaction-verifier-terminal-v1",
            "status": "invalid_input",
            "site": args.site,
            "candidate": {
                "workspace": workspace.as_posix(),
                "site_path": site_path.as_posix(),
                "pre_hash": pre_tree,
                "post_hash": post_tree,
                "unchanged": unchanged,
            },
            "candidate_identity_sha256": candidate_identity_sha256,
            "candidate_tree_sha256": candidate_tree_sha,
            "input_contract": {"path": args.contract.resolve().as_posix(), "sha256": contract_hash},
            "surface": {"base_url": args.base_url, "gateway_prefix": args.gateway_prefix},
            "budgets": {"max_pages": args.max_pages, "max_depth": args.max_depth, "timeout_ms": args.timeout_ms},
            "browser": {"path": args.browser_path.as_posix() if args.browser_path else None, "dependency_path": args.dependency_path.as_posix() if args.dependency_path else None},
            "artifacts": {"builder-repair-package.json": {"path": (evidence_dir / "builder-repair-package.json").as_posix(), "sha256": sha256_file(evidence_dir / "builder-repair-package.json")}},
            "repair_package": {"path": "builder-repair-package.json", "status": repair.get("status", "repair_required"), "group_count": repair.get("group_count", 0)},
            "failures": [{"code": "invalid_input", "message": str(exc)}],
            "exhaustive": {"status": "not_requested", "plan": None, "checkpoint_dir": None, "batch_size": None, "resume": False, "merge": None},
        }
        atomic_json(evidence_dir / "terminal-result.json", result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
