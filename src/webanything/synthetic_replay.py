"""Compile and validate full-capture synthetic replay contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
OBJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_ACTIONS = {"click", "fill", "select", "check", "uncheck", "press"}
BASE_WORKFLOWS = {"login", "logout", "reset"}
SUPPORTED_WORKFLOWS = {
    *BASE_WORKFLOWS,
    "stateful_action",
    "navigate",
    "search",
    "playback",
    "favorite",
    "comment",
    "history",
    "profile",
    "add_to_cart",
    "checkout",
    "orders",
    "book",
    "bookings",
}
CAPTURE_KINDS = {"dom", "css", "font", "image", "icon", "media", "metadata"}
SQLITE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GENERIC_EVENT_TABLES = {"events", "replay_events", "captured_actions"}


class SyntheticReplayContractError(ValueError):
    """The replay contract or one of its frozen artifacts is invalid."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SyntheticReplayContractError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyntheticReplayContractError(f"JSON object required: {path}")
    return value


def _relative(base: Path, raw: Any, label: str, *, prefix: str | None = None) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise SyntheticReplayContractError(f"{label} must be a nonempty relative path")
    relative = Path(raw)
    if ".." in relative.parts or (prefix and (not relative.parts or relative.parts[0] != prefix)):
        raise SyntheticReplayContractError(f"{label} must stay under {prefix or 'the contract bundle'}/")
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise SyntheticReplayContractError(f"{label} escapes the contract bundle") from exc
    return resolved


