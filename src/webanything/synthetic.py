"""Compile and validate deterministic synthetic benchmark contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
OBJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ROUTE_ID_RE = re.compile(r"^route_[a-z0-9_]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_ACTIONS = {"click", "fill", "select", "check", "uncheck", "press"}
SUPPORTED_ASSERTIONS = {"local_state", "dom", "url"}
SUPPORTED_CONTENT_MODES = {"fictional", "source_fidelity"}
INTERACTION_INVENTORY_SELECTOR = (
    "button:not([disabled]),a[href],input:not([type=hidden]):not([disabled]),"
    "select:not([disabled]),textarea:not([disabled]),"
    "[role=button]:not([aria-disabled=true]),[role=link]"
)
ACTION_ID_SELECTOR_RE = re.compile(
    r"\[data-action-id=(?:\"([a-z][a-z0-9_]*)\"|'([a-z][a-z0-9_]*)'|([a-z][a-z0-9_]*))\]"
)


class SyntheticContractError(ValueError):
    """The proposed synthetic contract is unsafe or non-deterministic."""


def contract_content_mode(contract: dict[str, Any]) -> str:
    """Return the explicit content mode while preserving legacy v1 contracts."""
    policy = contract.get("content_policy")
    if not isinstance(policy, dict):
        return "fictional"
    return str(policy.get("mode") or "fictional")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SyntheticContractError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyntheticContractError(f"JSON object required: {path}")
    return value


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, value: Any) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_bytes(path, payload)


def _relative_artifact(base: Path, raw: Any, label: str, *, prefix: str | None = None) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise SyntheticContractError(f"{label} must be a nonempty relative path")
    relative = Path(raw)
    if ".." in relative.parts:
        raise SyntheticContractError(f"{label} must not escape its bundle")
    if prefix and (not relative.parts or relative.parts[0] != prefix):
        raise SyntheticContractError(f"{label} must live under {prefix}/")
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise SyntheticContractError(f"{label} escapes its bundle") from exc
    return resolved


def _route_path(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.startswith("/") or raw.startswith("//") or "#" in raw:
        raise SyntheticContractError(f"{label} must be a deterministic local path")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        raise SyntheticContractError(f"{label} must not contain an origin")
    return raw


def _validate_fixed_time(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.endswith("Z"):
        raise SyntheticContractError("clock.fixed_time must be an RFC 3339 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyntheticContractError("clock.fixed_time is invalid") from exc
    return raw


def _success_assertions(task: dict[str, Any], label: str) -> list[dict[str, Any]]:
    raw = task.get("success")
    values = raw if isinstance(raw, list) else [raw]
    if not values or any(not isinstance(item, dict) for item in values):
        raise SyntheticContractError(f"{label}.success must contain assertions")
    assertions: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        kind = item.get("type")
        if kind not in SUPPORTED_ASSERTIONS:
            raise SyntheticContractError(f"{label}.success[{index}].type is unsupported")
        assertion = dict(item)
        if kind == "local_state":
            if not isinstance(item.get("path"), str) or not item["path"]:
                raise SyntheticContractError(f"{label}.success[{index}].path is required")
            if "equals" not in item:
                raise SyntheticContractError(f"{label}.success[{index}].equals is required")
        elif kind == "dom":
            if not isinstance(item.get("selector"), str) or not item["selector"]:
                raise SyntheticContractError(f"{label}.success[{index}].selector is required")
        else:
            assertion["path"] = _route_path(item.get("path"), f"{label}.success[{index}].path")
        assertions.append(assertion)
    return assertions


def load_tasks(path: Path, *, route_paths: set[str]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SyntheticContractError(f"tasks file is unavailable: {path}: {exc}") from exc
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            task = json.loads(raw)
        except ValueError as exc:
            raise SyntheticContractError(f"invalid task JSON at {path}:{number}") from exc
        label = f"task line {number}"
        if not isinstance(task, dict):
            raise SyntheticContractError(f"{label} must be an object")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not OBJECT_ID_RE.fullmatch(task_id):
            raise SyntheticContractError(f"{label}.task_id must be a stable snake-case id")
        if task_id in seen:
            raise SyntheticContractError(f"duplicate task_id: {task_id}")
        seen.add(task_id)
        start_url = _route_path(task.get("start_url"), f"{label}.start_url")
        if start_url not in route_paths:
            raise SyntheticContractError(f"{label}.start_url is not a declared route")
        if not isinstance(task.get("instruction"), str) or not task["instruction"].strip():
            raise SyntheticContractError(f"{label}.instruction is required")
        actions = task.get("actions")
        if not isinstance(actions, list) or not actions:
            raise SyntheticContractError(f"{label}.actions must contain an independent replay")
        for action_index, action in enumerate(actions):
            if not isinstance(action, dict) or action.get("type") not in SUPPORTED_ACTIONS:
                raise SyntheticContractError(f"{label}.actions[{action_index}] is unsupported")
            if not isinstance(action.get("selector"), str) or not action["selector"]:
                raise SyntheticContractError(f"{label}.actions[{action_index}].selector is required")
            if action["type"] in {"fill", "select", "press"} and not isinstance(action.get("value"), str):
                raise SyntheticContractError(f"{label}.actions[{action_index}].value is required")
        assertions = _success_assertions(task, label)
        allowed = task.get("allowed_state_changes")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(item, str) and item for item in allowed):
            raise SyntheticContractError(f"{label}.allowed_state_changes must be nonempty")
        covered_action_ids = task.get("covered_action_ids")
        if covered_action_ids is not None and (
            not isinstance(covered_action_ids, list)
            or not covered_action_ids
            or any(not isinstance(item, str) or not OBJECT_ID_RE.fullmatch(item) for item in covered_action_ids)
            or len(set(covered_action_ids)) != len(covered_action_ids)
        ):
            raise SyntheticContractError(f"{label}.covered_action_ids must contain unique stable action ids")
        normalized = {
            "task_id": task_id,
            "start_url": start_url,
            "initial_state": str(task.get("initial_state") or "default"),
            "instruction": task["instruction"].strip(),
            "actions": actions,
            "success": assertions,
            "allowed_state_changes": list(dict.fromkeys(allowed)),
        }
        if covered_action_ids is not None:
            normalized["covered_action_ids"] = covered_action_ids
        tasks.append(normalized)
    if not tasks:
        raise SyntheticContractError("tasks.jsonl must contain at least one task")
    return tasks


def _validate_contract_shape(contract: dict[str, Any]) -> None:
    if contract.get("schema") != "webanything-synthetic-contract-v1" or contract.get("schema_version") != 1:
        raise SyntheticContractError("unsupported synthetic contract schema")
    if contract.get("build_mode") != "benchmark_synthetic":
        raise SyntheticContractError("synthetic contract build_mode must be benchmark_synthetic")
    site = contract.get("site_id")
    if not isinstance(site, str) or not SITE_ID_RE.fullmatch(site):
        raise SyntheticContractError("site_id must match [a-z0-9][a-z0-9_]*")
    parsed_seed = urlsplit(str(contract.get("seed_url") or ""))
    if parsed_seed.scheme not in {"http", "https"} or not parsed_seed.hostname or parsed_seed.username or parsed_seed.password:
        raise SyntheticContractError("seed_url must be an absolute credential-free HTTP(S) URL")
    brand = contract.get("brand")
    if (
        not isinstance(brand, dict)
        or not isinstance(brand.get("fictional"), bool)
        or not str(brand.get("name") or "").strip()
    ):
        raise SyntheticContractError("brand must be explicitly classified and named")
    clock = contract.get("clock")
    if not isinstance(clock, dict) or clock.get("timezone") != "UTC":
        raise SyntheticContractError("clock.timezone must be UTC")
    _validate_fixed_time(clock.get("fixed_time"))
    random = contract.get("random")
    if not isinstance(random, dict) or random.get("algorithm") != "mulberry32-v1" or not isinstance(random.get("seed"), int):
        raise SyntheticContractError("random must declare mulberry32-v1 and an integer seed")
    if not 0 <= random["seed"] <= 0xFFFFFFFF:
        raise SyntheticContractError("random.seed must fit uint32")
    storage = contract.get("storage")
    expected_namespace = f"webanything.{site}.v{(storage or {}).get('schema_version')}"
    if (
        not isinstance(storage, dict)
        or storage.get("engine") != "localStorage"
        or not isinstance(storage.get("schema_version"), int)
        or storage.get("schema_version", 0) < 1
        or storage.get("namespace") != expected_namespace
    ):
        raise SyntheticContractError(f"storage namespace must be {expected_namespace}")
    auth = contract.get("auth")
    if (
        not isinstance(auth, dict)
        or auth.get("mode") != "local_synthetic"
        or not isinstance(auth.get("default_user_id"), str)
        or not OBJECT_ID_RE.fullmatch(auth["default_user_id"])
        or not isinstance(auth.get("default_role"), str)
        or not OBJECT_ID_RE.fullmatch(auth["default_role"])
    ):
        raise SyntheticContractError("auth must declare a stable local synthetic user and role")
    routes = contract.get("routes")
    if not isinstance(routes, list) or not routes:
        raise SyntheticContractError("routes must be nonempty")
    route_ids: set[str] = set()
    route_paths: set[str] = set()
    interaction_policy = contract.get("interaction_policy")
    if interaction_policy is not None and interaction_policy != {
        "mode": "all_visible_controls",
        "inventory_selector": INTERACTION_INVENTORY_SELECTOR,
        "action_attribute": "data-action-id",
        "coverage": "all_declared_and_no_extra",
    }:
        raise SyntheticContractError("interaction_policy must use the fail-closed visible-control inventory")
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            raise SyntheticContractError(f"routes[{index}] must be an object")
        route_id = route.get("id")
        if not isinstance(route_id, str) or not ROUTE_ID_RE.fullmatch(route_id) or route_id in route_ids:
            raise SyntheticContractError(f"routes[{index}].id is invalid or duplicated")
        route_ids.add(route_id)
        path = _route_path(route.get("path"), f"routes[{index}].path")
        if path in route_paths:
            raise SyntheticContractError(f"duplicate route path: {path}")
        route_paths.add(path)
        selectors = route.get("required_selectors")
        if not isinstance(selectors, list) or not selectors or not all(isinstance(item, str) and item for item in selectors):
            raise SyntheticContractError(f"routes[{index}].required_selectors must be nonempty")
        action_ids = route.get("required_action_ids")
        if interaction_policy is not None:
            if (
                not isinstance(action_ids, list)
                or not action_ids
                or any(not isinstance(item, str) or not OBJECT_ID_RE.fullmatch(item) for item in action_ids)
                or len(set(action_ids)) != len(action_ids)
            ):
                raise SyntheticContractError(f"routes[{index}].required_action_ids must contain unique stable action ids")
        elif action_ids is not None:
            raise SyntheticContractError("required_action_ids require interaction_policy")
    reset = contract.get("reset")
    if (
        not isinstance(reset, dict)
        or reset.get("supported") is not True
        or reset.get("entrypoint") != "/reset.html"
        or reset.get("api") != "window.__WEBANYTHING__.reset"
        or _route_path(reset.get("initial_url"), "reset.initial_url") not in route_paths
    ):
        raise SyntheticContractError("reset contract is incomplete or uses an undeclared initial route")
    network = contract.get("network_policy")
    if not isinstance(network, dict) or network.get("external_requests") != 0 or network.get("allowed_origins") != []:
        raise SyntheticContractError("network_policy must forbid every external origin")
    policy = contract.get("content_policy")
    mode = contract_content_mode(contract)
    if (
        not isinstance(policy, dict)
        or mode not in SUPPORTED_CONTENT_MODES
        or policy.get("private_user_data_allowed") is not False
        or not isinstance(policy.get("forbidden_terms"), list)
        or not all(isinstance(item, str) and len(item) >= 2 for item in policy["forbidden_terms"])
        or len(set(policy["forbidden_terms"])) != len(policy["forbidden_terms"])
    ):
        raise SyntheticContractError("content_policy is invalid")
    if mode == "source_fidelity":
        if (
            policy.get("mode") != "source_fidelity"
            or policy.get("synthetic_only") is not False
            or policy.get("upstream_assets_allowed") is not True
            or policy.get("forbidden_terms") != []
            or brand.get("fictional") is not False
        ):
            raise SyntheticContractError(
                "source_fidelity requires a non-fictional brand, frozen-source content, and no forbidden source terms"
            )
    elif (
        policy.get("synthetic_only") is not True
        or policy.get("upstream_assets_allowed") is not False
        or brand.get("fictional") is not True
    ):
        raise SyntheticContractError("fictional content mode requires fictional, synthetic-only local content")
    fixtures = contract.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise SyntheticContractError("fixtures must contain hash-bound deterministic inputs")
    fixture_paths: set[str] = set()
    for index, fixture in enumerate(fixtures):
        if (
            not isinstance(fixture, dict)
            or not isinstance(fixture.get("path"), str)
            or not fixture["path"].startswith("fixtures/")
            or not SHA256_RE.fullmatch(str(fixture.get("sha256") or ""))
            or fixture["path"] in fixture_paths
        ):
            raise SyntheticContractError(f"fixtures[{index}] is invalid or duplicated")
        fixture_paths.add(fixture["path"])
    if "fixtures/initial-state.json" not in fixture_paths:
        raise SyntheticContractError("fixtures must include fixtures/initial-state.json")
    tasks = contract.get("tasks")
    if (
        not isinstance(tasks, dict)
        or tasks.get("path") != "tasks.jsonl"
        or not SHA256_RE.fullmatch(str(tasks.get("sha256") or ""))
        or not isinstance(tasks.get("count"), int)
        or tasks["count"] < 1
    ):
        raise SyntheticContractError("tasks must be hash-bound and nonempty")
    evaluator = contract.get("evaluator")
    if (
        not isinstance(evaluator, dict)
        or evaluator.get("api_global") != "window.__WEBANYTHING__"
        or evaluator.get("authoritative_surfaces") != ["dom", "url", "localStorage", "network"]
    ):
        raise SyntheticContractError("evaluator must expose the standard independent surfaces")
    visual = contract.get("visual_reference")
    if visual is not None:
        if (
            not isinstance(visual, dict)
            or visual.get("required") is not True
            or not isinstance(visual.get("path"), str)
            or not visual["path"].startswith("visual-reference/")
            or not SHA256_RE.fullmatch(str(visual.get("sha256") or ""))
            or not SHA256_RE.fullmatch(str(visual.get("reference_sha256") or ""))
        ):
            raise SyntheticContractError("visual_reference must bind a required frozen reference")


def _validate_interaction_coverage(contract: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
    policy = contract.get("interaction_policy")
    covered = {
        action_id
        for task in tasks
        for action_id in task.get("covered_action_ids", [])
    }
    if policy is None:
        if covered:
            raise SyntheticContractError("covered_action_ids require interaction_policy")
        return
    for task in tasks:
        selector_ids = {
            next(value for value in match.groups() if value is not None)
            for action in task["actions"]
            for match in ACTION_ID_SELECTOR_RE.finditer(action["selector"])
        }
        declared_by_task = set(task.get("covered_action_ids", []))
        if selector_ids != declared_by_task:
            raise SyntheticContractError(
                f"task {task['task_id']} covered_action_ids must exactly match its data-action-id selectors"
            )
        route_action_ids = {
            action_id
            for route in contract["routes"]
            if route["path"] == task["start_url"]
            for action_id in route["required_action_ids"]
        }
        if not declared_by_task.issubset(route_action_ids):
            raise SyntheticContractError(
                f"task {task['task_id']} covers action ids outside its start route"
            )
    declared = {
        action_id
        for route in contract["routes"]
        for action_id in route["required_action_ids"]
    }
    missing = sorted(declared - covered)
    unexpected = sorted(covered - declared)
    if missing or unexpected:
        raise SyntheticContractError(
            "interaction action coverage differs from declared visible controls: "
            f"missing={missing}, unexpected={unexpected}"
        )


def compile_synthetic_contract(spec_path: Path, output_path: Path) -> dict[str, Any]:
    """Normalize one pre-Builder bundle and bind its fixtures/tasks by hash."""
    spec_path = spec_path.resolve()
    output_path = output_path.resolve()
    spec = _read_json(spec_path)
    if spec.get("schema") not in {"webanything-synthetic-contract-draft-v1", "webanything-synthetic-contract-v1"}:
        raise SyntheticContractError("unsupported synthetic contract draft schema")
    base = spec_path.parent
    raw_fixtures = spec.get("fixtures")
    if not isinstance(raw_fixtures, list) or not raw_fixtures:
        raise SyntheticContractError("fixtures must list deterministic JSON inputs")
    fixtures: list[dict[str, str]] = []
    seen_targets: set[str] = set()
    for index, item in enumerate(raw_fixtures):
        if not isinstance(item, dict):
            raise SyntheticContractError(f"fixtures[{index}] must be an object")
        source_raw = item.get("source") or item.get("path")
        target_raw = item.get("path")
        source = _relative_artifact(base, source_raw, f"fixtures[{index}].source")
        target = _relative_artifact(output_path.parent, target_raw, f"fixtures[{index}].path", prefix="fixtures")
        if target_raw in seen_targets:
            raise SyntheticContractError(f"duplicate fixture target: {target_raw}")
        seen_targets.add(str(target_raw))
        if not source.is_file():
            raise SyntheticContractError(f"fixture is missing: {source}")
        _read_json(source)
        if source != target:
            _atomic_bytes(target, source.read_bytes())
        fixtures.append({"path": str(target_raw), "sha256": sha256_file(target)})
    tasks_source_raw = spec.get("tasks_source") or (spec.get("tasks") or {}).get("source") or "tasks.jsonl"
    tasks_source = _relative_artifact(base, tasks_source_raw, "tasks_source")
    task_target = output_path.parent / "tasks.jsonl"
    route_paths = {
        _route_path(item.get("path"), f"routes[{index}].path")
        for index, item in enumerate(spec.get("routes") or [])
        if isinstance(item, dict)
    }
    tasks = load_tasks(tasks_source, route_paths=route_paths)
    task_payload = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in tasks)
    _atomic_bytes(task_target, task_payload.encode("utf-8"))
    visual_descriptor = None
    raw_visual = spec.get("visual_reference")
    if raw_visual is not None:
        if not isinstance(raw_visual, dict) or raw_visual.get("required") is not True:
            raise SyntheticContractError("visual_reference.required must be true when a visual reference is supplied")
        source_raw = raw_visual.get("source") or raw_visual.get("path")
        target_raw = raw_visual.get("path") or "visual-reference/synthetic_visual_reference.json"
        source = _relative_artifact(base, source_raw, "visual_reference.source")
        target = _relative_artifact(output_path.parent, target_raw, "visual_reference.path", prefix="visual-reference")
        try:
            from .synthetic_visual import load_visual_reference
            reference = load_visual_reference(source)
        except (ImportError, RuntimeError, ValueError) as exc:
            raise SyntheticContractError(f"visual reference is invalid: {exc}") from exc
        if reference.get("site") != spec.get("site_id"):
            raise SyntheticContractError("visual reference names another site")
        for route in reference["routes"]:
            for key in ("screenshot", "comparison_screenshot", "geometry"):
                artifact_raw = route[key]["path"]
                source_artifact = _relative_artifact(source.parent, artifact_raw, f"visual_reference.{key}")
                target_artifact = _relative_artifact(target.parent, artifact_raw, f"visual_reference.{key}")
                if source_artifact != target_artifact:
                    _atomic_bytes(target_artifact, source_artifact.read_bytes())
        if source != target:
            _atomic_bytes(target, source.read_bytes())
        visual_descriptor = {
            "required": True,
            "path": str(target_raw),
            "sha256": sha256_file(target),
            "reference_sha256": reference["reference_sha256"],
        }
    contract = {
        key: value
        for key, value in spec.items()
        if key not in {"tasks_source", "tasks", "fixtures", "compiled_at", "visual_reference"}
    }
    contract.update({
        "schema": "webanything-synthetic-contract-v1",
        "schema_version": 1,
        "build_mode": "benchmark_synthetic",
        "fixtures": sorted(fixtures, key=lambda item: item["path"]),
        "tasks": {"path": "tasks.jsonl", "sha256": sha256_file(task_target), "count": len(tasks)},
        "evaluator": {
            "api_global": "window.__WEBANYTHING__",
            "authoritative_surfaces": ["dom", "url", "localStorage", "network"],
        },
    })
    if visual_descriptor is not None:
        contract["visual_reference"] = visual_descriptor
    _validate_contract_shape(contract)
    _validate_interaction_coverage(contract, tasks)
    _atomic_json(output_path, contract)
    return contract


def load_synthetic_contract(path: Path, *, verify_artifacts: bool = True) -> dict[str, Any]:
    path = path.resolve()
    contract = _read_json(path)
    _validate_contract_shape(contract)
    if not verify_artifacts:
        return contract
    for index, fixture in enumerate(contract.get("fixtures") or []):
        if not isinstance(fixture, dict) or not SHA256_RE.fullmatch(str(fixture.get("sha256") or "")):
            raise SyntheticContractError(f"fixtures[{index}] is not hash-bound")
        artifact = _relative_artifact(path.parent, fixture.get("path"), f"fixtures[{index}].path", prefix="fixtures")
        if not artifact.is_file() or sha256_file(artifact) != fixture["sha256"]:
            raise SyntheticContractError(f"fixture hash mismatch: {artifact}")
    tasks = contract.get("tasks") or {}
    task_path = _relative_artifact(path.parent, tasks.get("path"), "tasks.path")
    if not task_path.is_file() or sha256_file(task_path) != tasks.get("sha256"):
        raise SyntheticContractError(f"tasks hash mismatch: {task_path}")
    route_paths = {item["path"] for item in contract["routes"]}
    loaded = load_tasks(task_path, route_paths=route_paths)
    if len(loaded) != tasks.get("count"):
        raise SyntheticContractError("tasks count differs from the contract")
    _validate_interaction_coverage(contract, loaded)
    visual = contract.get("visual_reference")
    if visual is not None:
        reference_path = _relative_artifact(path.parent, visual.get("path"), "visual_reference.path", prefix="visual-reference")
        if not reference_path.is_file() or sha256_file(reference_path) != visual.get("sha256"):
            raise SyntheticContractError(f"visual reference hash mismatch: {reference_path}")
        try:
            from .synthetic_visual import load_visual_reference
            reference = load_visual_reference(reference_path)
        except (ImportError, RuntimeError, ValueError) as exc:
            raise SyntheticContractError(f"visual reference is invalid: {exc}") from exc
        if reference.get("reference_sha256") != visual.get("reference_sha256"):
            raise SyntheticContractError("visual reference identity differs from the contract")
    return contract


def contract_artifact_paths(contract_path: Path) -> Iterable[Path]:
    contract = load_synthetic_contract(contract_path)
    yield contract_path.resolve()
    for fixture in contract["fixtures"]:
        yield (contract_path.resolve().parent / fixture["path"]).resolve()
    yield (contract_path.resolve().parent / contract["tasks"]["path"]).resolve()
    visual = contract.get("visual_reference")
    if visual:
        reference_path = (contract_path.resolve().parent / visual["path"]).resolve()
        yield reference_path
        from .synthetic_visual import load_visual_reference
        reference = load_visual_reference(reference_path)
        for route in reference["routes"]:
            yield (reference_path.parent / route["screenshot"]["path"]).resolve()
            yield (reference_path.parent / route["comparison_screenshot"]["path"]).resolve()
            yield (reference_path.parent / route["geometry"]["path"]).resolve()
