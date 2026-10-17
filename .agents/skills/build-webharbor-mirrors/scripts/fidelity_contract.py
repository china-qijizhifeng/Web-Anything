#!/usr/bin/env python3
"""Current fidelity evidence contract shared by builders and status tools."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


TOOL_VERSION = "webharbor-fidelity-v19"
FIDELITY_CONTRACT_VERSION = 3
MINIMUM_NONBLANK_RETENTION = 0.5


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_local_entry_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (parsed.path or "/") == "/" and not parsed.query


def _entry_route_issues(evidence: dict[str, Any]) -> list[str]:
    results = evidence.get("results")
    if not isinstance(results, list):
        return []
    entry_results = [
        result for result in results
        if isinstance(result, dict) and _is_local_entry_url(result.get("local_url"))
    ]
    issues: list[str] = []
    if not entry_results:
        issues.append("fidelity evidence does not cover the local entry route '/'")
    excluded = evidence.get("excluded_routes", [])
    if isinstance(excluded, list) and any(
        isinstance(route, dict) and _is_local_entry_url(route.get("local_url"))
        for route in excluded
    ):
        issues.append("the local entry route '/' cannot be excluded from fidelity")
    return issues


def _spec_route_issues(spec: Any, evidence: dict[str, Any]) -> list[str]:
    if not isinstance(spec, dict):
        return ["fidelity spec is not a JSON object"]
    routes = spec.get("routes")
    if not isinstance(routes, list) or not any(
        isinstance(route, dict) and _is_local_entry_url(route.get("local_url"))
        for route in routes
    ):
        return ["fidelity spec does not include the mandatory local entry route '/'"]
    excluded = spec.get("excluded_routes", [])
    if isinstance(excluded, list) and any(
        isinstance(route, dict) and _is_local_entry_url(route.get("local_url"))
        for route in excluded
    ):
        return ["fidelity spec excludes the mandatory local entry route '/'"]
    result_names = {
        result.get("route") for result in evidence.get("results", [])
        if isinstance(result, dict)
    }
    entry_names = {
        route.get("name") for route in routes
        if isinstance(route, dict) and _is_local_entry_url(route.get("local_url"))
    }
    if not entry_names.issubset(result_names):
        return ["fidelity results do not include every entry-route spec record"]
    return []


def _input_hash_issues(evidence: dict[str, Any], root: Path) -> list[str]:
    issues: list[str] = []
    spec = evidence.get("spec")
    if not isinstance(spec, dict):
        return ["missing fidelity spec identity"]
    relative = spec.get("path")
    expected_hash = spec.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected_hash, str):
        return ["incomplete fidelity spec identity"]
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        return ["fidelity spec is missing or outside the repository"]
    if _sha256_file(path) != expected_hash:
        issues.append("fidelity spec changed after evidence generation")
    else:
        try:
            spec_value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            issues.append("fidelity spec cannot be parsed")
        else:
            issues.extend(_spec_route_issues(spec_value, evidence))

    site = evidence.get("site")
    if evidence.get("upstream_source_mode") == "captured_replay" and isinstance(site, str):
        capture_path = root / "mirror_factory" / "evidence" / site / "upstream.json"
        expected_capture_hash = evidence.get("upstream_capture_evidence_sha256")
        if not capture_path.is_file() or not isinstance(expected_capture_hash, str):
            issues.append("captured replay evidence identity is missing")
        elif _sha256_file(capture_path) != expected_capture_hash:
            issues.append("captured upstream evidence changed after fidelity verification")
    return issues


def fidelity_evidence_issues(
    evidence: Any,
    *,
    root: Path | None = None,
) -> list[str]:
    """Return every reason evidence cannot count as a current fidelity pass."""
    if not isinstance(evidence, dict):
        return ["fidelity evidence is not a JSON object"]
    issues: list[str] = []
    if evidence.get("status") != "passed":
        issues.append(f"fidelity status is {evidence.get('status')!r}, not 'passed'")
    if evidence.get("tool") != TOOL_VERSION:
        issues.append(f"fidelity tool is not {TOOL_VERSION}")
    if evidence.get("fidelity_contract_version") != FIDELITY_CONTRACT_VERSION:
        issues.append(
            f"fidelity contract is not version {FIDELITY_CONTRACT_VERSION}"
        )
    if evidence.get("failures") != []:
        issues.append("fidelity evidence contains failures or lacks an empty failure list")
    if evidence.get("require_complete_resources") is not True:
        issues.append("complete-resource enforcement was not enabled")
    if evidence.get("runtime_javascript_check") is not True:
        issues.append("JavaScript runtime verification was not enabled")
    minimum_retention = evidence.get("minimum_nonblank_retention")
    if (
        not isinstance(minimum_retention, (int, float))
        or isinstance(minimum_retention, bool)
        or minimum_retention < MINIMUM_NONBLANK_RETENTION
    ):
        issues.append(
            f"minimum nonblank retention is below {MINIMUM_NONBLANK_RETENTION}"
        )
    results = evidence.get("results")
    if not isinstance(results, list) or not results:
        issues.append("fidelity evidence has no route results")
    else:
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                issues.append(f"route result {index} is not an object")
                continue
            if not isinstance(result.get("nonblank_retention"), (int, float)):
                issues.append(f"route result {index} lacks nonblank retention")
            for side in ("upstream", "local", "runtime_local"):
                capture = result.get(side)
                if not isinstance(capture, dict):
                    issues.append(f"route result {index} lacks {side} capture")
                    continue
                integrity = capture.get("render_integrity")
                if not isinstance(integrity, dict):
                    issues.append(f"route result {index} lacks {side} resource integrity")
                    continue
                settle = integrity.get("settle")
                if not isinstance(settle, dict) or settle.get("stable") is not True:
                    issues.append(f"route result {index} has unstable {side} capture")
        issues.extend(_entry_route_issues(evidence))
    if root is not None:
        issues.extend(_input_hash_issues(evidence, root))
    return issues


def fidelity_evidence_state(evidence: Any, *, root: Path | None = None) -> str:
    """Classify evidence as a current pass, current failure, or stale contract."""
    if isinstance(evidence, dict) and (
        evidence.get("tool") == TOOL_VERSION
        and evidence.get("fidelity_contract_version") == FIDELITY_CONTRACT_VERSION
    ):
        if evidence.get("status") != "passed":
            return "failed_current"
        return "passed_current" if not fidelity_evidence_issues(evidence, root=root) else "stale"
    return "stale"
