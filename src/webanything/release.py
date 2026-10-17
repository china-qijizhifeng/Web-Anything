"""Fail-closed release gate for accepted WebAnything candidates."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


ACCEPTED_STATUSES = {"passed", "accepted", "automation_verified", "publicly_verified"}
TERMINAL_STATUSES = ACCEPTED_STATUSES | {"failed", "rejected", "timed_out", "invalid"}
EXCLUDED_FINGERPRINT_DIRS = {"instance", "__pycache__", ".pytest_cache"}


class ReleaseError(RuntimeError):
    """Raised when a release hard gate is not satisfied."""


def candidate_identity(
    site_path: Path,
    *,
    contract_paths: Iterable[Path] = (),
    reference_paths: Iterable[Path] = (),
    configuration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a canonical candidate identity suitable for freeze/evidence binding."""
    site_path = site_path.resolve()
    tree = tree_identity(site_path)
    contracts = []
    for path in sorted((Path(item).resolve() for item in contract_paths), key=lambda item: item.as_posix()):
        if not path.is_file():
            raise ReleaseError(f"candidate contract is missing: {path}")
        contracts.append({"sha256": sha256_file(path)})
    references = []
    for path in sorted((Path(item).resolve() for item in reference_paths), key=lambda item: item.as_posix()):
        if not path.is_file():
            raise ReleaseError(f"candidate reference is missing: {path}")
        references.append({"sha256": sha256_file(path)})
    inputs = {
        "tree_sha256": tree["sha256"],
        "contract_sha256s": [item["sha256"] for item in contracts],
        "configuration": configuration or {},
    }
    if references:
        inputs["reference_sha256s"] = [item["sha256"] for item in references]
    identity = {
        "schema": "webanything-candidate-identity-v1",
        "sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "tree": tree,
        "contracts": contracts,
        "configuration": configuration or {},
    }
    if references:
        identity["references"] = references
    return identity


def freeze_candidate(path: Path, identity: dict[str, Any], *, state: str = "frozen") -> dict[str, Any]:
    if state not in {"candidate", "frozen", "invalidated"}:
        raise ReleaseError(f"unsupported candidate freeze state: {state}")
    record = {"schema": "webanything-candidate-freeze-v1", "state": state, "identity": identity}
    atomic_json(path, record)
    return record


def load_candidate_freeze(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"invalid candidate freeze record: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "webanything-candidate-freeze-v1":
        raise ReleaseError(f"unsupported candidate freeze record: {path}")
    identity = value.get("identity") or {}
    inputs = {
        "tree_sha256": (identity.get("tree") or {}).get("sha256"),
        "contract_sha256s": [item.get("sha256") for item in identity.get("contracts") or []],
        "configuration": identity.get("configuration") or {},
    }
    if "references" in identity:
        references = identity.get("references")
        if not isinstance(references, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("sha256"), str)
            for item in references
        ):
            raise ReleaseError(f"candidate reference identities are invalid: {path}")
        inputs["reference_sha256s"] = [item.get("sha256") for item in references]
    expected = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if identity.get("schema") != "webanything-candidate-identity-v1" or identity.get("sha256") != expected:
        raise ReleaseError(f"candidate identity digest is invalid: {path}")
    return value


def invalidate_downstream_evidence(run_dir: Path, *, reason: str) -> list[str]:
    """Atomically mark old acceptance artifacts invalid before a Builder repair."""
    invalidated: list[str] = []
    for path in sorted(run_dir.rglob("*.json")):
        relative = path.relative_to(run_dir)
        if relative.parts[0] not in {"verification", "release", "deployment"} and path.name != "result.json":
            continue
        if path.name in {"webanything-run.json", "candidate-freeze.json", "repair-transition.json"}:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if any(value.get(key) in ACCEPTED_STATUSES for key in ("status", "terminal_status", "release_status")):
            value["status"] = "invalidated"
            if value.get("release_status") in ACCEPTED_STATUSES or value.get("release_status") == "frozen":
                value["release_status"] = "invalidated"
            value["invalidation"] = {"reason": reason, "source": "builder-repair-transition"}
            atomic_json(path, value)
            invalidated.append(path.relative_to(run_dir).as_posix())
    return invalidated


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_identity(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_FINGERPRINT_DIRS for part in relative.parts):
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


