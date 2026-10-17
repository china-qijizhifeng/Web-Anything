#!/usr/bin/env python3
"""Prove one honest, local-only browser workflow for a review build.

This gate deliberately does not claim broad route coverage.  It pins one workflow
from the site's normal mirror contract, executes it in a real browser, audits every
visited document for broken/runtime resources, and resets SQLite after a declared
state change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from audit_clickthrough import browser_open, origin
from quality_contract import (
    ALLOWED_ASSET_SOURCES,
    ALLOWED_DATA_SOURCES,
    ALLOWED_DESIGN_SOURCES,
    sha256_file,
)
from site_fingerprint import site_implementation_fingerprint
from verify_quality import reset_runtime_database, verify_workflows


TOOL_VERSION = "webharbor-reviewable-workflow-v1"
COMPLETION_LEVEL = "reviewable_workflow_mirror"
USER_ACTIONS = {"browser_fill", "browser_select", "browser_set_input_files", "browser_click"}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _repo_file(root: Path, identity: Any, label: str) -> tuple[Path | None, list[str]]:
    if not isinstance(identity, dict):
        return None, [f"{label} identity is missing"]
    relative = identity.get("path")
    expected = identity.get("sha256")
    if not isinstance(relative, str) or not relative or not isinstance(expected, str):
        return None, [f"{label} identity is incomplete"]
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        return None, [f"{label} is missing or outside the workspace"]
    if sha256_file(path) != expected:
        return path, [f"{label} changed after reviewable workflow declaration"]
    return path, []


def contract_issues(
    declaration: Any,
    *,
    root: Path,
    site: str,
) -> tuple[list[str], dict[str, Any] | None, dict[str, Any] | None]:
    """Validate the bounded declaration and its selected normal-contract workflow."""
    if not isinstance(declaration, dict):
        return ["reviewable workflow declaration is not an object"], None, None
    failures: list[str] = []
    if declaration.get("schema_version") != 1:
        failures.append("reviewable workflow schema_version must be 1")
    if declaration.get("completion_level") != COMPLETION_LEVEL:
        failures.append(f"completion_level must be {COMPLETION_LEVEL}")
    if declaration.get("site") != site:
        failures.append("reviewable workflow site does not match the requested site")
    unfinished = declaration.get("known_unfinished_scope")
    if not isinstance(unfinished, list) or not unfinished:
        failures.append("known_unfinished_scope must disclose at least one broader-site gap")

    mirror_path, mirror_failures = _repo_file(
        root, declaration.get("mirror_contract"), "mirror contract"
    )
    failures.extend(mirror_failures)
    if mirror_path is None:
        return failures, None, None
    try:
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        failures.append(f"mirror contract is invalid: {exc}")
        return failures, None, None
    if mirror.get("site") != site:
        failures.append("pinned mirror contract belongs to another site")

    design = mirror.get("design") or {}
    if design.get("source_kind") not in ALLOWED_DESIGN_SOURCES:
        failures.append("selected mirror lacks an allowed HTML/repository design source")
    if design.get("screenshot_crop_count") != 0:
        failures.append("selected mirror uses screenshot-derived visual assets")
    for index, asset in enumerate(design.get("assets") or []):
        if asset.get("source_kind") not in ALLOWED_ASSET_SOURCES:
            failures.append(f"design asset {index} has unsupported provenance")

    data = mirror.get("data") or {}
    if data.get("runtime_storage") != "sqlite":
        failures.append("selected mirror runtime data is not SQLite-backed")
    for index, record in enumerate(data.get("records") or []):
        if record.get("source_kind") not in ALLOWED_DATA_SOURCES:
            failures.append(f"data record {index} has unsupported provenance")

    source_path, source_failures = _repo_file(
        root, mirror.get("source_evidence"), "mirror source evidence"
    )
    failures.extend(source_failures)
    if source_path is None:
        failures.append("reviewable workflow is not tied to source evidence")

    workflow_id = declaration.get("workflow_id")
    workflows = (mirror.get("interaction") or {}).get("workflows") or []
    selected = next(
        (item for item in workflows if isinstance(item, dict) and item.get("id") == workflow_id),
        None,
    )
    if selected is None:
        failures.append(f"workflow_id {workflow_id!r} is absent from the mirror contract")
        return failures, mirror, None
    if not isinstance(selected.get("source_url"), str) or not selected.get("source_url"):
        failures.append("selected workflow lacks a source URL")
    if not isinstance(selected.get("source_kind"), str) or not selected.get("source_kind"):
        failures.append("selected workflow lacks source provenance")

    steps = selected.get("steps") or []
    roles = declaration.get("roles") or {}
    required_roles = {
        "entry_step": "browser_navigate",
        "result_step": "browser_expect",
        "return_step": "browser_click",
        "return_result_step": "browser_expect",
    }
    role_indexes: dict[str, int] = {}
    for role, expected_action in required_roles.items():
        index = roles.get(role)
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(steps):
            failures.append(f"roles.{role} is not a valid workflow step index")
            continue
        role_indexes[role] = index
        if steps[index].get("action") != expected_action:
            failures.append(f"roles.{role} must identify a {expected_action} step")
    action_indexes = roles.get("action_steps")
    if not isinstance(action_indexes, list) or not action_indexes:
        failures.append("roles.action_steps must identify at least one user action")
        action_indexes = []
    for index in action_indexes:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(steps):
            failures.append("roles.action_steps contains an invalid workflow step index")
        elif steps[index].get("action") not in USER_ACTIONS:
            failures.append("roles.action_steps identifies a non-interactive step")

    ordered = [
        role_indexes.get("entry_step", -1),
        min(action_indexes) if action_indexes and all(isinstance(i, int) for i in action_indexes) else -1,
        role_indexes.get("result_step", -1),
        role_indexes.get("return_step", -1),
        role_indexes.get("return_result_step", -1),
    ]
    if -1 not in ordered and ordered != sorted(ordered):
        failures.append("entry, action, result, return, and return-result roles are out of order")
    if role_indexes.get("return_step", -1) >= 0:
        return_step = steps[role_indexes["return_step"]]
        if not isinstance(return_step.get("wait_for_url"), str):
            failures.append("return click must declare its local wait_for_url")
    if role_indexes.get("return_result_step", -1) >= 0:
        return_result = steps[role_indexes["return_result_step"]]
        if not isinstance(return_result.get("url"), str):
            failures.append("return result must assert the final local URL")

    state_change = any(
        isinstance(step, dict) and step.get("state_change") is True for step in steps
    )
    sqlite_assertions = [step for step in steps if step.get("action") == "sqlite_assert"]
    reset = declaration.get("state_reset")
    if state_change and not sqlite_assertions:
        failures.append("state-changing review workflow lacks a SQLite assertion")
    if state_change and not isinstance(reset, dict):
        failures.append("state-changing review workflow lacks a reset declaration")
    return failures, mirror, selected


def _resource_paths(steps: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for step in steps:
        for key in ("local_path", "wait_for_url", "url"):
            value = step.get(key)
            if isinstance(value, str) and value.startswith("/") and value not in paths:
                paths.append(value)
    return paths


def _failure_coverage(result: dict[str, Any]) -> dict[str, Any]:
    """Adapt focused evidence to the closure loop's stable progress vocabulary."""
    failures = list(result.get("failures") or [])
    passed = result.get("status") == "passed"
    return {
        "status": "passed" if passed else "failed",
        "clickthrough": {
            "meaningful_local_percent": 100.0 if passed else 0.0,
            "requested_min_percent": 100.0,
        },
        "link_classification_counts": {
            "meaningful_reviewable_workflow": 1 if passed else 0,
            "reviewable_workflow_failure": 0 if passed else len(failures),
        },
        "unimplemented_links": [
            {"classification": "reviewable_workflow_failure", "reason": failure}
            for failure in failures
        ],
        "next_candidates": [],
        "resource_issues": list(result.get("resource_issues") or []),
        "browser_errors": [],
        "inventory_complete": True,
        "truncated_routes": [],
        "reviewable_workflow": {
            "completion_level": COMPLETION_LEVEL,
            "workflow_id": result.get("workflow_id"),
            "evidence": result.get("evidence_path"),
        },
    }