def _route(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.startswith("/") or raw.startswith("//") or "#" in raw:
        raise SyntheticReplayContractError(f"{label} must be a deterministic local path")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        raise SyntheticReplayContractError(f"{label} must not contain an origin")
    return raw


def _fixed_time(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.endswith("Z"):
        raise SyntheticReplayContractError("clock.fixed_time must be an RFC 3339 UTC timestamp")
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyntheticReplayContractError("clock.fixed_time is invalid") from exc
    return raw


def _sqlite_ok(path: Path) -> None:
    if not path.is_file():
        raise SyntheticReplayContractError(f"SQLite seed is missing: {path}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise SyntheticReplayContractError(f"SQLite seed is invalid: {path}: {exc}") from exc
    if not result or result[0] != "ok":
        raise SyntheticReplayContractError(f"SQLite seed integrity check failed: {path}")


def _success_condition(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SyntheticReplayContractError(f"{label} must be an object")
    surface = raw.get("surface")
    if surface == "url":
        path = _route(raw.get("path_equals"), f"{label}.path_equals")
        condition = {"surface": "url", "path_equals": urlsplit(path).path}
        if "query_equals" in raw:
            if not isinstance(raw["query_equals"], str) or raw["query_equals"].startswith("?"):
                raise SyntheticReplayContractError(f"{label}.query_equals must be a query string without ?")
            condition["query_equals"] = raw["query_equals"]
        return condition
    if surface == "dom":
        selector = raw.get("selector")
        if not isinstance(selector, str) or not selector:
            raise SyntheticReplayContractError(f"{label}.selector is required")
        modes = [
            isinstance(raw.get("visible"), bool),
            isinstance(raw.get("text_equals"), str),
            isinstance(raw.get("attribute"), str) and isinstance(raw.get("equals"), str),
        ]
        if sum(modes) != 1:
            raise SyntheticReplayContractError(
                f"{label} must declare exactly one DOM assertion: visible, text_equals, or attribute/equals"
            )
        if "visible" in raw:
            return {"surface": "dom", "selector": selector, "visible": raw["visible"]}
        if "text_equals" in raw:
            return {"surface": "dom", "selector": selector, "text_equals": raw["text_equals"]}
        if not raw["attribute"]:
            raise SyntheticReplayContractError(f"{label}.attribute is required")
        return {
            "surface": "dom",
            "selector": selector,
            "attribute": raw["attribute"],
            "equals": raw["equals"],
        }
    if surface == "sqlite":
        table = raw.get("table")
        where = raw.get("where")
        count = raw.get("count_equals")
        if not isinstance(table, str) or not SQLITE_IDENTIFIER_RE.fullmatch(table):
            raise SyntheticReplayContractError(f"{label}.table is invalid")
        if (
            not isinstance(where, dict)
            or not where
            or any(not isinstance(key, str) or not SQLITE_IDENTIFIER_RE.fullmatch(key) for key in where)
            or any(not isinstance(value, (str, int, float, bool)) for value in where.values())
        ):
            raise SyntheticReplayContractError(f"{label}.where must contain stable scalar keys")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise SyntheticReplayContractError(f"{label}.count_equals must be a nonnegative integer")
        return {"surface": "sqlite", "table": table, "where": dict(where), "count_equals": count}
    raise SyntheticReplayContractError(f"{label}.surface is unsupported")


def _load_tasks(
    path: Path,
    route_paths: set[str],
    *,
    required_workflows: set[str],
    release_tier: str,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SyntheticReplayContractError(f"tasks file is unavailable: {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            task = json.loads(line)
        except ValueError as exc:
            raise SyntheticReplayContractError(f"invalid task JSON at {path}:{number}") from exc
        label = f"task line {number}"
        task_id = task.get("task_id") if isinstance(task, dict) else None
        if not isinstance(task_id, str) or not OBJECT_ID_RE.fullmatch(task_id) or task_id in seen:
            raise SyntheticReplayContractError(f"{label}.task_id is invalid or duplicated")
        seen.add(task_id)
        start_url = _route(task.get("start_url"), f"{label}.start_url")
        if start_url not in route_paths:
            raise SyntheticReplayContractError(f"{label}.start_url is not a declared route")
        workflow = task.get("workflow")
        if workflow not in SUPPORTED_WORKFLOWS:
            raise SyntheticReplayContractError(f"{label}.workflow is unsupported")
        initial_state = task.get("initial_state", "anonymous")
        if initial_state not in {"anonymous", "authenticated"}:
            raise SyntheticReplayContractError(f"{label}.initial_state is unsupported")
        if not isinstance(task.get("instruction"), str) or not task["instruction"].strip():
            raise SyntheticReplayContractError(f"{label}.instruction is required")
        actions = task.get("actions")
        if not isinstance(actions, list) or (workflow != "reset" and not actions):
            raise SyntheticReplayContractError(f"{label}.actions must contain a replay")
        for index, action in enumerate(actions):
            if not isinstance(action, dict) or action.get("type") not in SUPPORTED_ACTIONS:
                raise SyntheticReplayContractError(f"{label}.actions[{index}] is unsupported")
            if not isinstance(action.get("selector"), str) or not action["selector"]:
                raise SyntheticReplayContractError(f"{label}.actions[{index}].selector is required")
            if action["type"] in {"fill", "select", "press"} and not isinstance(action.get("value"), str):
                raise SyntheticReplayContractError(f"{label}.actions[{index}].value is required")
        evaluator_id = task.get("evaluator_id")
        if not isinstance(evaluator_id, str) or not OBJECT_ID_RE.fullmatch(evaluator_id):
            raise SyntheticReplayContractError(f"{label}.evaluator_id must be stable")
        success = task.get("success")
        if not isinstance(success, list) or not success:
            raise SyntheticReplayContractError(f"{label}.success must contain semantic postconditions")
        conditions = [
            _success_condition(condition, f"{label}.success[{index}]")
            for index, condition in enumerate(success)
        ]
        if release_tier == "product_complete" and workflow not in BASE_WORKFLOWS:
            semantic = any(
                condition["surface"] in {"url", "dom"}
                or (
                    condition["surface"] == "sqlite"
                    and condition["table"] not in GENERIC_EVENT_TABLES
                )
                for condition in conditions
            )
            if not semantic:
                raise SyntheticReplayContractError(
                    f"{label} only checks generic action logs; product_complete tasks require a business postcondition"
                )
        tasks.append({
            "task_id": task_id,
            "workflow": workflow,
            "initial_state": initial_state,
            "start_url": start_url,
            "instruction": task["instruction"].strip(),
            "actions": actions,
            "evaluator_id": evaluator_id,
            "success": conditions,
        })
    workflows = {task["workflow"] for task in tasks}
    if not required_workflows.issubset(workflows):
        missing = ", ".join(sorted(required_workflows - workflows))
        raise SyntheticReplayContractError(f"tasks do not cover required workflows: {missing}")
    return tasks


def _capture_manifest(base: Path, descriptor: dict[str, Any], *, verify: bool) -> tuple[dict[str, Any], list[Path]]:
    path = _relative(base, descriptor.get("path"), "capture_manifest.path", prefix="capture")
    manifest = _read_json(path)
    if manifest.get("schema") != "webanything-replay-capture-manifest-v1":
        raise SyntheticReplayContractError("unsupported replay capture manifest")
    if manifest.get("external_javascript_execution") is not False or manifest.get("source_auth_persisted") is not False:
        raise SyntheticReplayContractError("capture manifest must exclude upstream JavaScript execution and source auth")
    privacy = manifest.get("privacy")
    if (
        not isinstance(privacy, dict)
        or privacy.get("status") != "passed"
        or privacy.get("source_credentials_removed") is not True
        or not isinstance(privacy.get("private_fields_removed"), int)
        or privacy["private_fields_removed"] < 0
    ):
        raise SyntheticReplayContractError("capture manifest lacks a passing privacy report")
    files = manifest.get("files")
    routes = manifest.get("routes")
    if not isinstance(files, list) or not files or not isinstance(routes, list) or not routes:
        raise SyntheticReplayContractError("capture manifest must contain routes and files")
    artifact_paths = [path]
    seen: set[str] = set()
    total_bytes = 0
    for index, item in enumerate(files):
        if (
            not isinstance(item, dict)
            or not SHA256_RE.fullmatch(str(item.get("sha256") or ""))
            or item.get("kind") not in CAPTURE_KINDS
        ):
            raise SyntheticReplayContractError(f"capture files[{index}] is invalid")
        item_path = _relative(base, item.get("path"), f"capture files[{index}].path", prefix="capture")
        relative = item_path.relative_to(base.resolve()).as_posix()
        if relative in seen:
            raise SyntheticReplayContractError(f"duplicate capture file: {relative}")
        if item_path.suffix.casefold() in {".js", ".mjs", ".cjs"}:
            raise SyntheticReplayContractError(f"upstream JavaScript is forbidden in replay capture: {relative}")
        seen.add(relative)
        if verify and (not item_path.is_file() or sha256_file(item_path) != item["sha256"]):
            raise SyntheticReplayContractError(f"capture file hash mismatch: {relative}")
        if item_path.is_file():
            total_bytes += item_path.stat().st_size
        artifact_paths.append(item_path)
    bundle_identity = canonical_sha256([{"path": item["path"], "sha256": item["sha256"]} for item in files])
    if descriptor.get("bundle_sha256") not in (None, bundle_identity):
        raise SyntheticReplayContractError("capture bundle hash mismatch")
    return {
        "path": descriptor["path"],
        "sha256": sha256_file(path),
        "bundle_sha256": bundle_identity,
        "file_count": len(files),
        "bytes": total_bytes,
    }, artifact_paths


def _validate_shape(contract: dict[str, Any], base: Path, *, verify_artifacts: bool) -> list[Path]:
    if contract.get("schema") != "webanything-synthetic-replay-contract-v1" or contract.get("schema_version") != 1:
        raise SyntheticReplayContractError("unsupported synthetic replay contract schema")
    if contract.get("build_mode") != "benchmark_synthetic_replay":
        raise SyntheticReplayContractError("replay build_mode must be benchmark_synthetic_replay")
    site = contract.get("site_id")
    if not isinstance(site, str) or not SITE_ID_RE.fullmatch(site):
        raise SyntheticReplayContractError("site_id is invalid")
    parsed = urlsplit(str(contract.get("seed_url") or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SyntheticReplayContractError("seed_url must be credential-free HTTP(S)")
    clock = contract.get("clock")
    if not isinstance(clock, dict) or clock.get("timezone") != "UTC":
        raise SyntheticReplayContractError("clock.timezone must be UTC")
    _fixed_time(clock.get("fixed_time"))
    routes = contract.get("routes")
    if not isinstance(routes, list) or not routes:
        raise SyntheticReplayContractError("routes must be nonempty")
    route_paths: set[str] = set()
    route_kinds: set[str] = set()
    for index, route in enumerate(routes):
        if not isinstance(route, dict) or not isinstance(route.get("id"), str) or not route["id"].startswith("route_"):
            raise SyntheticReplayContractError(f"routes[{index}] is invalid")
        path = _route(route.get("path"), f"routes[{index}].path")
        if path in route_paths:
            raise SyntheticReplayContractError(f"duplicate route: {path}")
        route_paths.add(path)
        kind = route.get("kind")
        if not isinstance(kind, str) or not OBJECT_ID_RE.fullmatch(kind):
            raise SyntheticReplayContractError(f"routes[{index}].kind is invalid")
        route_kinds.add(kind)
        if route.get("source") not in {"captured", "synthetic", "system"}:
            raise SyntheticReplayContractError(f"routes[{index}].source is invalid")
        selectors = route.get("required_selectors")
        if not isinstance(selectors, list) or not selectors or not all(isinstance(x, str) and x for x in selectors):
            raise SyntheticReplayContractError(f"routes[{index}].required_selectors must be nonempty")
        actions = route.get("required_action_ids")
        if (
            not isinstance(actions, list)
            or any(not isinstance(item, str) or not OBJECT_ID_RE.fullmatch(item) for item in actions)
            or len(actions) != len(set(actions))
        ):
            raise SyntheticReplayContractError(f"routes[{index}].required_action_ids is invalid")
    coverage = contract.get("coverage")
    if not isinstance(coverage, dict):
        raise SyntheticReplayContractError("coverage is required")
    profile = coverage.get("profile")
    release_tier = coverage.get("release_tier")
    required_route_kinds = coverage.get("required_route_kinds")
    required_workflows = coverage.get("required_workflows")
    if not isinstance(profile, str) or not OBJECT_ID_RE.fullmatch(profile):
        raise SyntheticReplayContractError("coverage.profile is invalid")
    if release_tier not in {"smoke", "product_complete"}:
        raise SyntheticReplayContractError("coverage.release_tier is invalid")
    if (
        not isinstance(required_route_kinds, list)
        or not required_route_kinds
        or any(not isinstance(item, str) or not OBJECT_ID_RE.fullmatch(item) for item in required_route_kinds)
        or len(required_route_kinds) != len(set(required_route_kinds))
    ):
        raise SyntheticReplayContractError("coverage.required_route_kinds is invalid")
    if not set(required_route_kinds).issubset(route_kinds):
        missing = ", ".join(sorted(set(required_route_kinds) - route_kinds))
        raise SyntheticReplayContractError(f"routes do not cover required route kinds: {missing}")
    if (
        not isinstance(required_workflows, list)
        or not required_workflows
        or any(item not in SUPPORTED_WORKFLOWS for item in required_workflows)
        or len(required_workflows) != len(set(required_workflows))
        or not BASE_WORKFLOWS.issubset(set(required_workflows))
    ):
        raise SyntheticReplayContractError("coverage.required_workflows is invalid")
    if coverage.get("generic_fallback_allowed") is not False:
        raise SyntheticReplayContractError("coverage.generic_fallback_allowed must be false")
    runtime = contract.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("kind") != "flask_sqlite" or runtime.get("proxy_prefix_aware") is not True:
        raise SyntheticReplayContractError("runtime must be a proxy-prefix-aware flask_sqlite service")
    if not isinstance(runtime.get("entrypoint"), str) or Path(runtime["entrypoint"]).is_absolute() or ".." in Path(runtime["entrypoint"]).parts:
        raise SyntheticReplayContractError("runtime.entrypoint must be relative")
    _route(runtime.get("health_path"), "runtime.health_path")
    storage = contract.get("storage")
    if not isinstance(storage, dict) or storage.get("engine") != "SQLite" or storage.get("reset_mode") != "atomic_seed_replace":
        raise SyntheticReplayContractError("storage must use SQLite with atomic_seed_replace")
    seed_path = _relative(base, storage.get("seed_path"), "storage.seed_path", prefix="fixtures")
    runtime_path = Path(str(storage.get("runtime_path") or ""))
    if runtime_path.is_absolute() or not runtime_path.parts or runtime_path.parts[0] != "instance" or ".." in runtime_path.parts:
        raise SyntheticReplayContractError("storage.runtime_path must stay under instance/")
    if verify_artifacts:
        _sqlite_ok(seed_path)
        if sha256_file(seed_path) != storage.get("seed_sha256"):
            raise SyntheticReplayContractError("SQLite seed hash mismatch")
    auth = contract.get("auth")
    if not isinstance(auth, dict) or auth.get("mode") != "local_synthetic" or auth.get("initial_state") not in {"explicit_login", "default_authenticated"}:
        raise SyntheticReplayContractError("auth must be local_synthetic with an explicit initial state")
    if (
        auth.get("source_credentials_persisted") is not False
        or not isinstance(auth.get("demo_user_ids"), list)
        or not auth["demo_user_ids"]
        or any(not isinstance(item, str) or not OBJECT_ID_RE.fullmatch(item) for item in auth["demo_user_ids"])
    ):
        raise SyntheticReplayContractError("auth must declare demo users and exclude source credentials")
    reset = contract.get("reset")
    if not isinstance(reset, dict) or reset.get("supported") is not True or reset.get("method") != "POST":
        raise SyntheticReplayContractError("reset must expose a POST endpoint")
    _route(reset.get("endpoint"), "reset.endpoint")
    _route(reset.get("entrypoint"), "reset.entrypoint")
    evaluator = contract.get("evaluator")
    if not isinstance(evaluator, dict) or evaluator.get("result_field") != "passed":
        raise SyntheticReplayContractError("evaluator.result_field must be passed")
    _route(evaluator.get("endpoint_template").replace("{evaluator_id}", "probe") if isinstance(evaluator.get("endpoint_template"), str) else None, "evaluator.endpoint_template")
    network = contract.get("network_policy")
    if (
        not isinstance(network, dict)
        or network.get("external_requests") != 0
        or network.get("allowed_origins") != []
        or network.get("upstream_javascript_execution") is not False
    ):
        raise SyntheticReplayContractError("network policy must be local-only")
    content = contract.get("content_policy")
    if content != {
        "mode": "source_fidelity",
        "private_user_data_allowed": False,
        "source_assets_local_only": True,
    }:
        raise SyntheticReplayContractError("content_policy must permit only local source-fidelity assets without private data")
    capture, capture_paths = _capture_manifest(base, contract.get("capture_manifest") or {}, verify=verify_artifacts)
    if capture != contract.get("capture_manifest"):
        raise SyntheticReplayContractError("capture manifest descriptor is not canonical")
    capture_payload = _read_json(base / capture["path"])
    captured_routes = {_route(item.get("local_path"), "capture route.local_path") for item in capture_payload["routes"]}
    if not captured_routes.issubset(route_paths):
        raise SyntheticReplayContractError("capture manifest maps an undeclared local route")
    visual = contract.get("visual_reference")
    visual_path = _relative(base, (visual or {}).get("path"), "visual_reference.path", prefix="visual-reference")
    if (
        not isinstance(visual, dict)
        or not SHA256_RE.fullmatch(str(visual.get("sha256") or ""))
        or not SHA256_RE.fullmatch(str(visual.get("reference_sha256") or ""))
    ):
        raise SyntheticReplayContractError("visual reference descriptor is invalid")
    if verify_artifacts and (not visual_path.is_file() or sha256_file(visual_path) != visual["sha256"]):
        raise SyntheticReplayContractError("visual reference hash mismatch")
    tasks_desc = contract.get("tasks")
    tasks_path = _relative(base, (tasks_desc or {}).get("path"), "tasks.path")
    if not isinstance(tasks_desc, dict) or not SHA256_RE.fullmatch(str(tasks_desc.get("sha256") or "")):
        raise SyntheticReplayContractError("tasks descriptor is invalid")
    tasks = _load_tasks(
        tasks_path,
        route_paths,
        required_workflows=set(required_workflows),
        release_tier=release_tier,
    )
    if verify_artifacts and sha256_file(tasks_path) != tasks_desc["sha256"]:
        raise SyntheticReplayContractError("tasks hash mismatch")
    if tasks_desc.get("count") != len(tasks):
        raise SyntheticReplayContractError("tasks count mismatch")
    concurrency = contract.get("concurrency")
    if not isinstance(concurrency, dict) or concurrency.get("isolation") not in {"isolated_instance", "session_partitioned"}:
        raise SyntheticReplayContractError("concurrency.isolation is invalid")
    maximum = concurrency.get("max_parallel_sessions")
    if not isinstance(maximum, int) or maximum < 1:
        raise SyntheticReplayContractError("concurrency.max_parallel_sessions must be positive")
    if concurrency["isolation"] == "isolated_instance" and maximum != 1:
        raise SyntheticReplayContractError("isolated_instance replay sites permit exactly one session per runtime")
    if concurrency["isolation"] == "session_partitioned" and auth.get("state_scope") != "browser_session":
        raise SyntheticReplayContractError("session_partitioned sites must scope auth and state to browser_session")
    if concurrency["isolation"] == "isolated_instance" and auth.get("state_scope") != "instance":
        raise SyntheticReplayContractError("isolated_instance sites must scope auth and state to instance")
    return [seed_path, visual_path, tasks_path, *capture_paths]


def load_synthetic_replay_contract(path: Path, *, verify_artifacts: bool = True) -> dict[str, Any]:
    path = path.resolve()
    contract = _read_json(path)
    _validate_shape(contract, path.parent, verify_artifacts=verify_artifacts)
    return contract


def contract_artifact_paths(path: Path) -> list[Path]:
    path = path.resolve()
    contract = _read_json(path)
    return _validate_shape(contract, path.parent, verify_artifacts=True)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def compile_synthetic_replay_contract(spec_path: Path, output_path: Path) -> dict[str, Any]:
    """Compile a draft and copy all frozen inputs into a self-contained bundle."""
    spec_path = spec_path.resolve()
    output_path = output_path.resolve()
    spec = _read_json(spec_path)
    if spec.get("schema") != "webanything-synthetic-replay-spec-v1":
        raise SyntheticReplayContractError("unsupported synthetic replay spec")
    source_base = spec_path.parent
    output_base = output_path.parent
    contract = dict(spec)
    contract["schema"] = "webanything-synthetic-replay-contract-v1"
    contract["schema_version"] = 1
    contract["build_mode"] = "benchmark_synthetic_replay"
    source_capture = _read_json(_relative(source_base, (spec.get("capture_manifest") or {}).get("path"), "capture_manifest.path", prefix="capture"))
    for item in source_capture.get("files") or []:
        source = _relative(source_base, item.get("path"), "capture file", prefix="capture")
        if not source.is_file() or sha256_file(source) != item.get("sha256"):
            raise SyntheticReplayContractError(f"capture file hash mismatch: {item.get('path')}")
    source_visual_path = _relative(source_base, spec["visual_reference"]["path"], "visual_reference.path", prefix="visual-reference")
    source_visual = _read_json(source_visual_path)
    visual_artifacts: list[str] = []
    for route in source_visual.get("routes") or []:
        for key in ("screenshot", "comparison_screenshot", "geometry"):
            artifact = route.get(key) if isinstance(route, dict) else None
            if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
                visual_artifacts.append((Path(spec["visual_reference"]["path"]).parent / artifact["path"]).as_posix())
    copy_paths = [
        (spec["capture_manifest"]["path"], "capture_manifest"),
        (spec["storage"]["seed_path"], "storage.seed_path"),
        (spec["visual_reference"]["path"], "visual_reference.path"),
        (spec["tasks"]["path"], "tasks.path"),
    ] + [(item["path"], "capture file") for item in source_capture.get("files") or []] + [
        (path, "visual reference artifact") for path in visual_artifacts
    ]
    for raw, label in copy_paths:
        source = _relative(source_base, raw, label)
        target = _relative(output_base, raw, label)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target:
            shutil.copy2(source, target)
    seed = _relative(output_base, contract["storage"]["seed_path"], "storage.seed_path", prefix="fixtures")
    _sqlite_ok(seed)
    contract["storage"] = {**contract["storage"], "seed_sha256": sha256_file(seed)}
    capture, _ = _capture_manifest(output_base, contract["capture_manifest"], verify=True)
    contract["capture_manifest"] = capture
    visual = _relative(output_base, contract["visual_reference"]["path"], "visual_reference.path", prefix="visual-reference")
    contract["visual_reference"] = {**contract["visual_reference"], "sha256": sha256_file(visual)}
    tasks = _relative(output_base, contract["tasks"]["path"], "tasks.path")
    route_paths = {_route(item.get("path"), "route.path") for item in contract.get("routes") or []}
    coverage = contract.get("coverage") or {}
    loaded_tasks = _load_tasks(
        tasks,
        route_paths,
        required_workflows=set(coverage.get("required_workflows") or []),
        release_tier=str(coverage.get("release_tier") or ""),
    )
    contract["tasks"] = {"path": contract["tasks"]["path"], "sha256": sha256_file(tasks), "count": len(loaded_tasks)}
    _validate_shape(contract, output_base, verify_artifacts=True)
    _atomic_json(output_path, contract)
    return contract
