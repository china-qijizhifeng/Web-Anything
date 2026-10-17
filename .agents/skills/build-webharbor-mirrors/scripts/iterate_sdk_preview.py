#!/usr/bin/env python3
"""Force an SDK repair/audit loop and publish only a passing E2B preview."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from audit_clickthrough import AuditConfig, audit, load_scope_policy
from finalize_preview import current_evidence_failures, default_deployment_target
from sdk_trace_gate import evaluate_trace_run
from verify_reviewable_workflow import (
    coverage_result as reviewable_coverage_result,
    verify as verify_reviewable_workflow,
)


ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = Path(__file__).resolve().parents[1]
TRACE_WRAPPER = ROOT / "scripts" / "run_codex_trace.sh"
DEFAULT_MANIFEST = ROOT / "generated" / "demo-preview-manifest.json"
DEFAULT_SANDBOX = ""
DEFAULT_GALLERY_URL = ""


class ClosureError(RuntimeError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def acquire_run_lock(run_dir: Path, timeout_seconds: int) -> object:
    """Serialize all mutations of one trace workspace.

    Continuation traces are append-only JSONL files.  Two SDK turns writing the
    same run concurrently can produce duplicate prompts/item terminals and make
    an otherwise recoverable trace permanently unusable.  The kernel lock is
    released automatically if a worker is killed, so an unattended supervisor
    can safely start a replacement process.
    """
    if timeout_seconds < 1:
        raise ClosureError("--lock-timeout-seconds must be positive")
    lock_path = run_dir / ".auto-closure.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            owner = {
                "pid": os.getpid(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(owner, sort_keys=True) + "\n")
            handle.flush()
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise ClosureError(
                    f"timed out waiting for trace lock: {lock_path}"
                )
            time.sleep(0.25)


def release_run_lock(handle: object | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def closure_configuration(args: argparse.Namespace) -> dict:
    def file_sha256(path: Path | None) -> str | None:
        if path is None or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    value = {
        "acceptance_profile": args.acceptance_profile,
        "site": args.site,
        "source_site": args.source_site,
        "official_hosts": sorted(args.official_host),
        "min_clickthrough": args.min_clickthrough,
        "max_pages": args.max_pages,
        "scope_policy_sha256": file_sha256(args.scope_policy),
        "extra_requirements_sha256": file_sha256(args.extra_requirements),
        "minimum_sdk_rounds": args.minimum_sdk_rounds,
        "sdk_retries": args.sdk_retries,
        "publication_enabled": not args.no_publish,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), **value}


def resumed_sdk_rounds(state_path: Path, configuration: dict) -> int:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if state.get("closure_configuration", {}).get("sha256") != configuration["sha256"]:
        return 0
    try:
        return max(0, int(state.get("sdk_rounds_completed", 0)))
    except (TypeError, ValueError):
        return 0


def resumed_state(state_path: Path, configuration: dict) -> tuple[list[dict], list[dict], int, int, tuple[float, int, int, int] | None]:
    """Load only state produced by the same closure configuration.

    Older batches intentionally have no configuration fingerprint.  Their
    narrative is retained on disk, but it is not allowed to drive a new run.
    """
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return [], [], 0, 0, None
    if state.get("closure_configuration", {}).get("sha256") != configuration["sha256"]:
        return [], [], 0, 0, None
    rounds = [item for item in state.get("rounds", []) if isinstance(item, dict)]
    warnings = [item for item in state.get("trace_warnings", []) if isinstance(item, dict)]
    try:
        sdk_rounds = max(0, int(state.get("sdk_rounds_completed", 0)))
    except (TypeError, ValueError):
        sdk_rounds = 0
    try:
        stalled = max(0, int(state.get("stalled_rounds", 0)))
    except (TypeError, ValueError):
        stalled = 0
    previous = rounds[-1].get("score") if rounds else None
    if not isinstance(previous, list) or len(previous) != 4:
        previous = None
    else:
        try:
            previous = tuple(float(value) for value in previous)  # type: ignore[assignment]
        except (TypeError, ValueError):
            previous = None
    return rounds, warnings, sdk_rounds, stalled, previous


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=8)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=8)


def wait_for_site(base_url: str, site: str, process: subprocess.Popen, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ClosureError(f"site process exited with {process.returncode}")
        connection: http.client.HTTPConnection | None = None
        try:
            port = int(base_url.rsplit(":", 1)[1])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/_health")
            response = connection.getresponse()
            payload = json.loads(response.read())
            if response.status == 200 and payload.get("site") == site:
                return
            last = f"unexpected health payload: {payload!r}"
        except Exception as exc:
            last = repr(exc)
        finally:
            if connection is not None:
                connection.close()
        time.sleep(0.2)
    raise ClosureError(f"site did not become healthy: {last}")


def start_site(
    workspace: Path,
    source_site: str,
    expected_site: str,
    port: int,
    runtime_python: str,
    log_path: Path,
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    dependency_path = workspace / ".assets" / "builder-python"
    if not dependency_path.is_dir():
        dependency_path = ROOT / ".assets" / "builder-python"
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if dependency_path.is_dir():
        env["PYTHONPATH"] = str(dependency_path) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
    process = subprocess.Popen(
        [
            runtime_python,
            str(workspace / "src/webanything/runtime.py"),
            "serve",
            "--site", source_site,
            "--port", str(port),
            "--host", "127.0.0.1",
            "--sites-root", str(workspace / "sites"),
        ],
        cwd=workspace,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    try:
        wait_for_site(f"http://127.0.0.1:{port}", expected_site, process)
    except Exception:
        stop_process(process)
        raise
    return process


def sync_skill(workspace: Path) -> None:
    destination = workspace / ".agents" / "skills" / SKILL_DIR.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(SKILL_DIR, destination)


def audit_once(
    workspace: Path,
    site: str,
    source_site: str,
    base_url: str,
    official_hosts: list[str],
    min_clickthrough: float,
    max_pages: int,
    scope_policy: dict | None,
) -> tuple[dict, list[str]]:
    evidence_dir = workspace / "mirror_factory" / "evidence" / site
    coverage_path = evidence_dir / "clickthrough-coverage.json"
    coverage = audit(
        AuditConfig(
            base_url=base_url,
            max_pages=max_pages,
            timeout_ms=15000,
            min_body_chars=100,
            min_clickthrough=min_clickthrough,
            official_hosts=frozenset(host.casefold().strip().strip(".") for host in official_hosts),
            include_external=False,
            scope_policy=scope_policy,
        ),
        coverage_path,
        workspace / "sites" / source_site,
    )
    return coverage, current_evidence_failures(workspace, site, source_site)


def reviewable_audit_once(
    workspace: Path,
    site: str,
    base_url: str,
    declaration: Path,
) -> tuple[dict, list[str]]:
    output = workspace / "mirror_factory" / "evidence" / site / "reviewable-workflow.json"
    result = verify_reviewable_workflow(
        root=workspace,
        site=site,
        declaration_path=declaration,
        base_url=base_url,
        output=output,
    )
    return reviewable_coverage_result(result), list(result.get("failures") or [])


def progress_score(coverage: dict, evidence_failures: list[str]) -> tuple[float, int, int, int]:
    return (
        float(coverage["clickthrough"]["meaningful_local_percent"]),
        -len(coverage.get("unimplemented_links") or []),
        -len(coverage.get("resource_issues") or []),
        -len(evidence_failures),
    )


def acceptance_passed(coverage: dict, evidence_failures: list[str]) -> bool:
    return coverage.get("status") == "passed" and not evidence_failures


MAX_REPAIR_BRIEF_BYTES = 56_000


def _compact_items(items: list, keys: tuple[str, ...], limit: int) -> list:
    compacted = []
    for item in items[:limit]:
        if isinstance(item, str):
            compacted.append(item[:1200])
            continue
        if not isinstance(item, dict):
            compacted.append(str(item)[:1200])
            continue
        compacted.append({
            key: value[:1200] if isinstance(value, str) else value
            for key in keys
            if (value := item.get(key)) not in (None, "", [], {})
        })
    return compacted


def repair_brief(
    round_number: int,
    coverage: dict,
    evidence_failures: list[str],
    *,
    acceptance_profile: str = "full-site",
) -> str:
    unimplemented = coverage.get("unimplemented_links") or []
    candidates = coverage.get("next_candidates") or []
    resources = coverage.get("resource_issues") or []
    summary = {
        "round": round_number,
        "meaningful_local_percent": coverage["clickthrough"]["meaningful_local_percent"],
        "requested_min_percent": coverage["clickthrough"]["requested_min_percent"],
        "classification_counts": coverage.get("link_classification_counts"),
        "total_next_candidates": len(candidates),
        "total_unimplemented_links": len(unimplemented),
        "total_resource_issues": len(resources),
        "scope_policy": coverage.get("scope_policy"),
        "next_candidates": _compact_items(candidates, (), 30),
        "unimplemented_links": _compact_items(
            unimplemented,
            ("classification", "reason", "source_path", "text", "url", "href", "target"),
            30,
        ),
        "resource_issues": _compact_items(
            resources,
            ("kind", "route_key", "resource_type", "url", "failure", "error"),
            30,
        ),
        "browser_errors": _compact_items(coverage.get("browser_errors") or [], (), 20),
        "inventory_complete": coverage.get("inventory_complete"),
        "truncated_route_count": len(coverage.get("truncated_routes") or []),
        "truncated_routes": _compact_items(coverage.get("truncated_routes") or [], (), 30),
        "current_evidence_failures": evidence_failures,
    }
    if acceptance_profile == "reviewable-workflow":
        prefix = f"""Continue the forced reviewable-workflow closure loop, round {round_number}.