def verification_record(path: Path, *, identity: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"invalid verification evidence: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseError(f"verification evidence must be an object: {path}")
    statuses = [payload.get("status"), payload.get("terminal_status"), payload.get("state")]
    normalized = next((str(value).strip().casefold() for value in statuses if value), None)
    if normalized not in ACCEPTED_STATUSES:
        raise ReleaseError(f"verification evidence is not accepted: {path}: {normalized!r}")
    schema = payload.get("schema") or payload.get("tool") or payload.get("terminal_result")
    gate = None
    if schema == "webharbor-fidelity-v19":
        gate = "fidelity"
        route_results = payload.get("route_results") or payload.get("routes") or []
        homepage = any(
            isinstance(item, dict)
            and (item.get("route") or item.get("path") or item.get("local_path")) == "/"
            and item.get("status") == "passed"
            for item in route_results
        )
        if (
            payload.get("fidelity_contract_version") != 3
            or payload.get("require_complete_resources") is not True
            or payload.get("runtime_javascript_check") is not True
            or payload.get("failures") != []
            or not homepage
        ):
            raise ReleaseError(f"fidelity evidence lacks required v19 domain proof: {path}")
    elif schema == "webharbor-quality-verifier-v1":
        gate = "quality"
        components = payload.get("components") or {}
        if (
            payload.get("quality_contract_version") != 1
            or payload.get("failures") != []
            or payload.get("grade") not in {"full_fidelity", "synthetic_grounded_data"}
            or any((components.get(name) or {}).get("status") != "passed" for name in ("design", "data", "interaction"))
        ):
            raise ReleaseError(f"quality evidence is not release grade: {path}")
    elif schema == "webharbor-interaction-verifier-terminal-v1":
        gate = "interaction"
        exhaustive = payload.get("exhaustive") or {}
        merge = exhaustive.get("merge") or {}
        closure = payload.get("closure")
        if not isinstance(closure, dict) or closure.get("status") != "passed":
            raise ReleaseError(f"interaction evidence is missing a passed closure: {path}")
        if exhaustive.get("status") != "passed" or merge.get("schema") != "webharbor-interaction-verification-merge-v1" or merge.get("status") != "passed":
            raise ReleaseError(f"interaction evidence lacks exhaustive checkpoint closure: {path}")
        if merge.get("present_batch_count") != merge.get("expected_batch_count") or merge.get("covered_item_count") != merge.get("assigned_item_count"):
            raise ReleaseError(f"interaction checkpoint coverage is incomplete: {path}")
        plan = exhaustive.get("plan")
        if isinstance(plan, dict):
            if plan.get("candidate_sha256") != (identity.get("tree") or {}).get("sha256"):
                raise ReleaseError(f"interaction evidence plan candidate hash is stale: {path}")
            contract_hashes = {item.get("sha256") for item in identity.get("contracts") or []}
            if plan.get("contract_sha256") not in contract_hashes:
                raise ReleaseError(f"interaction evidence plan contract hash is stale: {path}")
            if merge.get("plan_identity") != plan.get("plan_identity"):
                raise ReleaseError(f"interaction checkpoint merge is not bound to its plan: {path}")
    elif schema == "webanything-resource-privacy-audit-v1":
        gate = "resource_privacy"
        if payload.get("unexpected_external_requests", 0) != 0 or payload.get("unresolved_external_requests") not in ([], None) or payload.get("private_evidence_paths") not in ([], None):
            raise ReleaseError(f"resource/privacy evidence is unresolved: {path}")
    elif schema == "webanything-runtime-reset-v1":
        gate = "runtime_reset"
        if payload.get("seed_sha256") != payload.get("runtime_sha256") or payload.get("byte_identical") is not True:
            raise ReleaseError(f"runtime reset evidence is not byte-identical: {path}")
    elif schema == "webanything-candidate-immutability-v1":
        gate = "candidate_immutability"
        if payload.get("unchanged") is not True or payload.get("pre_sha256") != payload.get("post_sha256"):
            raise ReleaseError(f"candidate immutability evidence failed: {path}")
    else:
        raise ReleaseError(f"unsupported verification evidence schema: {path}: {schema!r}")
    expected_identity = identity.get("sha256")
    recorded_identity = payload.get("candidate_identity_sha256")
    if recorded_identity is None and isinstance(payload.get("candidate_identity"), dict):
        recorded_identity = payload["candidate_identity"].get("sha256")
    if recorded_identity != expected_identity:
        raise ReleaseError(f"verification evidence candidate identity mismatch: {path}")
    recorded_tree = payload.get("candidate_sha256") or payload.get("candidate_tree_sha256")
    if gate == "interaction":
        candidate = payload.get("candidate") or {}
        pre_hash = candidate.get("pre_hash") or {}
        post_hash = candidate.get("post_hash") or {}
        recorded_tree = payload.get("candidate_tree_sha256") or pre_hash.get("sha256")
        if candidate.get("unchanged") is not True or pre_hash != post_hash:
            raise ReleaseError(f"verifier changed the candidate: {path}")
        contract_hashes = {item.get("sha256") for item in identity.get("contracts") or []}
        if (payload.get("input_contract") or {}).get("sha256") not in contract_hashes:
            raise ReleaseError(f"interaction evidence contract identity mismatch: {path}")
    if recorded_tree != (identity.get("tree") or {}).get("sha256"):
        raise ReleaseError(f"verification evidence candidate tree mismatch: {path}")
    if gate in {"fidelity", "quality"}:
        contract_hashes = {item.get("sha256") for item in identity.get("contracts") or []}
        if payload.get("contract_sha256") not in contract_hashes:
            raise ReleaseError(f"{gate} evidence contract identity mismatch: {path}")
    candidate = payload.get("candidate")
    if isinstance(candidate, dict) and candidate.get("unchanged") is False:
        raise ReleaseError(f"verifier changed the candidate: {path}")
    return {
        "gate": gate,
        "artifact": path.name,
        "sha256": sha256_file(path),
        "status": normalized,
        "schema": schema,
        "candidate_identity_sha256": expected_identity,
        "candidate_sha256": (identity.get("tree") or {}).get("sha256"),
        "payload": payload,
    }


def private_evidence_scan(site_path: Path) -> list[str]:
    failures: list[str] = []
    sensitive_markers = (
        "storage-state", "storage_state", "cookies", "private-upstream-screenshots",
        "auth-profile", "encrypted-auth-profile", "synthetic_visual_reference",
        "visual-reference", "raw-screenshot", "raw-dom",
    )
    for path in sorted(site_path.rglob("*")):
        relative = path.relative_to(site_path).as_posix().casefold()
        if any(marker in relative for marker in sensitive_markers):
            failures.append(path.relative_to(site_path).as_posix())
    return failures


def remove_private_paths(paths: Iterable[Path], *, allowed_root: Path) -> list[str]:
    removed: list[str] = []
    for path in paths:
        resolved = path.resolve()
        if not _inside(resolved, allowed_root):
            raise ReleaseError(f"private evidence path escapes the run root: {resolved}")
        if not resolved.exists() and not resolved.is_symlink():
            continue
        if resolved.is_dir() and not resolved.is_symlink():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
        removed.append(resolved.relative_to(allowed_root.resolve()).as_posix())
    return removed


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def release_candidate(
    *,
    site: str,
    profile: str,
    site_path: Path,
    seed_db: Path,
    runtime_db: Path,
    verification_paths: list[Path],
    output_dir: Path,
    expected_seed_sha256: str | None = None,
    private_paths: list[Path] | None = None,
    private_allowed_root: Path | None = None,
    freeze_path: Path | None = None,
    verification_dir: Path | None = None,
) -> dict[str, Any]:
    site_path = site_path.resolve()
    seed_db = seed_db.resolve()
    runtime_db = runtime_db.resolve()
    output_dir = output_dir.resolve()
    reasons: list[dict[str, Any]] = []

    def reject(code: str, message: str, **details: Any) -> None:
        reasons.append({"code": code, "message": message, **details})

    if not site_path.is_dir():
        reject("candidate_missing", f"candidate site does not exist: {site_path}")
    if not seed_db.is_file() or not _inside(seed_db, site_path / "instance_seed"):
        reject("seed_invalid", "seed database must exist under candidate instance_seed")
    if not runtime_db.is_file() or not _inside(runtime_db, site_path / "instance"):
        reject("runtime_invalid", "runtime database must exist under candidate instance")
    if private_paths:
        reject("release_gate_read_only", "release gate does not delete supplied private paths")
    identity: dict[str, Any] = {}
    freeze: dict[str, Any] = {}
    if freeze_path is None or not freeze_path.resolve().is_file():
        reject("candidate_freeze_missing", "a candidate freeze record is required")
    else:
        try:
            freeze = load_candidate_freeze(freeze_path.resolve())
            identity = freeze.get("identity") or {}
            if freeze.get("state") != "frozen":
                reject("candidate_not_frozen", f"candidate freeze state is {freeze.get('state')!r}")
            configuration = identity.get("configuration") or {}
            if configuration.get("site") not in (None, site) or configuration.get("profile") not in (None, profile):
                reject("candidate_configuration_mismatch", "candidate freeze configuration differs from the requested release")
            current = tree_identity(site_path)
            if (identity.get("tree") or {}).get("sha256") != current["sha256"] or identity.get("tree") != current:
                reject("candidate_mutated", "candidate tree differs from the frozen identity")
            if not identity.get("contracts"):
                reject("candidate_contract_identity_missing", "frozen identity has no contract/input hashes")
        except ReleaseError as exc:
            reject("candidate_freeze_invalid", str(exc))

    seed_hash = sha256_file(seed_db) if seed_db.is_file() else None
    runtime_hash = sha256_file(runtime_db) if runtime_db.is_file() else None
    if expected_seed_sha256 and seed_hash != expected_seed_sha256:
        reject("seed_hash_mismatch", f"expected {expected_seed_sha256}, got {seed_hash}")
    if seed_hash != runtime_hash:
        reject("reset_not_byte_identical", "runtime database is not byte-identical to seed")
    remaining_private = private_evidence_scan(site_path) if site_path.is_dir() else []
    if remaining_private:
        reject("private_evidence_present", "private evidence remains in candidate", paths=remaining_private[:10])

    if verification_dir is not None:
        directory = verification_dir.resolve()
        if not directory.is_dir():
            reject("evidence_directory_missing", f"verification directory does not exist: {directory}")
        else:
            verification_paths = sorted({*verification_paths, *directory.glob("*.json")}, key=lambda item: item.as_posix())
    verifications: list[dict[str, Any]] = []
    if not verification_paths:
        reject("evidence_missing", "verification evidence is required")
    elif identity:
        for path in verification_paths:
            try:
                verifications.append(verification_record(path.resolve(), identity=identity))
            except ReleaseError as exc:
                reject("evidence_invalid", str(exc), artifact=path.name)
    contract_hashes = {item.get("sha256") for item in identity.get("contracts") or []}
    for item in verifications:
        payload = item.get("payload") or {}
        contract_path_value = payload.get("contract_path")
        if not contract_path_value and isinstance(payload.get("input_contract"), dict):
            contract_path_value = payload["input_contract"].get("path")
        if not contract_path_value:
            continue
        contract_path = Path(str(contract_path_value)).expanduser()
        if not contract_path.is_file():
            reject("contract_missing", f"evidence contract path is missing: {contract_path}", artifact=item.get("artifact"))
            continue
        if sha256_file(contract_path) not in contract_hashes:
            reject("contract_changed", f"evidence contract bytes do not match the frozen identity: {contract_path}", artifact=item.get("artifact"))
    required_gates = {"fidelity", "quality", "interaction", "resource_privacy", "runtime_reset", "candidate_immutability"}
    gate_counts = {gate: sum(item.get("gate") == gate for item in verifications) for gate in required_gates}
    # The terminal interaction result is itself an immutable read-only check;
    # count it for the explicit release gate when no separate artifact exists.
    if gate_counts["candidate_immutability"] == 0:
        gate_counts["candidate_immutability"] = sum(
            item.get("gate") == "interaction" and (item.get("payload", {}).get("candidate") or {}).get("unchanged") is True
            for item in verifications
        )
    for gate, count in sorted(gate_counts.items()):
        if count == 0:
            reject("required_gate_missing", f"required {gate} evidence is missing", gate=gate)
        elif count > 1:
            reject("duplicate_gate_evidence", f"required {gate} evidence is duplicated", gate=gate)
    if seed_hash and runtime_hash:
        reset = next((item["payload"] for item in verifications if item["gate"] == "runtime_reset"), None)
        if reset and (reset.get("seed_sha256") != seed_hash or reset.get("runtime_sha256") != runtime_hash):
            reject("reset_evidence_stale", "runtime reset evidence does not bind current database bytes")
    fidelity = next((item for item in verifications if item["gate"] == "fidelity"), None)
    quality = next((item for item in verifications if item["gate"] == "quality"), None)
    if fidelity and quality and quality["payload"].get("fidelity_evidence_sha256") != fidelity["sha256"]:
        reject("quality_fidelity_mismatch", "quality evidence does not bind the current fidelity artifact")

    gate_result = {
        "schema": "webanything-release-gate-result-v1",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "site": site,
        "profile": profile,
        "status": "failed" if reasons else "passed",
        "candidate_identity_sha256": identity.get("sha256"),
        "candidate_sha256": (identity.get("tree") or {}).get("sha256"),
        "reason_codes": reasons,
        "required_gates": {gate: {"status": "passed" if gate_counts.get(gate) == 1 else "failed", "artifacts": gate_counts.get(gate, 0)} for gate in sorted(required_gates)},
    }
    manifest_path = output_dir / "release-manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    atomic_json(output_dir / "release-gate-result.json", gate_result)
    if reasons:
        raise ReleaseError("release gate failed: " + ", ".join(item["code"] for item in reasons))

    candidate = identity["tree"]
    manifest = {
        "schema": "webanything-release-manifest-v2",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "site": site,
        "profile": profile,
        "candidate_sha256": candidate["sha256"],
        "candidate": candidate,
        "candidate_identity": identity,
        "seed_reset": {
            "status": "passed",
            "seed_path": seed_db.relative_to(site_path).as_posix(),
            "runtime_path": runtime_db.relative_to(site_path).as_posix(),
            "seed_sha256": seed_hash,
            "runtime_sha256": runtime_hash,
        },
        "privacy_scan": {
            "status": "passed",
            "remaining_private_paths": [],
            "removed_paths": [],
        },
        "verification": {"status": "passed", "artifacts": [{k: v for k, v in item.items() if k != "payload"} for item in verifications]},
        "release_gate": {"path": "release-gate-result.json", "sha256": sha256_file(output_dir / "release-gate-result.json")},
        "deployment_binding": {
            "algorithm": candidate["algorithm"],
            "candidate_sha256": candidate["sha256"],
            "candidate_identity_sha256": identity["sha256"],
        },
        "release_status": "frozen",
    }
    atomic_json(manifest_path, manifest)
    return manifest