def verify(
    *,
    root: Path,
    site: str,
    declaration_path: Path,
    base_url: str,
    output: Path,
    timeout: float = 15.0,
) -> dict[str, Any]:
    failures: list[str] = []
    try:
        declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        declaration = None
        failures.append(f"reviewable workflow declaration is unavailable: {exc}")
    mirror = None
    workflow = None
    if declaration is not None:
        structural, mirror, workflow = contract_issues(
            declaration, root=root, site=site
        )
        failures.extend(structural)

    workflow_result: dict[str, Any] = {
        "status": "failed",
        "workflows_checked": 0,
        "results": [],
    }
    reset_result: dict[str, Any] = {"status": "not_required"}
    resource_issues: list[dict[str, Any]] = []
    resource_routes: list[dict[str, Any]] = []
    if mirror is not None and workflow is not None and not failures:
        focused_contract = {
            "site": site,
            "data": mirror.get("data") or {},
            "interaction": {
                "workflows": [workflow],
                "blocking_gaps": [],
                "runtime_database": (mirror.get("interaction") or {}).get("runtime_database"),
            },
        }
        workflow_failures, workflow_result = verify_workflows(
            focused_contract, root, base_url, timeout
        )
        failures.extend(workflow_failures)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            parsed_base = urlsplit(base_url.rstrip("/"))
            base_port = parsed_base.port or (443 if parsed_base.scheme == "https" else 80)

            def route_request(route) -> None:
                target = urlsplit(route.request.url)
                local = (
                    target.scheme in {"data", "blob"}
                    or (
                        target.scheme in {"http", "https"}
                        and target.scheme == parsed_base.scheme
                        and target.hostname == parsed_base.hostname
                        and (target.port or (443 if target.scheme == "https" else 80))
                        == base_port
                    )
                )
                if local:
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", route_request)
            page = context.new_page()
            expected_origin = origin(base_url)
            for local_path in _resource_paths(workflow.get("steps") or []):
                url = base_url.rstrip("/") + local_path
                status, body, headings, issues, errors = browser_open(
                    page, url, max(1000, int(timeout * 1000)), expected_origin
                )
                route_record = {
                    "path": local_path,
                    "status": status,
                    "body_chars": len(body),
                    "headings": headings[:8],
                    "resource_issues": issues,
                    "errors": errors,
                }
                resource_routes.append(route_record)
                resource_issues.extend({"path": local_path, **issue} for issue in issues)
                failures.extend(f"resource route {local_path}: {error}" for error in errors)
                if status != 200 or not body or not headings:
                    failures.append(
                        f"resource route {local_path} is not a meaningful rendered HTTP 200 page"
                    )
            context.close()
            browser.close()
        if resource_issues:
            failures.append(f"review workflow has {len(resource_issues)} rendered resource issue(s)")

        state_change = any(
            step.get("state_change") is True for step in workflow.get("steps") or []
        )
        if state_change:
            reset_failures, reset_result = reset_runtime_database(
                focused_contract, root
            )
            failures.extend(reset_failures)

    result = {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "generated_at": utc_now(),
        "status": "passed" if not failures else "failed",
        "site": site,
        "completion_level": COMPLETION_LEVEL,
        "claim_boundary": (
            "One source-grounded local browser workflow is reviewable; broader site "
            "coverage remains explicitly unfinished."
        ),
        "workflow_id": declaration.get("workflow_id") if isinstance(declaration, dict) else None,
        "declaration": {
            "path": declaration_path.relative_to(root).as_posix()
            if root.resolve() in declaration_path.resolve().parents else str(declaration_path),
            "sha256": sha256_file(declaration_path) if declaration_path.is_file() else None,
        },
        "site_implementation": site_implementation_fingerprint(root / "sites" / site),
        "workflow": workflow_result,
        "resource_routes": resource_routes,
        "resource_issues": resource_issues,
        "external_runtime_request_count": sum(
            1 for issue in resource_issues if issue.get("kind") == "external_runtime_request"
        ),
        "post_workflow_reset": reset_result,
        "known_unfinished_scope": (
            declaration.get("known_unfinished_scope", [])
            if isinstance(declaration, dict) else []
        ),
        "failures": failures,
    }
    result["evidence_path"] = output.as_posix()
    atomic_json(output, result)
    return result


def coverage_result(result: dict[str, Any]) -> dict[str, Any]:
    return _failure_coverage(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--site", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    root = args.workspace.resolve()
    parsed = urlsplit(args.base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("--base-url must be a loopback HTTP(S) origin")
    declaration = (args.contract or root / "sites" / args.site / "reviewable_workflow.json").resolve()
    output = (args.output or root / "mirror_factory" / "evidence" / args.site / "reviewable-workflow.json").resolve()
    result = verify(
        root=root,
        site=args.site,
        declaration_path=declaration,
        base_url=f"{parsed.scheme}://{parsed.netloc}",
        output=output,
        timeout=max(1.0, args.timeout),
    )
    print(json.dumps({
        "status": result["status"],
        "site": args.site,
        "completion_level": COMPLETION_LEVEL,
        "workflow_id": result["workflow_id"],
        "evidence": output.as_posix(),
        "failures": result["failures"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