The deterministic browser gate produced this exact audit summary:

```json
"""
        suffix = """
```

Implement exactly one source-grounded browser workflow for human review: a real
entry route, one visible user action, a visible result, and a return/continue path.
Do not expand the whole site or spend this round on long-tail click-through.
Classify each fix as reusable or site-specific before editing. Keep authentic
HTML/CSS/assets and local-only runtime behavior. If the workflow changes SQLite
state, add a deterministic sqlite_assert and reset proof; never use screenshot
crops or invented upstream records.

Create or update sites/<site>/reviewable_workflow.json with completion_level
reviewable_workflow_mirror, a pinned mirror_contract workflow_id, explicit entry,
action, result, return, and return-result step roles, and nonempty
known_unfinished_scope. The parent runner executes this contract independently.

After repairs, regenerate current fidelity.json and quality.json with the existing
verifiers. Do not edit clickthrough-coverage.json by hand: the parent runner will
restart the no-reload server and recompute it independently after this turn. Do not
deploy or edit the shared gallery manifest; the parent runner owns fail-closed E2B
publication. Trace sealing and archive creation are not required.

Do not start app.run(...), flask run, the WebAnything runtime, or another persistent server
as a foreground SDK command. Do not run an unbounded large browser audit in the
foreground. For temporary runtime checks, use the skill's run-owned-site-check
entry point with a finite timeout; the parent already owns the authoritative audit
server and will recompute click-through after this turn. Return only after every
foreground command has a terminal result.
"""
    else:
        prefix = f"""Continue the forced full-site interaction-closure loop, round {round_number}.

