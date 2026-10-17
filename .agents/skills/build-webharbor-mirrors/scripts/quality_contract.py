#!/usr/bin/env python3
"""Three-fidelity contract shared by builders, verifiers, and queue tools."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Any


TOOL_VERSION = "webharbor-quality-verifier-v1"
QUALITY_CONTRACT_VERSION = 1
PASSING_GRADES = {"full_fidelity", "synthetic_grounded_data"}
ALLOWED_DESIGN_SOURCES = {
    "upstream_html_dom_css",
    "historical_html_dom_geometry",
    "official_repository",
}
ALLOWED_ASSET_SOURCES = {
    "upstream_capture",
    "historical_original_asset",
    "official_repository",
    "generated_neutral",
    "generated_ai",
}
ALLOWED_DATA_SOURCES = {
    "upstream_capture",
    "historical_html",
    "official_repository",
    "synthetic_grounded",
}
SAFE_SITE_RE = re.compile(r"[a-z0-9_]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_UPLOAD_FIXTURE_BYTES = 1024 * 1024
BROWSER_ACTIONS = {
    "browser_navigate", "browser_fill", "browser_select",
    "browser_set_input_files", "browser_click", "browser_expect",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_upload_fixture(root: Path, site: str, value: Any) -> Path:
    """Resolve one hash-pinned synthetic browser fixture inside its site."""
    if not isinstance(value, dict):
        raise ValueError("upload fixture identity is missing")
    relative = value.get("path")
    expected_sha256 = value.get("sha256")
    expected_bytes = value.get("bytes")
    media_type = value.get("media_type")
    disclosure = value.get("disclosure")
    if not isinstance(relative, str) or not relative:
        raise ValueError("upload fixture path is missing")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("upload fixture path is unsafe")
    if not isinstance(expected_sha256, str) or SHA256_RE.fullmatch(expected_sha256) is None:
        raise ValueError("upload fixture SHA256 is invalid")
    if not isinstance(expected_bytes, int) or not 0 < expected_bytes <= MAX_UPLOAD_FIXTURE_BYTES:
        raise ValueError(
            f"upload fixture size must be between 1 and {MAX_UPLOAD_FIXTURE_BYTES} bytes"
        )
    if not isinstance(media_type, str) or not media_type:
        raise ValueError("upload fixture media type is missing")
    if value.get("synthetic") is not True:
        raise ValueError("upload fixture is not declared synthetic")
    if not isinstance(disclosure, str) or not disclosure:
        raise ValueError("upload fixture disclosure is missing")
    if not isinstance(site, str) or SAFE_SITE_RE.fullmatch(site) is None:
        raise ValueError("upload fixture site id is invalid")

    root = root.resolve()
    raw_path = root / relative_path
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("upload fixture path contains a symlink")
    path = raw_path.resolve()
    fixture_root = (root / "sites" / site / "quality_fixtures").resolve()
    if fixture_root not in path.parents or not path.is_file():
        raise ValueError(
            "upload fixture is missing or outside sites/<site>/quality_fixtures"
        )
    actual_bytes = path.stat().st_size
    if actual_bytes != expected_bytes:
        raise ValueError(
            f"upload fixture size {actual_bytes} differs from declared {expected_bytes}"
        )
    if sha256_file(path) != expected_sha256:
        raise ValueError("upload fixture bytes changed after the contract was generated")
    return path


def _verified_repo_file(
    value: Any,
    *,
    root: Path,
    label: str,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} identity is missing"]
    relative = value.get("path")
    expected = value.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        return [f"{label} identity is incomplete"]
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        return [f"{label} is missing or outside the repository"]
    if sha256_file(path) != expected:
        return [f"{label} changed after the contract was generated"]
    return []


def quality_contract_issues(
    contract: Any,
    *,
    root: Path | None = None,
) -> list[str]:
    """Return structural and provenance errors in a mirror contract."""
    if not isinstance(contract, dict):
        return ["mirror contract is not a JSON object"]
    issues: list[str] = []
    if contract.get("quality_contract_version") != QUALITY_CONTRACT_VERSION:
        issues.append(f"quality contract is not version {QUALITY_CONTRACT_VERSION}")
    site = contract.get("site")
    if not isinstance(site, str) or SAFE_SITE_RE.fullmatch(site) is None:
        issues.append("mirror contract has an invalid site id")

    design = contract.get("design")
    if not isinstance(design, dict):
        issues.append("design inventory is missing")
    else:
        if design.get("source_kind") not in ALLOWED_DESIGN_SOURCES:
            issues.append("design source is missing, unsupported, or screenshot-derived")
        assets = design.get("assets")
        if not isinstance(assets, list):
            issues.append("asset provenance inventory is missing")
            assets = []
        screenshot_crops = 0
        for index, asset in enumerate(assets):
            if not isinstance(asset, dict):
                issues.append(f"asset {index} is not an object")
                continue
            source_kind = asset.get("source_kind")
            if source_kind == "screenshot_crop":
                screenshot_crops += 1
            elif source_kind not in ALLOWED_ASSET_SOURCES:
                issues.append(f"asset {index} has an unsupported source kind")
            for field in ("source_url", "source_sha256", "replay_sha256"):
                if not isinstance(asset.get(field), str) or not asset[field]:
                    issues.append(f"asset {index} lacks {field}")
            if source_kind in {"generated_neutral", "generated_ai"}:
                disclosure = asset.get("disclosure")
                derivation = asset.get("derivation")
                dimensions = asset.get("dimensions")
                if not isinstance(disclosure, dict) or not disclosure.get("reason"):
                    issues.append(f"generated asset {index} is not disclosed")
                if not isinstance(derivation, dict) or not (
                    derivation.get("tool") and derivation.get("version")
                    and derivation.get("seed") is not None
                ):
                    issues.append(f"generated asset {index} lacks generator provenance")
                if not isinstance(dimensions, dict) or not (
                    dimensions.get("vector") is True
                    or (
                        isinstance(dimensions.get("width"), int)
                        and dimensions["width"] > 0
                        and isinstance(dimensions.get("height"), int)
                        and dimensions["height"] > 0
                    )
                ):
                    issues.append(f"generated asset {index} lacks dimensions")
        declared_crops = design.get("screenshot_crop_count")
        if declared_crops != screenshot_crops:
            issues.append("screenshot crop count does not match the asset inventory")
        viewport = design.get("viewport")
        if viewport is not None and not (
            isinstance(viewport, dict)
            and type(viewport.get("width")) is int
            and viewport["width"] > 0
            and type(viewport.get("height")) is int
            and viewport["height"] > 0
        ):
            issues.append("design viewport must contain positive integer width and height")

    data = contract.get("data")
    if not isinstance(data, dict):
        issues.append("data provenance inventory is missing")
    else:
        if data.get("runtime_storage") != "sqlite":
            issues.append("runtime data storage is not SQLite")
        database = data.get("database")
        if not isinstance(database, dict) or not isinstance(database.get("path"), str):
            issues.append("seed database identity is missing")
        records = data.get("records")
        if not isinstance(records, list) or not records:
            issues.append("data provenance inventory has no records")
            records = []
        actual_counts: Counter[str] = Counter()
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                issues.append(f"data record {index} is not an object")
                continue
            source_kind = record.get("source_kind")
            actual_counts[str(source_kind)] += 1
            if source_kind not in ALLOWED_DATA_SOURCES:
                issues.append(f"data record {index} has an unsupported source kind")
            for field in ("source_url", "source_sha256", "value_sha256", "storage"):
                if field not in record:
                    issues.append(f"data record {index} lacks {field}")
            if source_kind == "synthetic_grounded":
                grounding = record.get("grounding")
                generator = record.get("generator")
                if not isinstance(grounding, dict) or not grounding.get("sources"):
                    issues.append(f"synthetic data record {index} lacks grounding sources")
                else:
                    for source_index, source in enumerate(grounding["sources"]):
                        if not isinstance(source, dict) or not (
                            isinstance(source.get("url"), str)
                            and isinstance(source.get("sha256"), str)
                        ):
                            issues.append(
                                f"synthetic data record {index} grounding source {source_index} is incomplete"
                            )
                if not isinstance(generator, dict) or not (
                    generator.get("tool") and generator.get("version")
                    and generator.get("seed") is not None
                ):
                    issues.append(
                        f"synthetic data record {index} lacks deterministic generator provenance"
                    )
        declared_counts = data.get("source_counts")
        if not isinstance(declared_counts, dict) or {
            key: value for key, value in sorted(actual_counts.items())
        } != declared_counts:
            issues.append("data source counts do not match the record inventory")
        if actual_counts.get("synthetic_grounded"):
            disclosure = data.get("synthetic_disclosure")
            if not isinstance(disclosure, dict) or not disclosure.get("reason"):
                issues.append("synthetic grounded data is not disclosed")

    interaction = contract.get("interaction")
    if not isinstance(interaction, dict):
        issues.append("interaction coverage inventory is missing")
    else:
        workflows = interaction.get("workflows")
        if not isinstance(workflows, list) or not workflows:
            issues.append("interaction inventory has no declared workflows")
        else:
            identifiers: set[str] = set()
            for index, workflow in enumerate(workflows):
                if not isinstance(workflow, dict):
                    issues.append(f"workflow {index} is not an object")
                    continue
                identifier = workflow.get("id")
                if not isinstance(identifier, str) or not identifier:
                    issues.append(f"workflow {index} lacks an id")
                elif identifier in identifiers:
                    issues.append(f"workflow id is duplicated: {identifier}")
                else:
                    identifiers.add(identifier)
                steps = workflow.get("steps")
                if not isinstance(steps, list) or not steps:
                    issues.append(f"workflow {index} has no declarative steps")
                    continue
                has_state_change = False
                has_state_assertion = False
                action_families: set[str] = set()
                sqlite_seen = False
                for step_index, step in enumerate(steps):
                    if not isinstance(step, dict):
                        issues.append(f"workflow {index} step {step_index} is not an object")
                        continue
                    action = step.get("action")
                    if action == "http":
                        action_families.add("http")
                        method = step.get("method", "GET")
                        if method not in {"GET", "POST"}:
                            issues.append(
                                f"workflow {index} step {step_index} has unsupported HTTP method"
                            )
                        if not isinstance(step.get("local_path"), str):
                            issues.append(
                                f"workflow {index} step {step_index} lacks a local path"
                            )
                        if method == "POST":
                            has_state_change = True
                            if not isinstance(step.get("form", step.get("json")), dict):
                                issues.append(
                                    f"workflow {index} POST step {step_index} lacks form or JSON data"
                                )
                    elif action == "sqlite_assert":
                        sqlite_seen = True
                        has_state_assertion = True
                        for field in ("table", "where"):
                            if field not in step:
                                issues.append(
                                    f"workflow {index} SQLite step {step_index} lacks {field}"
                                )
                    elif action in BROWSER_ACTIONS:
                        action_families.add("browser")
                        if sqlite_seen:
                            issues.append(
                                f"workflow {index} browser step {step_index} follows a SQLite assertion"
                            )
                        if action == "browser_navigate" and not isinstance(
                            step.get("local_path"), str
                        ):
                            issues.append(
                                f"workflow {index} browser navigation {step_index} lacks a local path"
                            )
                        if action in {
                            "browser_fill", "browser_select", "browser_set_input_files",
                            "browser_click",
                        } and not isinstance(
                            step.get("selector"), str
                        ):
                            issues.append(
                                f"workflow {index} browser step {step_index} lacks a selector"
                            )
                        if action == "browser_set_input_files":
                            fixture = step.get("fixture")
                            if root is None:
                                if not isinstance(fixture, dict):
                                    issues.append(
                                        f"workflow {index} upload step {step_index} lacks a fixture"
                                    )
                            else:
                                try:
                                    resolve_upload_fixture(root, site, fixture)
                                except ValueError as exc:
                                    issues.append(
                                        f"workflow {index} upload step {step_index}: {exc}"
                                    )
                        if action == "browser_expect":
                            if "selector" in step and not isinstance(step["selector"], str):
                                issues.append(
                                    f"workflow {index} browser expectation {step_index} has an invalid selector"
                                )
                            if "visible" in step and not isinstance(step["visible"], bool):
                                issues.append(
                                    f"workflow {index} browser expectation {step_index} has invalid visibility"
                                )
                            attributes = step.get("attributes", {})
                            if not isinstance(attributes, dict) or not all(
                                isinstance(name, str) and isinstance(value, str)
                                for name, value in attributes.items()
                            ):
                                issues.append(
                                    f"workflow {index} browser expectation {step_index} has invalid attributes"
                                )
                        if step.get("state_change") is True:
                            has_state_change = True
                    else:
                        issues.append(
                            f"workflow {index} step {step_index} has unsupported action"
                        )
                if has_state_change and not has_state_assertion:
                    issues.append(
                        f"workflow {index} changes state without a SQLite assertion"
                    )
                if len(action_families) > 1:
                    issues.append(
                        f"workflow {index} mixes HTTP and browser action families"
                    )
        if not isinstance(interaction.get("blocking_gaps"), list):
            issues.append("interaction blocking-gap inventory is missing")

    if root is not None:
        issues.extend(_verified_repo_file(
            contract.get("source_evidence"), root=root, label="source evidence"
        ))
    return issues


def quality_gate_failures(contract: Any) -> list[str]:
    """Return honest quality blockers from a structurally valid contract."""
    if not isinstance(contract, dict):
        return ["mirror contract is unavailable"]
    failures: list[str] = []
    design = contract.get("design", {})
    crops = design.get("screenshot_crop_count")
    if crops:
        failures.append(f"design inventory contains {crops} screenshot-derived crop(s)")
    interaction = contract.get("interaction", {})
    for gap in interaction.get("blocking_gaps", []):
        if isinstance(gap, dict):
            failures.append(
                f"interaction gap {gap.get('kind', 'unknown')}: {gap.get('count', 1)}"
            )
        else:
            failures.append(f"interaction gap: {gap}")
    return failures


def quality_evidence_issues(
    evidence: Any,
    *,
    root: Path | None = None,
) -> list[str]:
    """Return every reason evidence cannot count as a current three-fidelity pass."""
    if not isinstance(evidence, dict):
        return ["quality evidence is not a JSON object"]
    issues: list[str] = []
    if evidence.get("status") != "passed":
        issues.append(f"quality status is {evidence.get('status')!r}, not 'passed'")
    if evidence.get("tool") != TOOL_VERSION:
        issues.append(f"quality tool is not {TOOL_VERSION}")
    if evidence.get("quality_contract_version") != QUALITY_CONTRACT_VERSION:
        issues.append(f"quality contract is not version {QUALITY_CONTRACT_VERSION}")
    if evidence.get("grade") not in PASSING_GRADES:
        issues.append("quality grade is not a passing three-fidelity grade")
    if evidence.get("failures") != []:
        issues.append("quality evidence contains failures or lacks an empty failure list")
    for dimension in ("design", "data", "interaction"):
        result = evidence.get(dimension)
        if not isinstance(result, dict) or result.get("status") != "passed":
            issues.append(f"{dimension} fidelity did not pass")
    reset = evidence.get("post_workflow_reset")
    if not isinstance(reset, dict) or (
        not reset.get("seed_sha256")
        or reset.get("runtime_sha256") != reset.get("seed_sha256")
    ):
        issues.append("post-workflow runtime reset is missing or not byte-identical")
    if root is not None:
        issues.extend(_verified_repo_file(
            evidence.get("contract"), root=root, label="mirror contract"
        ))
        issues.extend(_verified_repo_file(
            evidence.get("fidelity_evidence"), root=root, label="fidelity evidence"
        ))
    return issues


def quality_evidence_state(evidence: Any, *, root: Path | None = None) -> str:
    """Classify quality evidence as a current pass, current failure, or stale proof."""
    if isinstance(evidence, dict) and (
        evidence.get("tool") == TOOL_VERSION
        and evidence.get("quality_contract_version") == QUALITY_CONTRACT_VERSION
    ):
        if evidence.get("status") != "passed":
            return "failed_current"
        return "passed_current" if not quality_evidence_issues(evidence, root=root) else "stale"
    return "stale"
