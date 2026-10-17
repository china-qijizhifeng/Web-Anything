#!/usr/bin/env python3
"""Verify design, data, and interaction fidelity for one running mirror."""

from __future__ import annotations

import argparse
import http.cookiejar
import hashlib
import json
import os
import re
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from browser_workflows import BROWSER_ACTIONS, verify_browser_steps
from fidelity_contract import fidelity_evidence_issues
from quality_contract import (
    QUALITY_CONTRACT_VERSION,
    TOOL_VERSION,
    quality_contract_issues,
    quality_gate_failures,
    sha256_file,
)
from site_fingerprint import site_implementation_fingerprint


SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def repository_root() -> Path:
    # Resolve from the nearest parent that owns the site/evidence trees. Trace
    # workspaces may wrap this skill one extra directory below the repository.
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / "sites").is_dir() and (parent / "mirror_factory").is_dir():
            return parent
    return script_path.parents[4]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or SAFE_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"unsafe or missing SQLite {label}")
    return value


def _resolve_repo_file(root: Path, relative: str, label: str) -> Path:
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise ValueError(f"{label} is missing or outside the repository")
    return path


def verify_sqlite(contract: dict[str, Any], root: Path) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    data = contract.get("data", {})
    database_record = data.get("database", {})
    try:
        database = _resolve_repo_file(root, database_record.get("path", ""), "seed database")
    except ValueError as exc:
        return [str(exc)], {"status": "failed", "records_checked": 0}
    checked = 0
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        for index, record in enumerate(data.get("records", [])):
            storage = record.get("storage", {})
            try:
                table = _safe_identifier(storage.get("table"), "table")
                key_column = _safe_identifier(storage.get("key_column"), "key column")
                value_column = _safe_identifier(storage.get("value_column"), "value column")
            except ValueError as exc:
                failures.append(f"data record {index}: {exc}")
                continue
            row = connection.execute(
                f'SELECT "{value_column}" AS value FROM "{table}" WHERE "{key_column}" = ?',
                (storage.get("key"),),
            ).fetchone()
            if row is None:
                failures.append(f"data record {index} is missing from SQLite")
                continue
            value = row["value"]
            body = value.encode("utf-8") if isinstance(value, str) else bytes(value)
            if hashlib.sha256(body).hexdigest() != record.get("value_sha256"):
                failures.append(f"data record {index} SQLite value hash differs")
            checked += 1

        for index, asset in enumerate(contract.get("design", {}).get("assets", [])):
            storage = asset.get("storage", {})
            try:
                table = _safe_identifier(storage.get("table"), "asset table")
                key_column = _safe_identifier(storage.get("key_column"), "asset key column")
            except ValueError as exc:
                failures.append(f"asset {index}: {exc}")
                continue
            row = connection.execute(
                f'SELECT source_sha256, replay_sha256 FROM "{table}" WHERE "{key_column}" = ?',
                (storage.get("key"),),
            ).fetchone()
            if row is None:
                failures.append(f"asset {index} is missing from SQLite")
                continue
            if row["source_sha256"] != asset.get("source_sha256"):
                failures.append(f"asset {index} source hash differs in SQLite")
            if row["replay_sha256"] != asset.get("replay_sha256"):
                failures.append(f"asset {index} replay hash differs in SQLite")
    except sqlite3.Error as exc:
        failures.append(f"SQLite verification failed: {exc}")
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    return failures, {
        "status": "passed" if not failures else "failed",
        "database": database.relative_to(root).as_posix(),
        "database_sha256": sha256_file(database),
        "records_checked": checked,
        "source_counts": data.get("source_counts", {}),
        "synthetic_disclosure": data.get("synthetic_disclosure"),
    }