The deterministic browser gate produced this exact audit summary:

```json
"""
        suffix = """
```

Fix as many highest-priority, source-evidenced gaps as possible. Classify each fix
as reusable or site-specific before editing: reusable behavior belongs in the
workspace's shared pipeline, while site-specific extraction or overrides stay in
the target site. A limitation, scope notice, departure notice, protocol handler,
empty 200 page, broken asset, or external runtime dependency earns no meaningful
local click-through credit. Preserve path and query semantics, popup navigation,
return paths, authentic HTML/CSS/assets, SQLite state, and deterministic reset.
Never invent upstream data or use screenshot crops. Respect robots, authentication,
GET-only capture, and source boundaries.

When `scope_policy.profile` is `major_surface_first`, close every required route and
core workflow before expanding any deferred family. Items classified as
`deferred_surface_exclusion`, `bounded_archive_exclusion`, or
`auth_or_account_exclusion` are disclosed phase-two work and must not consume this
round. Do not delete or hide their source links merely to improve the scoped metric.
A passing result may claim only `major_surface_mirror`, while the raw all-visible
metric and deferred count remain evidence of unfinished full-public-surface work.

After repairs, regenerate current fidelity.json and quality.json with the existing
verifiers. Do not edit clickthrough-coverage.json by hand: the parent runner will
restart the no-reload server and recompute it independently after this turn. Do not
deploy or edit the shared gallery manifest; the parent runner owns fail-closed E2B
publication. Trace sealing and archive creation are not required.

