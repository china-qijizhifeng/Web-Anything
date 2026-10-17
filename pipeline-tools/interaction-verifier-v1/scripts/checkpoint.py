"""Deterministic exhaustive-verifier plans and crash-safe batch checkpoints.

The browser runner is deliberately kept separate from this module.  Planning,
checkpoint validation, and merging are pure file/data operations so interrupted
runs can be resumed without trusting a filename or a status field in isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

PLAN_SCHEMA = "webharbor-interaction-verification-plan-v1"
CHECKPOINT_SCHEMA = "webharbor-interaction-verification-checkpoint-v1"
MERGED_SCHEMA = "webharbor-interaction-verification-merge-v1"
SHARED_ARTIFACT_SCHEMA = "webharbor-interaction-verification-shared-artifact-v1"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def shared_artifact_ref(path: Path, value: Any | None = None) -> dict[str, Any]:
    """Create a portable hash/size reference for an immutable shared artifact."""
    if value is not None:
        payload = canonical_json(value)
        return {"schema": SHARED_ARTIFACT_SCHEMA, "path": path.as_posix(), "bytes": len(payload), "sha256": sha256(payload)}
    payload = path.read_bytes()
    return {"schema": SHARED_ARTIFACT_SCHEMA, "path": path.as_posix(), "bytes": len(payload), "sha256": sha256(payload)}


def _inventory_ids(inventory: Iterable[dict[str, Any]] | None, *, kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(inventory or []):
        if not isinstance(item, dict):
            raise ValueError(f"{kind} inventory entries must be objects")
        identifier = item.get("id") or item.get("route_id") or item.get("control_id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"{kind} inventory entry {index} has no stable id")
        rows.append({"id": identifier, "index": index, "route": item.get("path") or item.get("source_path") or item.get("route")})
    ids = [item["id"] for item in rows]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{kind} inventory ids must be unique")
    return rows


def build_verification_plan(
    *,
    candidate_fingerprint: dict[str, Any] | str,
    contract_sha256: str,
    verifier_version: str,
    verifier_configuration: dict[str, Any] | None,
    viewport: dict[str, int],
    routes: Iterable[dict[str, Any]],
    controls: Iterable[dict[str, Any]],
    candidate_identity_sha256: str | None = None,
    analyzer_implementation_sha256: str | None = None,
    shared_artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable, exhaustive plan over every declared route and control."""
    route_rows = _inventory_ids(routes, kind="route")
    control_rows = _inventory_ids(controls, kind="control")
    if not route_rows:
        raise ValueError("exhaustive verification plan requires at least one route")
    if not isinstance(contract_sha256, str) or len(contract_sha256) != 64:
        raise ValueError("contract_sha256 must be a SHA-256 string")
    if not isinstance(viewport, dict) or not all(isinstance(viewport.get(k), int) and viewport[k] > 0 for k in ("width", "height")):
        raise ValueError("viewport must contain positive width and height")
    candidate_hash = candidate_fingerprint if isinstance(candidate_fingerprint, str) else candidate_fingerprint.get("sha256")
    if not isinstance(candidate_hash, str) or not candidate_hash:
        raise ValueError("candidate_fingerprint must contain sha256")
    configuration = verifier_configuration or {}
    identity_payload = {
        "candidate_fingerprint": candidate_fingerprint,
        "contract_sha256": contract_sha256,
        "verifier_version": verifier_version,
        "verifier_configuration": configuration,
        "viewport": {"width": viewport["width"], "height": viewport["height"]},
        "routes": route_rows,
        "controls": control_rows,
        "candidate_identity_sha256": candidate_identity_sha256,
        "analyzer_implementation_sha256": analyzer_implementation_sha256,
        "shared_artifacts": shared_artifacts or {},
    }
    plan = {
        "schema": PLAN_SCHEMA,
        "plan_identity": sha256(canonical_json(identity_payload)),
        "candidate_fingerprint": candidate_fingerprint,
        "candidate_sha256": candidate_hash,
        "contract_sha256": contract_sha256,
        "candidate_identity_sha256": candidate_identity_sha256,
        "analyzer_implementation_sha256": analyzer_implementation_sha256,
        "shared_artifacts": shared_artifacts or {},
        "verifier": {"version": verifier_version, "configuration": configuration},
        "viewport": identity_payload["viewport"],
        "inventory": {"routes": route_rows, "controls": control_rows},
    }
    plan["item_count"] = len(route_rows) + len(control_rows)
    return plan


