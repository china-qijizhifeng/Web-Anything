"""Fail-closed release gate for synthetic benchmark candidates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .release import (
    ReleaseError,
    atomic_json,
    load_candidate_freeze,
    private_evidence_scan,
    sha256_file,
    tree_identity,
)
from .synthetic import SyntheticContractError, contract_content_mode, load_synthetic_contract
from .synthetic_visual import SyntheticVisualError, load_visual_reference


SYNTHETIC_REQUIRED_GATES = {
    "synthetic_contract": "contract",
    "task_execution": "tasks",
    "browser_state_reset": "reset",
    "state_persistence": "persistence",
    "determinism": "determinism",
    "resource_privacy": "resource_privacy",
    "content_policy": "content_policy",
    "candidate_immutability": "candidate_immutability",
    "visual_fidelity": "visual_fidelity",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_evidence(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"invalid synthetic verification evidence: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "webanything-synthetic-verification-v1":
        raise ReleaseError(f"unsupported synthetic verification evidence: {path}")
    return value


def _read_visual_evidence(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"invalid synthetic visual verification evidence: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "webanything-synthetic-visual-verification-v1":
        raise ReleaseError(f"unsupported synthetic visual verification evidence: {path}")
    return value


def release_synthetic_candidate(
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
        contract = load_synthetic_contract(contract_path)
    except SyntheticContractError as exc:
        contract = {}
        reject("synthetic_contract_invalid", str(exc))
    if contract and contract.get("site_id") != site:
        reject("site_mismatch", "synthetic contract names another site")
    expected_comparison_mode = (
        "source_visible" if contract and contract_content_mode(contract) == "source_fidelity" else "content_neutral"
    )
    reference_manifest_hash = sha256_file(visual_reference_path) if visual_reference_path.is_file() else None
    reference: dict[str, Any] = {}
    try:
        reference = load_visual_reference(visual_reference_path)
    except SyntheticVisualError as exc:
        reject("visual_reference_invalid", str(exc))
    visual_contract = contract.get("visual_reference") if contract else None
    if (
        not isinstance(visual_contract, dict)
        or visual_contract.get("required") is not True
        or visual_contract.get("sha256") != reference_manifest_hash
        or visual_contract.get("reference_sha256") != reference.get("reference_sha256")
    ):
        reject("visual_reference_contract_mismatch", "synthetic contract does not bind this frozen visual reference")

    identity: dict[str, Any] = {}
    try:
        freeze = load_candidate_freeze(freeze_path)
        identity = freeze.get("identity") or {}
        if freeze.get("state") != "frozen":
            reject("candidate_not_frozen", "candidate freeze state is not frozen")
        configuration = identity.get("configuration") or {}
        if configuration.get("site") not in (None, site) or configuration.get("build_mode") != "benchmark_synthetic":
            reject("candidate_configuration_mismatch", "candidate freeze is not bound to this synthetic site")
        current = tree_identity(site_path)
        if current != (identity.get("tree") or {}):
            reject("candidate_mutated", "candidate differs from its frozen identity")
        if sha256_file(contract_path) not in {item.get("sha256") for item in identity.get("contracts") or []}:
            reject("contract_identity_mismatch", "synthetic contract is not bound by the candidate freeze")
        if reference_manifest_hash not in {item.get("sha256") for item in identity.get("references") or []}:
            reject("visual_reference_identity_mismatch", "visual reference is not bound by the candidate freeze")
    except ReleaseError as exc:
        reject("candidate_freeze_invalid", str(exc))

    evidence: dict[str, Any] = {}
    try:
        evidence = _read_evidence(verification_path)
    except ReleaseError as exc:
        reject("evidence_invalid", str(exc))
    if evidence:
        if evidence.get("status") != "passed" or evidence.get("failures") != []:
            reject("verification_failed", "synthetic verification is not passing")
        if evidence.get("site") != site or evidence.get("build_mode") != "benchmark_synthetic":
            reject("verification_site_mismatch", "synthetic verification names another site or mode")
        if evidence.get("candidate_identity_sha256") != identity.get("sha256"):
            reject("verification_identity_mismatch", "synthetic verification has a stale candidate identity")
        if evidence.get("candidate_sha256") != (identity.get("tree") or {}).get("sha256"):
            reject("verification_candidate_mismatch", "synthetic verification has a stale candidate tree")
        if evidence.get("contract_sha256") != (sha256_file(contract_path) if contract_path.is_file() else None):
            reject("verification_contract_mismatch", "synthetic verification has a stale contract")

    visual_evidence: dict[str, Any] = {}
    try:
        visual_evidence = _read_visual_evidence(visual_verification_path)
    except ReleaseError as exc:
        reject("visual_evidence_invalid", str(exc))
    if visual_evidence:
        if (
            visual_evidence.get("status") != "passed"
            or visual_evidence.get("repairable") is not False
            or visual_evidence.get("failures") != []
        ):
            reject("visual_verification_failed", "synthetic visual verification is not passing")
        if visual_evidence.get("site") != site or visual_evidence.get("build_mode") != "benchmark_synthetic":
            reject("visual_verification_site_mismatch", "synthetic visual verification names another site or mode")
        if visual_evidence.get("comparison_mode") != expected_comparison_mode:
            reject(
                "visual_verification_mode_mismatch",
                "synthetic visual verification used the wrong content comparison mode",
            )
        if visual_evidence.get("candidate_identity_sha256") != identity.get("sha256"):
            reject("visual_verification_identity_mismatch", "synthetic visual verification has a stale candidate identity")
        if visual_evidence.get("candidate_sha256") != (identity.get("tree") or {}).get("sha256"):
            reject("visual_verification_candidate_mismatch", "synthetic visual verification has a stale candidate tree")
        if visual_evidence.get("contract_sha256") != (sha256_file(contract_path) if contract_path.is_file() else None):
            reject("visual_verification_contract_mismatch", "synthetic visual verification has a stale contract")
        if visual_evidence.get("reference_manifest_sha256") != reference_manifest_hash:
            reject("visual_verification_reference_mismatch", "synthetic visual verification has a stale reference manifest")
        if visual_evidence.get("visual_reference_sha256") != reference.get("reference_sha256"):
            reject("visual_verification_reference_identity_mismatch", "synthetic visual verification has a stale reference identity")
        visual_network = visual_evidence.get("network") or {}
        if visual_network.get("unexpected_external_requests") != 0 or visual_network.get("urls") != []:
            reject("visual_verification_network_failed", "synthetic visual verification observed external requests")
        visual_candidate = visual_evidence.get("candidate") or {}
        if (
            visual_candidate.get("unchanged") is not True
            or visual_candidate.get("pre_hash") != identity.get("tree")
            or visual_candidate.get("post_hash") != identity.get("tree")
        ):
            reject("visual_verification_candidate_mutated", "synthetic visual verification lacks immutable candidate proof")
        visual_routes = visual_evidence.get("routes")
        if not isinstance(visual_routes, list) or not visual_routes or any(
            not isinstance(route, dict) or route.get("status") != "passed" for route in visual_routes
        ):
            reject("visual_verification_routes_failed", "synthetic visual verification lacks passing route evidence")

    checks = evidence.get("checks") if evidence else {}
    required_gates = dict(SYNTHETIC_REQUIRED_GATES)
    if contract.get("interaction_policy"):
        required_gates["interaction_coverage"] = "interactions"
    required_gate_results: dict[str, dict[str, Any]] = {}
    for gate, check in required_gates.items():
        passed = (
            visual_evidence.get("status") == "passed"
            if check == "visual_fidelity"
            else isinstance(checks, dict) and checks.get(check) == "passed"
        )
        required_gate_results[gate] = {
            "status": "passed" if passed else "failed",
            "artifacts": 1 if passed else 0,
        }
        if not passed:
            reject("required_gate_missing", f"required synthetic gate failed: {gate}", gate=gate)

    remaining_private = private_evidence_scan(site_path) if site_path.is_dir() else []
    if remaining_private:
        reject("private_evidence_present", "private evidence remains in candidate", paths=remaining_private[:10])
    reset = evidence.get("reset") or {}
    if reset.get("identical") is not True or reset.get("first_sha256") != reset.get("second_sha256"):
        reject("reset_hash_mismatch", "synthetic reset hashes are not identical")

    gate_result = {
        "schema": "webanything-release-gate-result-v1",
        "created_at": utc_now(),
        "site": site,
        "profile": "benchmark_synthetic",
        "build_mode": "benchmark_synthetic",
        "status": "failed" if reasons else "passed",
        "candidate_identity_sha256": identity.get("sha256"),
        "candidate_sha256": (identity.get("tree") or {}).get("sha256"),
        "reason_codes": reasons,
        "required_gates": required_gate_results,
    }
    manifest_path = output_dir / "release-manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    gate_path = output_dir / "release-gate-result.json"
    atomic_json(gate_path, gate_result)
    if reasons:
        raise ReleaseError("synthetic release gate failed: " + ", ".join(item["code"] for item in reasons))

    candidate = identity["tree"]
    manifest = {
        "schema": "webanything-release-manifest-v2",
        "created_at": utc_now(),
        "site": site,
        "profile": "benchmark_synthetic",
        "build_mode": "benchmark_synthetic",
        "candidate_sha256": candidate["sha256"],
        "candidate": candidate,
        "candidate_identity": identity,
        "seed_reset": {
            "status": "passed",
            "state_backend": "localStorage",
            "namespace": contract["storage"]["namespace"],
            "initial_state_sha256": reset["first_sha256"],
            "reset_state_sha256": reset["second_sha256"],
        },
        "privacy_scan": {"status": "passed", "remaining_private_paths": [], "removed_paths": []},
        "verification": {
            "status": "passed",
            "artifacts": [
                {
                    "gate": "synthetic_acceptance",
                    "artifact": verification_path.name,
                    "sha256": sha256_file(verification_path),
                    "schema": evidence["schema"],
                    "candidate_identity_sha256": identity["sha256"],
                    "candidate_sha256": candidate["sha256"],
                },
                {
                    "gate": "synthetic_visual_fidelity",
                    "artifact": visual_verification_path.name,
                    "sha256": sha256_file(visual_verification_path),
                    "schema": visual_evidence["schema"],
                    "candidate_identity_sha256": identity["sha256"],
                    "candidate_sha256": candidate["sha256"],
                },
            ],
        },
        "visual_reference": {
            "status": "passed",
            "manifest_sha256": reference_manifest_hash,
            "reference_sha256": reference["reference_sha256"],
            "raw_material_persisted": False,
            "comparison_mode": expected_comparison_mode,
        },
        "release_gate": {"path": "release-gate-result.json", "sha256": sha256_file(gate_path)},
        "deployment_binding": {
            "algorithm": candidate["algorithm"],
            "candidate_sha256": candidate["sha256"],
            "candidate_identity_sha256": identity["sha256"],
            "synthetic_contract_sha256": sha256_file(contract_path),
            "visual_reference_manifest_sha256": reference_manifest_hash,
            "visual_reference_sha256": reference["reference_sha256"],
        },
        "release_status": "frozen",
    }
    atomic_json(manifest_path, manifest)
    return manifest