Do not start app.run(...), flask run, the WebAnything runtime, or another persistent server
as a foreground SDK command. Do not run an unbounded large browser audit in the
foreground. For temporary runtime checks, use the skill's run-owned-site-check
entry point with a finite timeout; the parent already owns the authoritative audit
server and will recompute click-through after this turn. Return only after every
foreground command has a terminal result.
"""
    list_keys = (
        "unimplemented_links",
        "resource_issues",
        "next_candidates",
        "truncated_routes",
        "browser_errors",
    )
    while True:
        rendered = prefix + json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + suffix
        if len(rendered.encode("utf-8")) <= MAX_REPAIR_BRIEF_BYTES:
            return rendered
        largest = max(list_keys, key=lambda key: len(summary[key]))
        if not summary[largest]:
            raise ClosureError("repair brief metadata exceeds the deterministic byte limit")
        summary[largest].pop()


def next_phase(run_dir: Path) -> str:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    existing = {
        item.get("phase")
        for item in run.get("continuations", [])
        if isinstance(item, dict)
    }
    number = 1
    while f"auto-closure-{number}" in existing:
        number += 1
    return f"auto-closure-{number}"


def run_sdk_round(args: argparse.Namespace, requirements: Path, phase: str) -> int:
    command = [
        str(TRACE_WRAPPER), "continue", str(args.run_dir),
        "--requirements-file", str(requirements),
        "--site", args.site,
        "--phase", phase,
        "--timeout-minutes", str(args.timeout_minutes),
        "--skip-artifact",
        "--no-reflection",
    ]
    if args.agent_config:
        command.extend(["--agent-config", str(args.agent_config)])
    if args.model:
        command.extend(["--model", args.model])
    if args.reasoning_effort:
        command.extend(["--reasoning-effort", args.reasoning_effort])
    timeout_seconds = max(60, args.timeout_minutes * 60 + 30)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=8)
        return 124


def sdk_round_outcome(
    run_dir: Path,
    site: str,
    phase: str,
    returncode: int,
) -> dict:
    if returncode == 0:
        return {
            "returncode": 0,
            "continue_closure": True,
            "classification": "completed_cleanly",
            "trace_warning": None,
            "publication_gates_relaxed": False,
        }
    gate = evaluate_trace_run(run_dir, site, phase)
    return {
        "returncode": returncode,
        "continue_closure": bool(gate["eligible"]),
        "classification": gate["classification"],
        "trace_warning": gate if gate["eligible"] else None,
        "trace_gate": gate,
        "publication_gates_relaxed": False,
    }


def sdk_failure_is_retryable(
    run_dir: Path,
    phase: str,
    returncode: int,
    site: str | None = None,
) -> bool:
    """Identify failures that can safely get a bounded fresh SDK phase.

    A continuation trace is append-only.  If the recorder writes a duplicate
    prompt/item event in the phase that just finished, retrying the same phase
    would append more data to an already-invalid lifecycle.  A new phase gives
    the next turn a clean boundary while preserving the corrupt phase as
    evidence.  This exception is deliberately narrower than the ordinary
    transport retry path: dropped events, projection mismatches, semantic
    failures, and non-command orphans remain fail-closed.
    """
    if returncode in (124, -signal.SIGTERM, -signal.SIGKILL):
        return True
    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return returncode != 0
    continuations = [
        item for item in run.get("continuations", [])
        if isinstance(item, dict) and item.get("phase") == phase
    ]
    error = str((continuations[-1] if continuations else {}).get("execution_error") or "")
    lowered = error.casefold()
    transient_markers = (
        "timed out", "timeout", "sigterm", "sigkill", "426", "429",
        "connection reset", "stream disconnected", "stream closed",
        "missing an sdk turn terminal", "lacks an sdk turn terminal",
        "one or more phases lack an sdk turn terminal event",
        "aborterror", "transport",
    )
    if any(marker in lowered for marker in transient_markers):
        return True

    # The SDK turn itself must have completed.  A failed/timed-out turn is
    # handled by the transport branch above only when its wrapper exposes a
    # bounded transient marker; never infer recoverability from lifecycle data
    # alone.
    continuation = next(
        (
            item for item in reversed(run.get("continuations", []))
            if isinstance(item, dict) and item.get("phase") == phase
        ),
        None,
    )
    if not isinstance(continuation, dict) or continuation.get("execution_status") != "completed":
        return False
    if not lowered.startswith("trace lifecycle is incomplete:"):
        return False

    try:
        lifecycle = json.loads((run_dir / "lifecycle-audit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if lifecycle.get("status") != "incomplete" or not lifecycle.get("failures"):
        return False
    if lifecycle.get("dropped_event_count"):
        return False
    projections = lifecycle.get("projections") or {}
    if any(
        not (projections.get(name) or {}).get("sequence_match")
        or not (projections.get(name) or {}).get("content_match")
        for name in ("messages", "tool_calls")
    ):
        return False
    sequence_gaps = lifecycle.get("sequence_gaps") or []
    if sequence_gaps:
        return False

    phase_rows = [
        item for item in lifecycle.get("phases", [])
        if isinstance(item, dict) and item.get("phase") == phase
    ]
    if len(phase_rows) != 1:
        return False
    phase_row = phase_rows[0]
    duplicate_items = [
        item for item in lifecycle.get("duplicate_completed_items", [])
        if isinstance(item, dict) and item.get("phase") == phase
    ]
    after_terminal_items = [
        item for item in lifecycle.get("item_events_after_terminal", [])
        if isinstance(item, dict) and item.get("phase") == phase
    ]
    terminals = phase_row.get("terminal_events") or []
    phase_terminal_ok = (
        len(terminals) == 1 and terminals[0].get("type") == "turn.completed"
    )
    # A phase with no normal terminal is only retryable through the explicit
    # transport/missing-terminal marker handled above.  Here we handle recorder
    # shape corruption after a completed turn, so require that terminal.
    if not phase_terminal_ok:
        return False

    allowed_reasons = {
        f"SDK phase {phase!r} does not have exactly one prompt",
        f"SDK phase {phase!r} has events after its terminal event",
        "trace contains duplicate item terminal events",
        "trace contains item events after an item terminal event",
    }
    target_site = site or str(continuation.get("site") or run.get("site") or "")
    if not target_site:
        # Unit fixtures and old traces may omit the site metadata.  Infer only
        # when the workspace has one unambiguous app directory; never guess in
        # a multi-site trace.
        candidates = [
            item.name for item in (run_dir / "workspace" / "sites").iterdir()
            if item.is_dir() and (item / "app.py").is_file()
        ] if (run_dir / "workspace" / "sites").is_dir() else []
        if len(candidates) == 1:
            target_site = candidates[0]
    if not target_site:
        return False
    gate = evaluate_trace_run(run_dir, target_site, phase)
    reasons = gate.get("reasons") or []
    if not reasons or any(reason not in allowed_reasons for reason in reasons):
        return False
    if (
        phase_row.get("prompt_count") == 1
        and not duplicate_items
        and not after_terminal_items
        and not phase_row.get("events_after_terminal")
    ):
        return False
    return True


def finalize_command(args: argparse.Namespace, base_url: str) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("finalize_preview.py")),
        "--site", args.site,
        "--gallery-id", args.gallery_id,
        "--source-site", args.source_site,
        "--source-workspace", str(args.workspace),
        "--base-url", base_url,
        "--port", str(args.preview_port),
        "--name", args.name,
        "--description", args.description,
        "--tier", args.tier,
        "--expected-text", args.expected_text,
        "--manifest", str(args.manifest),
        "--sandbox-id", args.sandbox_id,
        "--gallery-url", args.gallery_url,
        "--min-clickthrough", str(args.min_clickthrough),
        "--max-pages", str(args.max_pages),
        "--output", str(args.output),
        "--e2b-python", args.e2b_python,
    ]
    if args.scope_policy:
        command.extend(["--scope-policy", str(args.scope_policy)])
    command.extend(["--acceptance-profile", args.acceptance_profile])
    if args.reviewable_contract:
        command.extend(["--reviewable-contract", str(args.reviewable_contract)])
    for host in args.official_host:
        command.extend(["--official-host", host])
    if args.replace_existing:
        command.append("--replace-existing")
    if args.env_file:
        command.extend(["--e2b-env-file", str(args.env_file)])
    return command


def parse_args() -> argparse.Namespace:
    default_sandbox, default_gallery_url = default_deployment_target()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--site", required=True)
    parser.add_argument("--gallery-id", help="Gallery entry id; defaults to --site.")
    parser.add_argument("--source-site")
    parser.add_argument("--preview-port", required=True, type=int)
    parser.add_argument("--audit-port", type=int)
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--tier", default="sdk_preview")
    parser.add_argument("--expected-text", required=True)
    parser.add_argument("--official-host", action="append", default=[])
    parser.add_argument("--min-clickthrough", type=float, default=80.0)
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument(
        "--scope-policy",
        type=Path,
        help="Optional bounded editorial archive scope policy JSON.",
    )
    parser.add_argument(
        "--acceptance-profile",
        choices=("full-site", "reviewable-workflow"),
        default="full-site",
    )
    parser.add_argument("--reviewable-contract", type=Path)
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--max-stalled-rounds", type=int, default=2)
    parser.add_argument(
        "--lock-timeout-seconds",
        type=int,
        default=3600,
        help="Maximum time to wait for another worker using this trace workspace.",
    )
    parser.add_argument(
        "--sdk-retries",
        type=int,
        default=2,
        help="Transient SDK wrapper retries per repair round.",
    )
    parser.add_argument("--timeout-minutes", type=int, default=120)
    parser.add_argument("--runtime-python", default=sys.executable)
    parser.add_argument("--agent-config", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--sandbox-id", default=default_sandbox)
    parser.add_argument("--gallery-url", default=default_gallery_url)
    parser.add_argument("--output", type=Path, default=ROOT / "generated" / "e2b-preview-gallery.json")
    parser.add_argument("--e2b-python", default=os.environ.get("WEBHARBOR_E2B_PYTHON", sys.executable))
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument(
        "--extra-requirements",
        type=Path,
        help="Append these operator-supplied, site-specific repair priorities to each SDK round.",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help=(
            "Run the SDK repair and deterministic acceptance loop without changing "
            "the shared E2B gallery. A passing candidate is recorded as automation_verified."
        ),
    )
    parser.add_argument(
        "--minimum-sdk-rounds",
        type=int,
        default=0,
        help=(
            "Require this many successful SDK repair/self-audit rounds for the current "
            "configuration before automated acceptance can pass."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.run_dir = args.run_dir.resolve()
    args.workspace = args.run_dir / "workspace"
    args.source_site = args.source_site or args.site
    args.gallery_id = args.gallery_id or args.site
    if args.acceptance_profile == "reviewable-workflow" and not args.reviewable_contract:
        args.reviewable_contract = (
            args.workspace / "sites" / args.source_site / "reviewable_workflow.json"
        )
    if args.max_rounds < 0 or args.max_stalled_rounds < 1:
        raise SystemExit("--max-rounds must be non-negative and --max-stalled-rounds positive")
    if args.lock_timeout_seconds < 1 or args.sdk_retries < 0:
        raise SystemExit("--lock-timeout-seconds must be positive and --sdk-retries non-negative")
    if args.minimum_sdk_rounds < 0 or args.minimum_sdk_rounds > args.max_rounds:
        raise SystemExit("--minimum-sdk-rounds must be between zero and --max-rounds")
    if not 0 <= args.min_clickthrough <= 100:
        raise SystemExit("--min-clickthrough must be between 0 and 100")
    if not (args.run_dir / "run.json").is_file() or not (args.workspace / ".git").is_dir():
        raise SystemExit(f"not a Codex SDK trace workspace: {args.run_dir}")
    if not (args.workspace / "sites" / args.source_site / "app.py").is_file():
        raise SystemExit(f"missing target app: sites/{args.source_site}/app.py")
    if args.scope_policy:
        args.scope_policy = args.scope_policy.resolve()
    if args.extra_requirements:
        args.extra_requirements = args.extra_requirements.resolve()
        if not args.extra_requirements.is_file():
            raise SystemExit(f"extra requirements file does not exist: {args.extra_requirements}")
    scope_policy = load_scope_policy(args.scope_policy)

    audit_port = args.audit_port or available_port()
    base_url = f"http://127.0.0.1:{audit_port}"
    state_path = args.run_dir / "auto-closure.json"
    configuration = closure_configuration(args)
    lock_handle = acquire_run_lock(args.run_dir, args.lock_timeout_seconds)
    (
        rounds,
        trace_warnings,
        sdk_rounds_completed,
        stalled,
        previous_score,
    ) = resumed_state(state_path, configuration)
    process: subprocess.Popen | None = None
    try:
        start_round = len(rounds)
        for round_number in range(start_round, args.max_rounds + 1):
            sync_skill(args.workspace)
            stop_process(process)
            process = start_site(
                args.workspace,
                args.source_site,
                args.site,
                audit_port,
                args.runtime_python,
                args.run_dir / f"auto-closure-server-{round_number}.log",
            )
            if args.acceptance_profile == "reviewable-workflow":
                coverage, evidence_failures = reviewable_audit_once(
                    args.workspace,
                    args.site,
                    base_url,
                    args.reviewable_contract,
                )
            else:
                coverage, evidence_failures = audit_once(
                    args.workspace,
                    args.site,
                    args.source_site,
                    base_url,
                    args.official_host,
                    args.min_clickthrough,
                    args.max_pages,
                    scope_policy,
                )
            score = progress_score(coverage, evidence_failures)
            round_result = {
                "round": round_number,
                "coverage_status": coverage["status"],
                "meaningful_local_percent": score[0],
                "unimplemented_count": len(coverage.get("unimplemented_links") or []),
                "resource_issue_count": len(coverage.get("resource_issues") or []),
                "evidence_failures": evidence_failures,
                "score": list(score),
            }
            rounds.append(round_result)
            atomic_json(state_path, {
                "schema_version": 1,
                "status": "auditing",
                "site": args.site,
                "audit_url": base_url,
                "rounds": rounds,
                "trace_warnings": trace_warnings,
                "closure_configuration": configuration,
                "sdk_rounds_completed": sdk_rounds_completed,
                "publication_gates_relaxed": False,
            })
            if (
                acceptance_passed(coverage, evidence_failures)
                and sdk_rounds_completed >= args.minimum_sdk_rounds
            ):
                if args.no_publish:
                    atomic_json(state_path, {
                        "schema_version": 1,
                        "status": "automation_verified",
                        "site": args.site,
                        "rounds": rounds,
                        "trace_warnings": trace_warnings,
                        "closure_configuration": configuration,
                        "sdk_rounds_completed": sdk_rounds_completed,
                        "gallery_manifest_changed": False,
                        "publication_skipped": True,
                        "human_review_required": False,
                        "publication_gates_relaxed": False,
                    })
                    return 0
                completed = subprocess.run(finalize_command(args, base_url), cwd=ROOT, text=True)
                status = "published" if completed.returncode == 0 else "publish_failed"
                deployment = {}
                if status == "published":
                    try:
                        deployment = json.loads(args.output.resolve().read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        status = "publish_failed"
                gateway_url = deployment.get("gateway_url") if status == "published" else None
                atomic_json(state_path, {
                    "schema_version": 1,
                    "status": status,
                    "site": args.site,
                    "rounds": rounds,
                    "trace_warnings": trace_warnings,
                    "closure_configuration": configuration,
                    "sdk_rounds_completed": sdk_rounds_completed,
                    "publication_gates_relaxed": False,
                    "gallery_url": gateway_url,
                    "site_url": gateway_url.rstrip("/") + f"/p/{args.preview_port}/" if gateway_url else None,
                })
                return 0 if status == "published" else 2
            if round_number == args.max_rounds:
                break
            if previous_score is not None and score <= previous_score:
                stalled += 1
            else:
                stalled = 0
            if stalled >= args.max_stalled_rounds:
                break
            previous_score = score
            stop_process(process)
            process = None
            requirements = args.run_dir / f"auto-closure-round-{round_number + 1}.md"
            requirements.write_text(
                repair_brief(
                    round_number + 1,
                    coverage,
                    evidence_failures,
                    acceptance_profile=args.acceptance_profile,
                ),
                encoding="utf-8",
            )
            if args.extra_requirements:
                with requirements.open("a", encoding="utf-8") as stream:
                    stream.write("\n\n## Additional operator priorities\n\n")
                    stream.write(args.extra_requirements.read_text(encoding="utf-8"))
                    stream.write("\n")
            phase = next_phase(args.run_dir)
            round_result["sdk_phase"] = phase
            sdk_returncode = run_sdk_round(args, requirements, phase)
            outcome = sdk_round_outcome(args.run_dir, args.source_site, phase, sdk_returncode)
            retry_count = 0
            while (
                not outcome["continue_closure"]
                and retry_count < args.sdk_retries
                and sdk_failure_is_retryable(
                    args.run_dir,
                    phase,
                    sdk_returncode,
                    args.source_site,
                )
            ):
                retry_count += 1
                retry_phase = f"{phase}-retry-{retry_count}"
                round_result.setdefault("sdk_retries", []).append({
                    "phase": retry_phase,
                    "attempt": retry_count,
                })
                sdk_returncode = run_sdk_round(args, requirements, retry_phase)
                outcome = sdk_round_outcome(
                    args.run_dir, args.source_site, retry_phase, sdk_returncode
                )
                phase = retry_phase
            round_result["sdk_retry_count"] = retry_count
            round_result["sdk_returncode"] = sdk_returncode
            round_result["sdk_outcome"] = outcome
            if outcome["trace_warning"]:
                trace_warnings.append(outcome["trace_warning"])
            if outcome["continue_closure"]:
                sdk_rounds_completed += 1
            atomic_json(state_path, {
                "schema_version": 1,
                "status": (
                    "repairing_with_trace_warning"
                    if outcome["trace_warning"]
                    else "repairing" if outcome["continue_closure"]
                    else "failed_sdk_round"
                ),
                "site": args.site,
                "audit_url": base_url,
                "rounds": rounds,
                "trace_warnings": trace_warnings,
                "closure_configuration": configuration,
                "sdk_rounds_completed": sdk_rounds_completed,
                "stalled_rounds": stalled,
                "gallery_manifest_changed": False,
                "publication_gates_relaxed": False,
            })
            if not outcome["continue_closure"]:
                return 2
        atomic_json(state_path, {
            "schema_version": 1,
            "status": "failed_no_progress" if stalled >= args.max_stalled_rounds else "failed_round_limit",
            "site": args.site,
            "rounds": rounds,
            "trace_warnings": trace_warnings,
            "closure_configuration": configuration,
            "sdk_rounds_completed": sdk_rounds_completed,
            "stalled_rounds": stalled,
            "gallery_manifest_changed": False,
            "publication_gates_relaxed": False,
        })
        return 2
    finally:
        stop_process(process)
        release_run_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
