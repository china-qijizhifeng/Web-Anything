#!/usr/bin/env python3
"""Run the source-coupled Explorer/Builder compatibility pipeline.

This entry point preserves the high-fidelity workflow used by the early
WebHarbor production runs: one SDK context explores the live source while it
builds, followed by a separate read-only Verifier.  Runtime credentials and
browser storage are injected by path/environment and never copied into the
workspace or run manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit


SCHEMA = "webanything-coupled-run-v1"
SITE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
COVERAGE_PROFILES = ("standard", "fast")
FAST_COVERAGE_BUDGET = {
    "max_source_pages": 20,
    "max_navigation_depth": 2,
    "max_representatives_per_family": 2,
    "visual_samples_per_template": 1,
}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
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


def require_public_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("source URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise SystemExit("source URL must not contain credentials")
    return raw


def initialize_workspace(root: Path, workspace: Path) -> bool:
    """Create an isolated git workspace once; never replace an existing one."""
    if workspace.exists():
        if not (workspace / ".git").exists():
            raise SystemExit(f"existing workspace is not an isolated git checkout: {workspace}")
        return False
    workspace.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", "--local", str(root), str(workspace)],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SystemExit(f"failed to initialize coupled workspace: {completed.stderr.strip()}")
    return True


def prepare_task_request(run_root: Path, source_url: str, raw_query: str | None) -> dict[str, Any] | None:
    path = run_root / "task-request.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    if existing is not None:
        saved_query = existing.get("query")
        if (existing.get("schema_version") != 1 or not isinstance(saved_query, str)
                or existing.get("query_sha256") != hashlib.sha256(saved_query.encode("utf-8")).hexdigest()):
            raise SystemExit("task request checkpoint is invalid or was modified")
    if raw_query is None:
        if existing is not None and existing.get("source_url") != source_url:
            raise SystemExit("source URL differs from this task checkpoint; use a new --run-dir")
        return existing
    query = raw_query.strip()
    if not query or len(query) > 4000 or "\x00" in query:
        raise SystemExit("--task-query must contain 1 to 4000 characters")
    if existing is not None:
        if existing.get("source_url") != source_url or existing.get("query") != query:
            raise SystemExit("task query or source URL differs from this run checkpoint; use a new --run-dir")
        return existing
    if (run_root / "workspace").exists():
        raise SystemExit("cannot add a task query to an existing non-task checkpoint; use a new --run-dir")
    request = {
        "schema_version": 1,
        "source_url": source_url,
        "query": query,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
    }
    atomic_json(path, request)
    return request


def prepare_requirements(
    run_root: Path, source: Path | None, task_request: dict[str, Any] | None = None,
    *, auto_full: bool = False, source_url: str = "", coverage_profile: str = "standard",
) -> Path:
    target = run_root / "requirements.md"
    if task_request is not None:
        if target.is_file() and (run_root / "workspace").exists():
            if task_request["query_sha256"] not in target.read_text(encoding="utf-8"):
                raise SystemExit("task requirements differ from this run checkpoint")
            return target
        supplemental = ""
        if source is not None:
            source = source.expanduser().resolve()
            if not source.is_file():
                raise SystemExit(f"requirements file does not exist: {source}")
            supplemental = source.read_text(encoding="utf-8")
        brief = f"""# Task-grounded reconstruction

Source URL: {task_request['source_url']}
Task query: {task_request['query']}
Task query SHA-256: {task_request['query_sha256']}

Explore the public source from the supplied URL and record the complete, shortest legitimate
user workflow needed to answer the task. Include each required navigation, search/filter,
selection, expansion, state transition, and terminal outcome. Preserve the order and the
source-observed preconditions. Do not substitute a direct deep link for an interaction the
task requires, or omit intermediate controls merely because the destination can load.

