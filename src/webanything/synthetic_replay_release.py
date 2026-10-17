"""Fail-closed release gate for full-capture synthetic replay candidates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .release import ReleaseError, atomic_json, load_candidate_freeze, private_evidence_scan, sha256_file, tree_identity
from .synthetic_replay import SyntheticReplayContractError, load_synthetic_replay_contract
from .synthetic_visual import SyntheticVisualError, load_visual_reference


REPLAY_REQUIRED_GATES = {
    "replay_contract": "contract",
    "capture_bundle": "capture_bundle",
    "product_coverage": "product_coverage",
    "route_interaction": "interactions",
    "auth_workflows": "auth",
    "task_execution": "tasks",
    "semantic_task_execution": "semantic_tasks",
    "sqlite_reset": "reset",
    "state_persistence": "persistence",
    "determinism": "determinism",
    "resource_privacy": "resource_privacy",
    "proxy_prefix": "proxy_prefix",
    "concurrency_isolation": "concurrency_isolation",
    "candidate_immutability": "candidate_immutability",
    "visual_fidelity": "visual_fidelity",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(path: Path, schema: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ReleaseError(f"unsupported {label}: {path}")
    return value


def release_synthetic_replay_candidate(
    *,
    site: str,
    site_path: Path,
    contract_path: Path,
    verification_path: Path,
    visual_reference_path: Path,
    visual_verification_path: Path,
    freeze_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    site_path = site_path.resolve()
    contract_path = contract_path.resolve()
    verification_path = verification_path.resolve()
    visual_reference_path = visual_reference_path.resolve()
    visual_verification_path = visual_verification_path.resolve()
    freeze_path = freeze_path.resolve()
    output_dir = output_dir.resolve()
    reasons: list[dict[str, Any]] = []

    def reject(code: str, message: str, **details: Any) -> None:
        reasons.append({"code": code, "message": message, **details})

    if not site_path.is_dir():
        reject("candidate_missing", f"candidate site does not exist: {site_path}")
    try:
        contract = load_synthetic_replay_contract(contract_path)
    except SyntheticReplayContractError as exc:
        contract = {}
        reject("replay_contract_invalid", str(exc))
    if contract and contract.get("site_id") != site:
        reject("site_mismatch", "replay contract names another site")
    if contract and (contract.get("coverage") or {}).get("release_tier") != "product_complete":
        reject(
            "product_coverage_incomplete",
            "production release requires coverage.release_tier=product_complete",
        )
    reference_hash = sha256_file(visual_reference_path) if visual_reference_path.is_file() else None
    try:
        reference = load_visual_reference(visual_reference_path)
    except SyntheticVisualError as exc:
        reference = {}
        reject("visual_reference_invalid", str(exc))
    visual_contract = contract.get("visual_reference") if contract else {}
    if visual_contract.get("sha256") != reference_hash or visual_contract.get("reference_sha256") != reference.get("reference_sha256"):
        reject("visual_reference_contract_mismatch", "contract does not bind this visual reference")

    identity: dict[str, Any] = {}
    try:
        freeze = load_candidate_freeze(freeze_path)
        identity = freeze.get("identity") or {}
        if freeze.get("state") != "frozen":
            reject("candidate_not_frozen", "candidate freeze state is not frozen")
        configuration = identity.get("configuration") or {}
        if configuration.get("site") not in (None, site) or configuration.get("build_mode") != "benchmark_synthetic_replay":
            reject("candidate_configuration_mismatch", "freeze is bound to another mode or site")
        if site_path.is_dir() and tree_identity(site_path) != (identity.get("tree") or {}):
            reject("candidate_mutated", "candidate differs from its frozen tree")
        contract_hashes = {item.get("sha256") for item in identity.get("contracts") or []}
        reference_hashes = {item.get("sha256") for item in identity.get("references") or []}
        if contract_path.is_file() and sha256_file(contract_path) not in contract_hashes:
            reject("contract_identity_mismatch", "freeze does not bind the replay contract")
        if reference_hash not in reference_hashes:
            reject("visual_reference_identity_mismatch", "freeze does not bind the visual reference")
    except ReleaseError as exc:
        reject("candidate_freeze_invalid", str(exc))

    try:
        evidence = _read(verification_path, "webanything-synthetic-replay-verification-v1", "replay verification")
    except ReleaseError as exc:
        evidence = {}
        reject("verification_invalid", str(exc))
    if evidence:
        if evidence.get("status") != "passed" or evidence.get("failures") != []:
            reject("verification_failed", "replay verification is not passing")
        if evidence.get("site") != site or evidence.get("build_mode") != "benchmark_synthetic_replay":
            reject("verification_site_mismatch", "replay verification names another site or mode")
        if evidence.get("candidate_identity_sha256") != identity.get("sha256"):
            reject("verification_identity_mismatch", "replay verification is stale")
        if evidence.get("candidate_sha256") != (identity.get("tree") or {}).get("sha256"):
            reject("verification_candidate_mismatch", "replay verification binds another candidate tree")
        if contract_path.is_file() and evidence.get("contract_sha256") != sha256_file(contract_path):
            reject("verification_contract_mismatch", "replay verification binds another contract")

    try:
        visual = _read(visual_verification_path, "webanything-synthetic-visual-verification-v1", "visual verification")
    except ReleaseError as exc:
        visual = {}
        reject("visual_verification_invalid", str(exc))
    if visual:
        if visual.get("status") != "passed" or visual.get("failures") != []:
            reject("visual_verification_failed", "visual verification is not passing")
        if visual.get("site") != site or visual.get("build_mode") != "benchmark_synthetic_replay":
            reject("visual_verification_site_mismatch", "visual verification names another site or mode")
        if visual.get("comparison_mode") != "source_visible":
            reject("visual_verification_mode_mismatch", "replay visual verification must use source_visible")
        if visual.get("candidate_identity_sha256") != identity.get("sha256") or visual.get("candidate_sha256") != (identity.get("tree") or {}).get("sha256"):
            reject("visual_verification_identity_mismatch", "visual verification is stale")
        if visual.get("contract_sha256") != (sha256_file(contract_path) if contract_path.is_file() else None):
            reject("visual_verification_contract_mismatch", "visual verification binds another contract")
        if visual.get("reference_manifest_sha256") != reference_hash or visual.get("visual_reference_sha256") != reference.get("reference_sha256"):
            reject("visual_verification_reference_mismatch", "visual verification binds another reference")
        if (visual.get("network") or {}).get("unexpected_external_requests") != 0:
            reject("visual_verification_network_failed", "visual verifier observed external requests")
        candidate_evidence = visual.get("candidate") or {}
        if candidate_evidence.get("unchanged") is not True:
            reject("visual_verification_candidate_mutated", "visual verifier changed the candidate")

    checks = evidence.get("checks") if evidence else {}
    required_results: dict[str, dict[str, Any]] = {}
    for gate, check in REPLAY_REQUIRED_GATES.items():
        passed = visual.get("status") == "passed" if check == "visual_fidelity" else isinstance(checks, dict) and checks.get(check) == "passed"
        required_results[gate] = {"status": "passed" if passed else "failed", "artifacts": 1 if passed else 0}
        if not passed:
            reject("required_gate_missing", f"required replay gate failed: {gate}", gate=gate)
    reset = evidence.get("reset") or {}
    if reset.get("byte_identical") is not True or reset.get("seed_sha256") != reset.get("first_sha256") or reset.get("first_sha256") != reset.get("second_sha256"):
        reject("reset_hash_mismatch", "reset evidence is not byte-identical to the seed")
    remaining_private = private_evidence_scan(site_path) if site_path.is_dir() else []
    if remaining_private:
        reject("private_evidence_present", "private capture/auth evidence remains in candidate", paths=remaining_private[:10])

    gate = {
        "schema": "webanything-release-gate-result-v1",
        "created_at": _utc_now(),
        "site": site,
        "profile": "benchmark_synthetic_replay",
        "build_mode": "benchmark_synthetic_replay",
        "status": "failed" if reasons else "passed",
        "candidate_identity_sha256": identity.get("sha256"),
        "candidate_sha256": (identity.get("tree") or {}).get("sha256"),
        "reason_codes": reasons,
        "required_gates": required_results,
    }
    manifest_path = output_dir / "release-manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    gate_path = output_dir / "release-gate-result.json"
    atomic_json(gate_path, gate)
    if reasons:
        raise ReleaseError("synthetic replay release gate failed: " + ", ".join(item["code"] for item in reasons))

    candidate = identity["tree"]
    manifest = {
        "schema": "webanything-release-manifest-v2",
        "created_at": _utc_now(),
        "site": site,
        "profile": "benchmark_synthetic_replay",
        "build_mode": "benchmark_synthetic_replay",
        "candidate_sha256": candidate["sha256"],
        "candidate": candidate,
        "candidate_identity": identity,
        "seed_reset": {
            "status": "passed",
            "state_backend": "SQLite",
            "seed_path": contract["storage"]["seed_path"],
            "runtime_path": contract["storage"]["runtime_path"],
            "seed_sha256": reset["seed_sha256"],
            "runtime_sha256": reset["second_sha256"],
        },
        "privacy_scan": {"status": "passed", "remaining_private_paths": [], "removed_paths": []},
        "verification": {
            "status": "passed",
            "artifacts": [
                {"gate": "synthetic_replay_acceptance", "artifact": verification_path.name, "sha256": sha256_file(verification_path), "schema": evidence["schema"], "candidate_identity_sha256": identity["sha256"], "candidate_sha256": candidate["sha256"]},
                {"gate": "synthetic_replay_visual_fidelity", "artifact": visual_verification_path.name, "sha256": sha256_file(visual_verification_path), "schema": visual["schema"], "candidate_identity_sha256": identity["sha256"], "candidate_sha256": candidate["sha256"]},
            ],
        },
        "visual_reference": {
            "status": "passed",
            "manifest_sha256": reference_hash,
            "reference_sha256": reference["reference_sha256"],
            "raw_material_persisted": False,
            "comparison_mode": "source_visible",
        },
        "concurrency": contract["concurrency"],
        "release_gate": {"path": "release-gate-result.json", "sha256": sha256_file(gate_path)},
        "deployment_binding": {
            "algorithm": candidate["algorithm"],
            "candidate_sha256": candidate["sha256"],
            "candidate_identity_sha256": identity["sha256"],
            "synthetic_contract_sha256": sha256_file(contract_path),
            "visual_reference_manifest_sha256": reference_hash,
            "visual_reference_sha256": reference["reference_sha256"],
            "capture_bundle_sha256": contract["capture_manifest"]["bundle_sha256"],
            "seed_sha256": contract["storage"]["seed_sha256"],
        },
        "release_status": "frozen",
    }
    atomic_json(manifest_path, manifest)
    return manifest
