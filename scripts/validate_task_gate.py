#!/usr/bin/env python3
"""Require source-derived task steps and executed verifier evidence before completion."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return value


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_assertion(value: object) -> bool:
    return _nonempty(value) or (isinstance(value, (dict, list)) and bool(value))


def _evidence(paths: object, root: Path) -> bool:
    if not isinstance(paths, list) or not paths:
        return False
    for raw in paths:
        if not _nonempty(raw):
            return False
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        artifact = (root / relative).resolve()
        if not artifact.is_relative_to(root.resolve()) or not artifact.is_file():
            return False
    return True


def validate(request_path: Path, builder_dir: Path, verifier_dir: Path) -> list[str]:
    failures: list[str] = []
    request = _read(request_path)
    contract = _read(builder_dir / "task-contract.json")
    coverage = _read(verifier_dir / "task-coverage.json")
    result = _read(verifier_dir / "verifier-result.json")
    query_hash = request.get("query_sha256")
    site_id = result.get("site_id")
    if not _nonempty(query_hash) or not _nonempty(site_id):
        failures.append("task request hash or verifier site ID is missing")
    if contract.get("task_query_sha256") != query_hash or coverage.get("task_query_sha256") != query_hash:
        failures.append("task query hash does not match Builder contract and Verifier coverage")
    if contract.get("site_id") != site_id or coverage.get("site_id") != site_id:
        failures.append("site ID differs between task contract, coverage, and verifier result")
    steps = contract.get("steps")
    covered = coverage.get("steps")
    if not isinstance(steps, list) or not steps:
        failures.append("Builder task contract has no ordered source-derived steps")
        steps = []
    if not isinstance(covered, list):
        failures.append("Verifier task coverage has no ordered steps")
        covered = []
    ids = [step.get("id") for step in steps if isinstance(step, dict)]
    covered_ids = [step.get("id") for step in covered if isinstance(step, dict)]
    if len(ids) != len(steps) or len(set(ids)) != len(ids) or any(not _nonempty(value) for value in ids):
        failures.append("Builder task step IDs must be present and unique")
    if ids != covered_ids:
        failures.append("Verifier did not cover every Builder task step in order")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or any(not _nonempty(step.get(key)) for key in
                                             ("source_route", "local_route", "action", "target", "expected_state")):
            failures.append(f"Builder task step {index} lacks a route, action, target, or expected state")
    for index, step in enumerate(covered, 1):
        if not isinstance(step, dict) or step.get("status") != "passed" or not _nonempty(
            step.get("action")
        ) or not _nonempty_assertion(step.get("assertion")) or not _evidence(
            step.get("evidence_paths"), verifier_dir
        ):
            failures.append(f"Verifier task step {index} lacks a passed assertion or persisted evidence")
    terminal = coverage.get("terminal_assertion")
    if not _nonempty(contract.get("terminal_expected_state")):
        failures.append("Builder task contract lacks a terminal expected state")
    if not isinstance(terminal, dict) or terminal.get("status") != "passed" or not _nonempty(
        terminal.get("expected")
    ) or not _nonempty_assertion(terminal.get("observed")) or not _evidence(
        terminal.get("evidence_paths"), verifier_dir
    ):
        failures.append("Verifier terminal task assertion lacks a passed result or persisted evidence")
    gates = result.get("gates")
    task_gate = gates.get("task_workflow") if isinstance(gates, dict) else None
    if coverage.get("status") != "passed" or not isinstance(task_gate, dict) or task_gate.get("status") != "passed":
        failures.append("Verifier task workflow gate did not pass")
    return failures


def main() -> int:
    request_path, builder_dir, verifier_dir = map(Path, sys.argv[1:4])
    try:
        failures = validate(request_path, builder_dir, verifier_dir)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        failures = [f"task evidence is missing or invalid: {exc}"]
    if not failures:
        return 0
    result_path = verifier_dir / "verifier-result.json"
    result = _read(result_path)
    result["status"] = "failed"
    if not isinstance(result.get("gates"), dict):
        result["gates"] = {}
    result["gates"]["task_workflow"] = {"status": "failed", "reasons": failures}
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    repair_path = verifier_dir / "repair-package.json"
    try:
        repair = _read(repair_path)
    except (OSError, ValueError):
        repair = {"schema_version": 1, "site_id": result.get("site_id")}
    if not isinstance(repair.get("failures"), list):
        repair["failures"] = []
    repair["failures"].append({
        "gate": "task_workflow", "expected": "Full source-derived task replay with persisted step evidence",
        "observed": "; ".join(failures),
    })
    repair_path.write_text(json.dumps(repair, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