Build all routes and controls on that workflow with enough surrounding context to discover
and complete it locally. Save a task-contract.json beside build-result.json with the query
hash and ordered steps. Each step needs a stable id, source route, local route, action,
target, and observable expected state. Exercise the full local workflow in a real browser;
include task_workflow in build-result.json checks. The independent Verifier will replay
every contract step and the terminal outcome before a task run can complete.
"""
        if supplemental:
            brief += "\n## Additional requirements\n\n" + supplemental + "\n"
        target.write_text(brief, encoding="utf-8")
        return target
    if source is None:
        if not target.is_file():
            if not auto_full:
                raise SystemExit("--requirements-file is required for a new coupled run")
            if coverage_profile == "fast":
                target.write_text(f"""# Fast Full public-site reconstruction

Source URL: {source_url}

Construct a shallow but coverage-closed public website. Inventory every discoverable
route family, page template, global navigation surface, and distinct interaction type
within the frozen exploration budget. Build one or two representative routes for each
family and one executed example for each interaction type. Repeated content pages,
unbounded lists, authentication-gated areas, and duplicate templates may be excluded
only with an explicit pattern, observation, and closure reason.

Complete means that the frozen type-level coverage contract is closed: every discovered
family and interaction type is represented, explicitly excluded, or reported as blocked.
It does not mean that every same-template content URL was copied. All visible controls on
included routes must work, the global shell must be complete, and the independent Verifier
must audit the family classification, representative selection, interactions, reset,
resources, and visual samples without weakening the normal candidate freeze.
""", encoding="utf-8")
            else:
                target.write_text(f"""# Full public-site reconstruction

Source URL: {source_url}

Explore the public, same-origin website from the supplied entry URL. Inventory the
discoverable route families and visible first-order controls, then build their
observable navigation, interactions, state, content, and visual structure.
Follow meaningful links and workflows until coverage closes. Exclude only
clearly unbounded or duplicate URL families with an explicit documented rule.
Do not claim full coverage when a discovered in-scope route or control remains
unbuilt or untested. Report inaccessible, authentication-gated, and unbounded
areas as limitations with source evidence; do not invent their behavior.