def partition_plan(plan: dict[str, Any], batch_size: int) -> list[dict[str, Any]]:
    if plan.get("schema") != PLAN_SCHEMA or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("valid plan and positive batch_size are required")
    if not plan.get("item_count"):
        raise ValueError("cannot partition an empty exhaustive plan")
    items = [
        {"kind": "route", "id": row["id"], "route": row.get("route")}
        for row in plan["inventory"]["routes"]
    ] + [
        {"kind": "control", "id": row["id"], "route": row.get("route")}
        for row in plan["inventory"]["controls"]
    ]
    batches: list[dict[str, Any]] = []
    for offset in range(0, len(items), batch_size):
        assigned = items[offset:offset + batch_size]
        index = len(batches)
        batches.append({
            "schema": CHECKPOINT_SCHEMA,
            "batch_id": f"batch-{index:06d}",
            "batch_index": index,
            "plan_identity": plan["plan_identity"],
            "candidate_sha256": plan["candidate_sha256"],
            "contract_sha256": plan["contract_sha256"],
            "verifier": plan["verifier"],
            "viewport": plan["viewport"],
            "candidate_identity_sha256": plan.get("candidate_identity_sha256"),
            "analyzer_implementation_sha256": plan.get("analyzer_implementation_sha256"),
            "shared_artifacts": plan.get("shared_artifacts", {}),
            "assigned": assigned,
            "assigned_ids": [f"{item['kind']}:{item['id']}" for item in assigned],
            "status": "pending",
        })
    return batches


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_checkpoint(path: Path, batch: dict[str, Any], *, status: str, result: Any) -> dict[str, Any]:
    if status not in {"passed", "failed", "timed_out"}:
        raise ValueError("checkpoint status must be passed, failed, or timed_out")
    payload = dict(batch)
    payload.update({"status": status, "result": result, "result_sha256": sha256(canonical_json(result))})
    atomic_json(path, payload)
    return payload


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def validate_checkpoint(value: dict[str, Any] | None, expected: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(value, dict):
        return False, "checkpoint is not a JSON object"
    for key in ("schema", "batch_id", "batch_index", "plan_identity", "candidate_sha256", "contract_sha256", "verifier", "viewport", "candidate_identity_sha256", "analyzer_implementation_sha256", "shared_artifacts", "assigned_ids", "status", "result_sha256"):
        if key not in value:
            return False, f"checkpoint missing {key}"
    if value.get("schema") != CHECKPOINT_SCHEMA:
        return False, "unsupported checkpoint schema"
    for key in ("plan_identity", "candidate_sha256", "contract_sha256", "verifier", "viewport", "candidate_identity_sha256", "analyzer_implementation_sha256", "shared_artifacts"):
        if value.get(key) != expected.get(key):
            return False, f"checkpoint {key} does not match plan"
    if value.get("batch_id") != expected.get("batch_id") or value.get("batch_index") != expected.get("batch_index"):
        return False, "checkpoint batch identity does not match plan"
    if value.get("assigned_ids") != expected.get("assigned_ids"):
        return False, "checkpoint assigned route/control IDs do not match plan"
    if value.get("status") not in {"passed", "failed", "timed_out"}:
        return False, "checkpoint is not terminal"
    if "result" not in value or value.get("result_sha256") != sha256(canonical_json(value["result"])):
        return False, "checkpoint result hash is invalid"
    return True, "valid"


def load_valid_checkpoints(directory: Path, batches: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    found: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for expected in batches:
        path = directory / f"{expected['batch_id']}.json"
        value = _read(path)
        valid, reason = validate_checkpoint(value, expected)
        if valid:
            # Resume may skip only completed/passed work. Failed and timed-out
            # terminal records remain useful diagnostic evidence but must be
            # retried before a later merge can close.
            if value.get("status") == "passed":
                found[expected["batch_id"]] = value  # type: ignore[assignment]
            else:
                errors.append(f"{expected['batch_id']}: prior batch was {value.get('status')}")
        elif value is not None:
            errors.append(f"{expected['batch_id']}: {reason}")
    return found, errors


def execute_batches(
    plan: dict[str, Any],
    batches: list[dict[str, Any]],
    directory: Path,
    executor,
    *,
    resume: bool = True,
) -> dict[str, Any]:
    """Execute only absent/incompatible batches and deterministically merge.

    ``executor`` receives one immutable batch assignment and returns either a
    result object or ``(terminal_status, result)``.  This small interface lets a
    process supervisor, test fixture, or browser worker own execution without
    weakening the merge rules.
    """
    existing, _errors = load_valid_checkpoints(directory, batches) if resume else ({}, [])
    values: list[dict[str, Any]] = []
    for batch in batches:
        if batch["batch_id"] in existing:
            values.append(existing[batch["batch_id"]])
            continue
        try:
            outcome = executor(dict(batch))
            if isinstance(outcome, tuple) and len(outcome) == 2:
                status, result = outcome
            else:
                result = outcome
                status = result.get("status", "passed") if isinstance(result, dict) else "passed"
            if status not in {"passed", "failed", "timed_out"}:
                status = "failed"
        except TimeoutError as exc:
            status, result = "timed_out", {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - terminal evidence must retain worker failures
            status, result = "failed", {"error": f"{type(exc).__name__}: {exc}"}
        values.append(write_checkpoint(directory / f"{batch['batch_id']}.json", batch, status=status, result=result))
    return merge_checkpoints(plan, batches, values)


def merge_checkpoints(plan: dict[str, Any], batches: list[dict[str, Any]], checkpoints: Iterable[dict[str, Any]]) -> dict[str, Any]:
    expected_by_id = {batch["batch_id"]: batch for batch in batches}
    supplied = list(checkpoints)
    failures: list[dict[str, Any]] = []
    by_batch: dict[str, dict[str, Any]] = {}
    all_ids: list[str] = []
    for value in supplied:
        batch_id = value.get("batch_id") if isinstance(value, dict) else None
        if batch_id in by_batch:
            failures.append({"code": "duplicate_batch", "batch_id": batch_id})
            continue
        expected = expected_by_id.get(batch_id)
        valid, reason = validate_checkpoint(value, expected or {})
        if not valid:
            failures.append({"code": "invalid_batch", "batch_id": batch_id, "reason": reason})
            continue
        by_batch[batch_id] = value
        all_ids.extend(value.get("assigned_ids") or [])
        if value.get("status") != "passed":
            failures.append({"code": "batch_not_passed", "batch_id": batch_id, "status": value.get("status")})
    missing = sorted(set(expected_by_id) - set(by_batch))
    failures.extend({"code": "missing_batch", "batch_id": batch_id} for batch_id in missing)
    expected_ids = [f"{item['kind']}:{item['id']}" for batch in batches for item in batch.get("assigned", [])]
    if len(all_ids) != len(set(all_ids)):
        failures.append({"code": "duplicate_assignment", "message": "route/control IDs are duplicated"})
    if sorted(all_ids) != sorted(expected_ids):
        failures.append({"code": "incomplete_coverage", "expected": expected_ids, "actual": all_ids})
    ordered = [by_batch[batch["batch_id"]] for batch in batches if batch["batch_id"] in by_batch]
    return {
        "schema": MERGED_SCHEMA,
        "plan_identity": plan.get("plan_identity"),
        "candidate_sha256": plan.get("candidate_sha256"),
        "contract_sha256": plan.get("contract_sha256"),
        "expected_batch_count": len(batches),
        "present_batch_count": len(by_batch),
        "assigned_item_count": len(expected_ids),
        "covered_item_count": len(set(all_ids)),
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "batches": [{"batch_id": item["batch_id"], "status": item["status"], "assigned_ids": item["assigned_ids"], "result": item.get("result")} for item in ordered],
    }