def verify_workflows(
    contract: dict[str, Any],
    root: Path,
    base_url: str,
    timeout: float,
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    results = []
    interaction = contract.get("interaction", {})
    blocking_gaps = interaction.get("blocking_gaps", [])
    normalized_base = base_url.rstrip("/") + "/"
    runtime_database_value = interaction.get("runtime_database")
    runtime_database = None
    if isinstance(runtime_database_value, str):
        try:
            runtime_database = _resolve_repo_file(
                root, runtime_database_value, "runtime database"
            )
        except ValueError as exc:
            failures.append(str(exc))
    for workflow in interaction.get("workflows", []):
        workflow_id = workflow.get("id")
        workflow_failures = []
        step_results = []
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        browser_steps = [
            step for step in workflow.get("steps", [])
            if isinstance(step, dict) and step.get("action") in BROWSER_ACTIONS
        ]
        if browser_steps:
            browser_failures, browser_results = verify_browser_steps(
                browser_steps,
                base_url=base_url,
                timeout=timeout,
                repository_root=root,
                site=contract.get("site"),
            )
            workflow_failures.extend(browser_failures)
            step_results.extend(browser_results)
        for step_index, step in enumerate(workflow.get("steps", [])):
            action = step.get("action")
            if action in BROWSER_ACTIONS:
                continue
            step_failures = []
            if action == "http":
                local_path = step.get("local_path", "")
                parsed_path = urllib.parse.urlsplit(local_path) if isinstance(local_path, str) else None
                if (
                    parsed_path is None
                    or not local_path.startswith("/")
                    or parsed_path.scheme
                    or parsed_path.netloc
                ):
                    step_failures.append("unsafe local path")
                    step_results.append({
                        "index": step_index, "action": action,
                        "status": "failed", "failures": step_failures,
                    })
                    workflow_failures.extend(step_failures)
                    continue
                url = urllib.parse.urljoin(normalized_base, local_path.lstrip("/"))
                method = step.get("method", "GET")
                headers = {}
                payload = None
                if method == "POST" and isinstance(step.get("form"), dict):
                    payload = urllib.parse.urlencode(step["form"], doseq=True).encode("utf-8")
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                elif method == "POST" and isinstance(step.get("json"), dict):
                    payload = json.dumps(step["json"], separators=(",", ":")).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                request = urllib.request.Request(
                    url, data=payload, headers=headers, method=method
                )
                try:
                    with opener.open(request, timeout=timeout) as response:
                        body = response.read()
                        status = response.status
                except urllib.error.HTTPError as exc:
                    body = exc.read()
                    status = exc.code
                except (urllib.error.URLError, TimeoutError) as exc:
                    step_failures.append(f"request failed: {exc}")
                    body = b""
                    status = None
                body_sha256 = hashlib.sha256(body).hexdigest()
                expected_status = step.get("expected_status", 200)
                allowed_statuses = (
                    set(expected_status)
                    if isinstance(expected_status, list)
                    else {expected_status}
                )
                if status not in allowed_statuses:
                    step_failures.append(
                        f"HTTP {status}, expected {sorted(allowed_statuses)}"
                    )
                expected_sha256 = step.get("expected_body_sha256")
                if isinstance(expected_sha256, str) and body_sha256 != expected_sha256:
                    step_failures.append("response body hash differs")
                for expected_text in step.get("expected_contains", []):
                    if not isinstance(expected_text, str) or expected_text.encode("utf-8") not in body:
                        step_failures.append(f"response lacks expected text: {expected_text!r}")
                step_results.append({
                    "index": step_index,
                    "action": action,
                    "method": method,
                    "local_path": local_path,
                    "http_status": status,
                    "body_sha256": body_sha256,
                    "status": "failed" if step_failures else "passed",
                    "failures": step_failures,
                })
            elif action == "sqlite_assert":
                if runtime_database is None:
                    step_failures.append("runtime database is unavailable")
                else:
                    try:
                        table = _safe_identifier(step.get("table"), "assertion table")
                        where = step.get("where")
                        if not isinstance(where, dict) or not where:
                            raise ValueError("SQLite assertion requires a non-empty where mapping")
                        where_columns = [
                            _safe_identifier(column, "assertion where column")
                            for column in where
                        ]
                        connection = sqlite3.connect(
                            f"file:{runtime_database}?mode=ro", uri=True
                        )
                        connection.row_factory = sqlite3.Row
                        predicate = " AND ".join(
                            f'"{column}" = ?' for column in where_columns
                        )
                        expected = step.get("expected", {})
                        expected_count = step.get("expected_count")
                        if expected_count is not None:
                            row = connection.execute(
                                f'SELECT COUNT(*) AS count FROM "{table}" WHERE {predicate}',
                                tuple(where[column] for column in where_columns),
                            ).fetchone()
                            if row["count"] != expected_count:
                                step_failures.append(
                                    f"SQLite row count {row['count']}, expected {expected_count}"
                                )
                        else:
                            if not isinstance(expected, dict) or not expected:
                                raise ValueError(
                                    "SQLite assertion requires expected or expected_count"
                                )
                            expected_columns = [
                                _safe_identifier(column, "assertion expected column")
                                for column in expected
                            ]
                            projection = ", ".join(
                                f'"{column}"' for column in expected_columns
                            )
                            row = connection.execute(
                                f'SELECT {projection} FROM "{table}" WHERE {predicate}',
                                tuple(where[column] for column in where_columns),
                            ).fetchone()
                            if row is None:
                                step_failures.append("SQLite row is missing")
                            else:
                                for column in expected_columns:
                                    if row[column] != expected[column]:
                                        step_failures.append(
                                            f"SQLite {column}={row[column]!r}, expected {expected[column]!r}"
                                        )
                        connection.close()
                    except (sqlite3.Error, ValueError) as exc:
                        step_failures.append(f"SQLite assertion failed: {exc}")
                step_results.append({
                    "index": step_index,
                    "action": action,
                    "status": "failed" if step_failures else "passed",
                    "failures": step_failures,
                })
            else:
                step_failures.append(f"unsupported action: {action!r}")
                step_results.append({
                    "index": step_index,
                    "action": action,
                    "status": "failed",
                    "failures": step_failures,
                })
            workflow_failures.extend(step_failures)
        if workflow_failures:
            failures.extend(
                f"workflow {workflow_id}: {failure}" for failure in workflow_failures
            )
        results.append({
            "id": workflow_id,
            "kind": workflow.get("kind"),
            "steps": step_results,
            "status": "failed" if workflow_failures else "passed",
        })
    return failures, {
        "status": "passed" if not failures and not blocking_gaps else "failed",
        "workflows_checked": len(results),
        "results": results,
        "blocking_gaps": blocking_gaps,
        "disclosed_nonblocking_gaps": contract.get("interaction", {}).get(
            "disclosed_nonblocking_gaps", []
        ),
    }


def reset_runtime_database(
    contract: dict[str, Any],
    root: Path,
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    try:
        seed_database = _resolve_repo_file(
            root,
            contract.get("data", {}).get("database", {}).get("path", ""),
            "seed database",
        )
        runtime_database = _resolve_repo_file(
            root,
            contract.get("interaction", {}).get("runtime_database", ""),
            "runtime database",
        )
        shutil.copy2(seed_database, runtime_database)
        seed_sha256 = sha256_file(seed_database)
        runtime_sha256 = sha256_file(runtime_database)
        try:
            connection = sqlite3.connect(f"file:{runtime_database}?mode=ro", uri=True)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass
        byte_identical = seed_sha256 == runtime_sha256
        if not byte_identical:
            failures.append("runtime database does not match seed after workflow reset")
        if integrity != "ok":
            failures.append(f"runtime database integrity after reset is {integrity!r}")
        return failures, {
            "status": "passed" if not failures else "failed",
            "seed_database": seed_database.relative_to(root).as_posix(),
            "runtime_database": runtime_database.relative_to(root).as_posix(),
            "seed_sha256": seed_sha256,
            "runtime_sha256": runtime_sha256,
            "byte_identical": byte_identical,
            "sqlite_integrity": integrity,
        }
    except (OSError, sqlite3.Error, ValueError) as exc:
        failures.append(f"post-workflow runtime reset failed: {exc}")
        return failures, {
            "status": "failed",
            "error": str(exc),
        }


def verify(
    *,
    root: Path,
    site: str,
    contract_path: Path,
    base_url: str,
    timeout: float,
) -> dict[str, Any]:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    failures = quality_contract_issues(contract, root=root)
    if contract.get("site") != site:
        failures.append("mirror contract site does not match the requested site")
    failures.extend(quality_gate_failures(contract))

    fidelity_path = root / "mirror_factory" / "evidence" / site / "fidelity.json"
    try:
        fidelity = json.loads(fidelity_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fidelity = None
    fidelity_failures = fidelity_evidence_issues(fidelity, root=root)
    fidelity_failures.extend(declared_viewport_failures(contract, fidelity))
    failures.extend(f"design fidelity: {failure}" for failure in fidelity_failures)

    sqlite_failures, data_result = verify_sqlite(contract, root)
    failures.extend(sqlite_failures)
    workflow_failures, interaction_result = verify_workflows(
        contract, root, base_url, timeout
    )
    failures.extend(workflow_failures)
    reset_failures, reset_result = reset_runtime_database(contract, root)
    failures.extend(reset_failures)

    design_failures = [
        failure for failure in failures
        if failure.startswith("design") or "screenshot" in failure or "asset" in failure
    ]
    source_counts = contract.get("data", {}).get("source_counts", {})
    grade = (
        "synthetic_grounded_data"
        if source_counts.get("synthetic_grounded", 0)
        else "full_fidelity"
    ) if not failures else "rejected"
    return {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "quality_contract_version": QUALITY_CONTRACT_VERSION,
        "phase": "three_fidelity_quality",
        "site": site,
        "site_implementation": site_implementation_fingerprint(root / "sites" / site),
        "generated_at": utc_now(),
        "status": "passed" if not failures else "failed",
        "grade": grade,
        "contract": {
            "path": contract_path.relative_to(root).as_posix(),
            "sha256": sha256_file(contract_path),
        },
        "fidelity_evidence": {
            "path": fidelity_path.relative_to(root).as_posix(),
            "sha256": sha256_file(fidelity_path) if fidelity_path.is_file() else None,
        },
        "design": {
            "status": "passed" if not fidelity_failures and not design_failures else "failed",
            "source_kind": contract.get("design", {}).get("source_kind"),
            "assets_checked": len(contract.get("design", {}).get("assets", [])),
            "screenshot_crop_count": contract.get("design", {}).get(
                "screenshot_crop_count"
            ),
            "viewport": contract.get("design", {}).get("viewport"),
        },
        "data": data_result,
        "interaction": interaction_result,
        "post_workflow_reset": reset_result,
        "failures": failures,
    }


def declared_viewport_failures(
    contract: dict[str, Any], fidelity: Any
) -> list[str]:
    """Require every fidelity result to use the contract's declared viewport."""
    expected = contract.get("design", {}).get("viewport")
    if not isinstance(expected, dict) or not isinstance(fidelity, dict):
        return []
    failures: list[str] = []
    for index, result in enumerate(fidelity.get("results", [])):
        if not isinstance(result, dict):
            continue
        actual = result.get("viewport")
        actual_size = (
            actual.get("width"), actual.get("height")
        ) if isinstance(actual, dict) else (None, None)
        expected_size = (expected.get("width"), expected.get("height"))
        if actual_size != expected_size:
            route = result.get("route") or f"result {index}"
            failures.append(
                f"{route} viewport {actual_size[0]}x{actual_size[1]} does not match "
                f"declared {expected_size[0]}x{expected_size[1]}"
            )
    return failures


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--site", required=True)
    value.add_argument("--base-url", required=True)
    value.add_argument("--contract", type=Path)
    value.add_argument("--output", type=Path)
    value.add_argument("--timeout", type=float, default=10.0)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    root = repository_root()
    contract_path = args.contract or root / "sites" / args.site / "mirror_contract.json"
    output = args.output or root / "mirror_factory" / "evidence" / args.site / "quality.json"
    try:
        result = verify(
            root=root,
            site=args.site,
            contract_path=contract_path.resolve(),
            base_url=args.base_url,
            timeout=args.timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": 1,
            "tool": TOOL_VERSION,
            "quality_contract_version": QUALITY_CONTRACT_VERSION,
            "phase": "three_fidelity_quality",
            "site": args.site,
            "generated_at": utc_now(),
            "status": "failed",
            "grade": "rejected",
            "design": {"status": "failed"},
            "data": {"status": "failed"},
            "interaction": {"status": "failed"},
            "failures": [str(exc)],
        }
    atomic_json(output, result)
    print(json.dumps({
        "site": args.site,
        "status": result["status"],
        "grade": result["grade"],
        "failures": len(result["failures"]),
        "evidence": output.relative_to(root).as_posix(),
    }, sort_keys=True))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