The independent Verifier must inspect the source scope independently, execute
the contracted local workflows, check visual fidelity and resources, prove
byte-identical reset, and report any coverage gap as a failed gate.
""", encoding="utf-8")
        return target
    source = source.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"requirements file does not exist: {source}")
    if source != target:
        shutil.copy2(source, target)
    return target


def prepare_storage_state(run_root: Path, source: Path | None) -> tuple[Path, bool]:
    if source is not None:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise SystemExit(f"browser storage state does not exist: {source}")
        return source, True
    target = run_root / ".session/browser-state.json"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"cookies":[],"origins":[]}\n', encoding="utf-8")
        target.chmod(0o600)
    return target, False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run coupled source exploration/build and independent verification from a URL."
    )
    parser.add_argument("source_url")
    parser.add_argument("--site", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--requirements-file")
    parser.add_argument("--task-query", help="user task to reconstruct through its complete interaction path")
    parser.add_argument("--mode", choices=("auto", "full", "jit"), default="auto")
    parser.add_argument("--coverage-profile", choices=COVERAGE_PROFILES, default="standard")
    parser.add_argument(
        "--storage-state",
        help="private Playwright storage-state path; injected at runtime and never copied",
    )
    parser.add_argument("--env-file", help="private provider environment file sourced by the runner")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default="xhigh")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    parser.add_argument("--max-cycles", type=int, default=12)
    parser.add_argument("--initialize-only", action="store_true")
    return parser


def command(args: argparse.Namespace) -> int:
    source_url = require_public_url(args.source_url)
    if not SITE_ID_RE.fullmatch(args.site):
        raise SystemExit("--site must use letters, digits, dot, underscore, or hyphen")
    if args.timeout_minutes < 1 or args.max_cycles < 1:
        raise SystemExit("--timeout-minutes and --max-cycles must be positive")

    root = repository_root()
    run_root = Path(args.run_dir).expanduser().resolve()
    if args.mode == "full" and args.task_query is not None:
        raise SystemExit("Full mode cannot use --task-query; use JIT mode")
    run_root.mkdir(parents=True, exist_ok=True)
    task_request = prepare_task_request(run_root, source_url, args.task_query)
    mode = ("jit" if task_request else "custom") if args.mode == "auto" else args.mode
    if mode == "jit" and task_request is None:
        raise SystemExit("JIT mode requires --task-query")
    if mode == "full" and task_request is not None:
        raise SystemExit("Full mode cannot use --task-query; use JIT mode")
    if mode != "full" and args.coverage_profile != "standard":
        raise SystemExit("--coverage-profile fast is available only in Full mode")
    existing_manifest = run_root / "coupled-run.json"
    if existing_manifest.is_file():
        saved = json.loads(existing_manifest.read_text(encoding="utf-8"))
        saved_mode = saved.get("mode") or ("jit" if saved.get("task_query_sha256") else "custom")
        saved_profile = saved.get("coverage_profile") or "standard"
        if (saved.get("site_id") != args.site or saved.get("source_url") != source_url
                or saved_mode != mode or saved_profile != args.coverage_profile):
            raise SystemExit("source URL, site ID, mode, or coverage profile differs from this run checkpoint; use a new --run-dir")
    requirements = prepare_requirements(
        run_root,
        Path(args.requirements_file) if args.requirements_file else None,
        task_request,
        auto_full=args.mode == "full", source_url=source_url,
        coverage_profile=args.coverage_profile,
    )
    storage_state, authorized_state_supplied = prepare_storage_state(
        run_root,
        Path(args.storage_state) if args.storage_state else None,
    )
    workspace = run_root / "workspace"
    initialized = initialize_workspace(root, workspace)

    manifest_path = run_root / "coupled-run.json"
    manifest = {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "site_id": args.site,
        "mode": mode,
        "coverage_profile": args.coverage_profile if mode == "full" else None,
        "coverage_budget": FAST_COVERAGE_BUDGET if mode == "full" and args.coverage_profile == "fast" else None,
        "source_url": source_url,
        "workspace": str(workspace),
        "requirements": str(requirements),
        "task_query_sha256": task_request["query_sha256"] if task_request else None,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "timeout_minutes": args.timeout_minutes,
        "max_cycles": args.max_cycles,
        "authorized_storage_state_supplied": authorized_state_supplied,
        "storage_state_persisted_in_workspace": False,
        "workspace_initialized": initialized,
        "status": "initialized",
        "updated_at": utc_now(),
    }
    atomic_json(manifest_path, manifest)
    if args.initialize_only:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    environment = dict(os.environ)
    environment.update(
        {
            "WEBANYTHING_STORAGE_STATE": str(storage_state),
            "WEBANYTHING_COUPLED_MODEL": args.model,
            "WEBANYTHING_COUPLED_REASONING_EFFORT": args.reasoning_effort,
            "WEBANYTHING_COUPLED_TIMEOUT_MINUTES": str(args.timeout_minutes),
            "WEBANYTHING_COUPLED_MAX_CYCLES": str(args.max_cycles),
            "WEBANYTHING_COVERAGE_PROFILE": args.coverage_profile,
        }
    )
    # The independent preflight imports the candidate Flask app. Keep it on the
    # same Python environment used to invoke the pipeline, including its deps.
    environment.setdefault("WEBANYTHING_PYTHON", sys.executable)
    if args.env_file:
        environment["WEBANYTHING_ENV_FILE"] = str(Path(args.env_file).expanduser().resolve())
    runner = root / "tools/coupled-pipeline/run_site_to_completion.sh"
    completed = subprocess.run(
        [str(runner), args.site, source_url, str(run_root), str(args.max_cycles)],
        cwd=root,
        env=environment,
        check=False,
    )
    try:
        status = json.loads((run_root / "auto-supervisor-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {"state": "incomplete"}
    manifest.update(
        {
            "status": status.get("state", "incomplete"),
            "supervisor_exit_code": completed.returncode,
            "updated_at": utc_now(),
        }
    )
    atomic_json(manifest_path, manifest)
    return int(completed.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    return command(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
