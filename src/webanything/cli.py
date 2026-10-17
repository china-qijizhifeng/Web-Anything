"""Command-line orchestration for the public WebAnything pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

from .contracts import ContractCompileError, load_and_compile
from .release import (
    ReleaseError,
    candidate_identity,
    freeze_candidate,
    invalidate_downstream_evidence,
    load_candidate_freeze,
    private_evidence_scan,
    release_candidate,
    remove_private_paths,
    sha256_file,
)
from .synthetic import SyntheticContractError, compile_synthetic_contract, load_synthetic_contract
from .synthetic_release import release_synthetic_candidate
from .synthetic_replay import (
    SyntheticReplayContractError,
    compile_synthetic_replay_contract,
    load_synthetic_replay_contract,
)
from .synthetic_replay_release import release_synthetic_replay_candidate
from .synthetic_replay_verify import SyntheticReplayVerificationError, verify_synthetic_replay_candidate
from .synthetic_replay_capture import SyntheticReplayCaptureError, freeze_replay_capture
from .synthetic_replay_acquire import (
    PRODUCT_PROFILES,
    ReplayAcquisitionError,
    capture_replay_url,
    import_authorized_browser_capture,
)
from .synthetic_visual import (
    AuthExpiredError,
    CaptureFailedError,
    SanitizeFailedError,
    SourceBlockedError,
    SyntheticVisualError,
    capture_visual_reference,
    seal_auth_state,
)
from .synthetic_visual_verify import SyntheticVisualVerificationError, verify_synthetic_visual
from .synthetic_verify import SyntheticVerificationError, verify_synthetic_candidate
from . import simple


PROFILES = ("task-driven", "immersive", "authenticated")
BUILD_MODES = ("mirror", "benchmark_synthetic", "benchmark_synthetic_replay")
SYNTHETIC_PROFILE = "benchmark_synthetic"
SYNTHETIC_REPLAY_PROFILE = "benchmark_synthetic_replay"


def repository_root() -> Path:
    override = os.environ.get("WEBANYTHING_ROOT")
    candidates = [
        Path(override).expanduser().resolve() if override else None,
        Path.cwd().resolve(),
        Path(__file__).resolve().parents[2],
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if (
            (candidate / ".agents/skills/build-webharbor-mirrors/SKILL.md").is_file()
            and (candidate / "pipeline-tools/interaction-verifier-v1/scripts/terminal.py").is_file()
        ):
            return candidate
    raise SystemExit("WebAnything repository root not found; run inside a clone or set WEBANYTHING_ROOT")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def require_public_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise SystemExit("URL must not contain credentials")
    return raw


def run(command: Sequence[str], *, cwd: Path | None = None) -> int:
    completed = subprocess.run(list(command), cwd=cwd, check=False)
    return int(completed.returncode)


def reconcile_build_status(run_dir: Path, run_config: dict[str, Any], return_code: int) -> dict[str, Any]:
    """Bind the public run record to the SDK runner's terminal evidence."""
    trace_path = run_dir / "run.json"
    trace: dict[str, Any] | None = None
    try:
        candidate = json.loads(trace_path.read_text(encoding="utf-8"))
        if isinstance(candidate, dict):
            trace = candidate
    except (OSError, ValueError):
        trace = None

    execution_status = trace.get("execution_status") if trace else None
    fatal_error_present = bool(trace.get("fatal_error")) if trace else False
    build_artifacts: dict[str, Any] | None = None
    try:
        candidate_artifacts = json.loads((run_dir / "build-artifacts.json").read_text(encoding="utf-8"))
        if isinstance(candidate_artifacts, dict):
            build_artifacts = candidate_artifacts
    except (OSError, ValueError):
        build_artifacts = None
    if execution_status == "timed_out":
        status = "timed_out"
    elif return_code == 0 and execution_status == "completed" and not fatal_error_present:
        status = "completed"
    else:
        status = "failed"

    updated = dict(run_config)
    updated.update({
        "status": status,
        "updated_at": utc_now(),
        "finished_at": trace.get("finished_at") if trace and trace.get("finished_at") else utc_now(),
        "runner_exit_code": return_code,
        "trace": {
            "path": str(trace_path),
            "available": trace is not None,
            "schema": trace.get("schema") if trace else None,
            "execution_status": execution_status,
            "reflection_status": trace.get("reflection_status") if trace else None,
            "fatal_error_present": fatal_error_present,
            "outer_artifact_status": trace.get("outer_artifact_status") if trace else None,
        },
        "artifacts": build_artifacts,
    })
    atomic_json(run_dir / "webanything-run.json", updated)
    return updated


def reconcile_repair_status(
    run_dir: Path,
    run_config: dict[str, Any],
    return_code: int,
    *,
    phase: str | None,
) -> dict[str, Any]:
    """Bind a continuation record to the SDK runner's latest terminal evidence."""
    trace_path = run_dir / "run.json"
    trace: dict[str, Any] | None = None
    try:
        candidate = json.loads(trace_path.read_text(encoding="utf-8"))
        if isinstance(candidate, dict):
            trace = candidate
    except (OSError, ValueError):
        trace = None

    latest_phase = trace.get("latest_phase") if trace else None
    execution_status = trace.get("latest_execution_status") if trace else None
    if not execution_status:
        execution_status = trace.get("execution_status") if trace else None
    fatal_error_present = bool(trace.get("fatal_error")) if trace else False
    build_artifacts: dict[str, Any] | None = None
    try:
        candidate_artifacts = json.loads((run_dir / "build-artifacts.json").read_text(encoding="utf-8"))
        if isinstance(candidate_artifacts, dict):
            build_artifacts = candidate_artifacts
    except (OSError, ValueError):
        build_artifacts = None
    continuations = trace.get("continuations") if trace else None
    continuation = None
    if isinstance(continuations, list):
        for item in reversed(continuations):
            if isinstance(item, dict) and (phase is None or item.get("phase") == phase):
                continuation = item
                break

    if execution_status == "timed_out":
        status = "timed_out"
    elif return_code == 0 and execution_status == "completed" and not fatal_error_present:
        status = "completed"
    else:
        status = "failed"

    finished_at = continuation.get("finished_at") if continuation else None
    updated = dict(run_config)
    updated.update({
        "status": status,
        "updated_at": utc_now(),
        "finished_at": finished_at or utc_now(),
        "runner_exit_code": return_code,
        "latest_phase": latest_phase or phase,
        "trace": {
            "path": str(trace_path),
            "available": trace is not None,
            "schema": trace.get("schema") if trace else None,
            "execution_status": execution_status,
            "latest_phase": latest_phase,
            "fatal_error_present": fatal_error_present,
            "outer_artifact_status": trace.get("outer_artifact_status") if trace else None,
        },
        "artifacts": build_artifacts,
    })
    atomic_json(run_dir / "webanything-run.json", updated)
    return updated


def _unique_repair_phase(run_dir: Path, phase: str | None) -> str | None:
    """Return a bounded retry phase when a reset reuses a terminal phase name."""
    if not phase:
        return phase
    try:
        trace = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return phase
    used = {
        item.get("phase")
        for item in (trace.get("continuations") or [])
        if isinstance(item, dict) and isinstance(item.get("phase"), str)
    }
    if phase not in used:
        return phase
    retry = 2
    while True:
        suffix = f"-retry-{retry}"
        candidate = f"{phase[:40 - len(suffix)]}{suffix}"
        if candidate not in used:
            return candidate
        retry += 1


def command_doctor(args: argparse.Namespace) -> int:
    root = repository_root()
    required_commands = ("git", "node", "npm", "rsync")
    command_checks = {name: shutil.which(name) for name in required_commands}
    file_checks = {
        "builder_skill": root / ".agents/skills/build-webharbor-mirrors/SKILL.md",
        "builder_runner": root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh",
        "synthetic_builder_skill": root / ".agents/skills/build-synthetic-benchmarks/SKILL.md",
        "synthetic_runtime": root / "runtime/webanything-benchmark-runtime.js",
        "synthetic_verifier": root / "src/webanything/synthetic_verify.py",
        "synthetic_visual_verifier": root / "src/webanything/synthetic_visual_verify.py",
        "synthetic_replay_builder_skill": root / ".agents/skills/build-synthetic-replays/SKILL.md",
        "synthetic_replay_runtime": root / "runtime/webanything_replay_runtime.py",
        "synthetic_replay_acquirer": root / "src/webanything/synthetic_replay_acquire.py",
        "replay_acquisition_schema": root / "schemas/replay-acquisition-v1.schema.json",
        "synthetic_replay_verifier": root / "src/webanything/synthetic_replay_verify.py",
        "sdk_runner": root / "tools/codex-mirror-trace/capture.mjs",
        "coupled_pipeline": root / "scripts/coupled_pipeline.py",
        "e2b_coupled_batch": root / "scripts/e2b_coupled_batch.py",
        "e2b_coupled_batch_schema": root / "schemas/e2b-coupled-batch-v1.schema.json",
        "coupled_builder": root / "tools/coupled-pipeline/run_coupled_builder.mjs",
        "coupled_verifier": root / "tools/coupled-pipeline/run_independent_verifier.mjs",
        "coupled_supervisor": root / "tools/coupled-pipeline/run_site_to_completion.sh",
        "interaction_verifier": root / "pipeline-tools/interaction-verifier-v1/scripts/terminal.py",
        "site_catalog": root / "site_catalog.json",
    }
    python_ok = sys.version_info >= (3, 11)
    core_ok = python_ok and all(command_checks.values()) and all(path.is_file() for path in file_checks.values())
    provider_configured = bool(
        os.environ.get("CODEX_MIRROR_API_KEY")
        or os.environ.get("WEBANYTHING_RESPONSES_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    payload = {
        "schema": "webanything-doctor-v1",
        "status": "passed" if core_ok else "failed",
        "root": str(root),
        "python": {
            "version": ".".join(str(item) for item in sys.version_info[:3]),
            "supported": python_ok,
        },
        "commands": command_checks,
        "files": {name: {"path": str(path), "exists": path.is_file()} for name, path in file_checks.items()},
        "provider_credentials_present": provider_configured,
        "provider_credentials_required_for": [
            "build", "repair", "coupled-pipeline", "e2b-coupled-batch"
        ],
        "e2b_credentials_present": bool(os.environ.get("E2B_API_KEY")),
        "credentials_values_recorded": False,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"WebAnything doctor: {payload['status']}")
        for name, value in command_checks.items():
            print(f"  {name}: {value or 'missing'}")
        for name, item in payload["files"].items():
            print(f"  {name}: {'ok' if item['exists'] else 'missing'}")
        print(f"  provider credentials: {'present' if provider_configured else 'not configured'}")
    return 0 if core_ok else 1


def command_build(args: argparse.Namespace) -> int:
    root = repository_root()
    url = require_public_url(args.url)
    build_mode = args.build_mode
    synthetic_contract: Path | None = None
    contract_sha256: str | None = None
    if build_mode in {"benchmark_synthetic", "benchmark_synthetic_replay"}:
        expected_profile = SYNTHETIC_REPLAY_PROFILE if build_mode == "benchmark_synthetic_replay" else SYNTHETIC_PROFILE
        if args.profile != expected_profile:
            raise SystemExit(f"{build_mode} builds require --profile {expected_profile}")
        if not args.synthetic_contract:
            raise SystemExit(f"{build_mode} builds require --synthetic-contract")
        synthetic_contract = Path(args.synthetic_contract).expanduser().resolve()
        try:
            contract = (
                load_synthetic_replay_contract(synthetic_contract)
                if build_mode == "benchmark_synthetic_replay"
                else load_synthetic_contract(synthetic_contract)
            )
        except (SyntheticContractError, SyntheticReplayContractError) as exc:
            raise SystemExit(str(exc)) from exc
        if contract["site_id"] != args.site:
            raise SystemExit("synthetic contract site_id differs from --site")
        if contract["seed_url"] != url:
            raise SystemExit("synthetic contract seed_url differs from the build URL")
        visual = contract.get("visual_reference") or {}
        if not visual.get("sha256") or (build_mode == "benchmark_synthetic" and visual.get("required") is not True):
            raise SystemExit(f"{build_mode} builds require a frozen visual reference")
        contract_sha256 = sha256_file(synthetic_contract)
        if args.supplemental_url or args.private_research_snapshot:
            raise SystemExit("synthetic builds do not accept supplemental or private research inputs")
    elif args.profile in {SYNTHETIC_PROFILE, SYNTHETIC_REPLAY_PROFILE} or args.synthetic_contract:
        raise SystemExit("mirror builds cannot use the synthetic profile or contract")
    run_dir = Path(args.run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    run_config = {
        "schema": "webanything-run-config-v1",
        "created_at": utc_now(),
        "url": url,
        "site": args.site,
        "profile": args.profile,
        "build_mode": build_mode,
        "viewport": {"width": args.viewport_width, "height": args.viewport_height},
        "budgets": {"max_pages": args.max_pages, "max_assets": args.max_assets},
        "status": "dispatched",
        "run_directory": str(run_dir),
    }
    if synthetic_contract is not None:
        run_config["synthetic_contract"] = {
            "path": str(synthetic_contract),
            "sha256": contract_sha256,
        }
    atomic_json(run_dir / "webanything-run.json", run_config)
    command = [
        str(root / "scripts/run_codex_trace.sh"),
        "capture",
        url,
        "--output",
        str(run_dir),
        "--site",
        args.site,
        "--profile",
        args.profile,
        "--build-mode",
        build_mode,
        "--max-pages",
        str(args.max_pages),
        "--max-assets",
        str(args.max_assets),
        "--timeout-minutes",
        str(args.timeout_minutes),
    ]
    if synthetic_contract is not None:
        command.extend(["--synthetic-contract", str(synthetic_contract)])
    if args.agent_config:
        command.extend(["--agent-config", str(Path(args.agent_config).expanduser().resolve())])
    if args.model:
        command.extend(["--model", args.model])
    if args.reasoning_effort:
        command.extend(["--reasoning-effort", args.reasoning_effort])
    if args.requirements_file:
        command.extend(["--requirements-file", str(Path(args.requirements_file).expanduser().resolve())])
    for value in args.supplemental_url:
        command.extend(["--supplemental-url", require_public_url(value)])
    if args.private_research_snapshot:
        command.append("--private-research-snapshot")
    if args.no_reflection:
        command.append("--no-reflection")
    if args.skip_artifact:
        command.append("--skip-artifact")
    return_code = run(command, cwd=root)
    reconcile_build_status(run_dir, run_config, return_code)
    return return_code


def command_repair(args: argparse.Namespace) -> int:
    root = repository_root()
    run_dir = Path(args.run_dir).expanduser().resolve()
    package = Path(args.repair_package).expanduser().resolve()
    if not (run_dir / "run.json").is_file():
        raise SystemExit(f"not a trace run: {run_dir}")
    try:
        payload = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"invalid repair package: {package}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("repair package must be a JSON object")
    run_config_path = run_dir / "webanything-run.json"
    try:
        run_config = json.loads(run_config_path.read_text(encoding="utf-8"))
        if not isinstance(run_config, dict):
            run_config = {}
    except (OSError, ValueError):
        run_config = {}
    run_config.update({
        "schema": "webanything-run-config-v1",
        "site": args.site,
        "profile": payload.get("to_profile", run_config.get("profile")),
        "status": "running",
        "run_directory": str(run_dir),
        "latest_phase": args.phase,
        "repair_package": str(package),
        "updated_at": utc_now(),
    })
    atomic_json(run_config_path, run_config)
    command = [
        str(root / "scripts/run_codex_trace.sh"),
        "continue",
        str(run_dir),
        "--requirements-file",
        str(package),
        "--site",
        args.site,
        "--timeout-minutes",
        str(args.timeout_minutes),
    ]
    if args.phase:
        command.extend(["--phase", args.phase])
    if args.agent_config:
        command.extend(["--agent-config", str(Path(args.agent_config).expanduser().resolve())])
    if args.model:
        command.extend(["--model", args.model])
    if args.reasoning_effort:
        command.extend(["--reasoning-effort", args.reasoning_effort])
    if args.no_reflection:
        command.append("--no-reflection")
    if getattr(args, "reset_trace", False):
        command.append("--reset-trace")
    if args.skip_artifact:
        command.append("--skip-artifact")
    return_code = run(command, cwd=root)
    reconcile_repair_status(run_dir, run_config, return_code, phase=args.phase)
    return return_code


def command_contract(args: argparse.Namespace) -> int:
    discovery = Path(args.discovery).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    try:
        contract = load_and_compile(
            discovery,
            site=args.site,
            profile=args.profile,
            viewport_width=args.viewport_width,
            viewport_height=args.viewport_height,
        )
    except ContractCompileError as exc:
        raise SystemExit(str(exc)) from exc
    atomic_json(output, contract)
    print(json.dumps({
        "status": "passed",
        "site": args.site,
        "profile": args.profile,
        "routes": len(contract["routes"]),
        "workflows": len(contract["workflows"]),
        "output": str(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_synthetic_contract(args: argparse.Namespace) -> int:
    spec = Path(args.spec).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    try:
        contract = compile_synthetic_contract(spec, output)
    except SyntheticContractError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        "status": "passed",
        "build_mode": "benchmark_synthetic",
        "site": contract["site_id"],
        "fixtures": len(contract["fixtures"]),
        "tasks": contract["tasks"]["count"],
        "output": str(output),
        "sha256": sha256_file(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_synthetic_replay_contract(args: argparse.Namespace) -> int:
    spec = Path(args.spec).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    try:
        contract = compile_synthetic_replay_contract(spec, output)
    except SyntheticReplayContractError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        "status": "passed",
        "build_mode": "benchmark_synthetic_replay",
        "site": contract["site_id"],
        "captured_files": contract["capture_manifest"]["file_count"],
        "tasks": contract["tasks"]["count"],
        "output": str(output),
        "sha256": sha256_file(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_freeze_replay_capture(args: argparse.Namespace) -> int:
    try:
        manifest = freeze_replay_capture(
            bundle_root=Path(args.bundle_root).expanduser(),
            capture_dir=Path(args.capture_dir).expanduser(),
            source_origin=args.source_origin,
            routes_path=Path(args.routes).expanduser(),
            privacy_report_path=Path(args.privacy_report).expanduser(),
            output_path=Path(args.output).expanduser(),
        )
    except SyntheticReplayCaptureError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        "status": "passed",
        "schema": manifest["schema"],
        "routes": len(manifest["routes"]),
        "files": len(manifest["files"]),
        "output": str(Path(args.output).expanduser().resolve()),
        "sha256": sha256_file(Path(args.output).expanduser().resolve()),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _capture_state_exit(code: int, label: str, exc: Exception) -> NoReturn:
    print(f"{label}: {exc}", file=sys.stderr)
    raise SystemExit(code)


def _capture_replay(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    try:
        return capture_replay_url(
            url=args.url,
            site=args.site,
            output_dir=output_dir,
            route_values=args.route,
            max_pages=args.max_pages,
            max_assets=args.max_assets,
            max_asset_bytes=args.max_asset_bytes,
            max_actions_per_route=args.max_actions_per_route,
            viewport_width=args.viewport_width,
            viewport_height=args.viewport_height,
            auth_profile_path=Path(args.auth_profile).expanduser() if args.auth_profile else None,
            key_path=Path(args.key_file).expanduser() if args.key_file else None,
            browser_path=Path(args.browser_path).expanduser() if args.browser_path else None,
            dependency_path=Path(args.dependency_path).expanduser() if args.dependency_path else None,
            private_selectors=args.private_selector,
            authenticated_selector=args.authenticated_selector,
            allow_private_network=args.allow_private_network,
            timeout_ms=args.capture_timeout_ms,
            product_profile=getattr(args, "product_profile", None),
        )
    except AuthExpiredError as exc:
        _capture_state_exit(20, "auth_expired", exc)
    except SourceBlockedError as exc:
        _capture_state_exit(21, "source_blocked", exc)
    except CaptureFailedError as exc:
        _capture_state_exit(22, "capture_failed", exc)
    except SanitizeFailedError as exc:
        _capture_state_exit(23, "sanitize_failed", exc)
    except (ReplayAcquisitionError, SyntheticReplayCaptureError, SyntheticReplayContractError, SyntheticVisualError) as exc:
        raise SystemExit(str(exc)) from exc


def command_capture_replay(args: argparse.Namespace) -> int:
    result = _capture_replay(args, Path(args.output_dir).expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_import_authorized_browser_capture(args: argparse.Namespace) -> int:
    try:
        result = import_authorized_browser_capture(
            baseline_dir=Path(args.baseline_dir).expanduser(),
            browser_export_dir=Path(args.browser_export_dir).expanduser(),
            output_dir=Path(args.output_dir).expanduser(),
            site=args.site,
            seed_url=args.url,
            product_profile=args.product_profile,
        )
    except (ReplayAcquisitionError, SyntheticReplayCaptureError, SyntheticReplayContractError, SyntheticVisualError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_replay_build(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    source_dir = run_dir / "replay-source"
    result = _capture_replay(args, source_dir)
    run_evidence = {
        **result,
        "output_dir": "replay-source",
        "contract_path": "replay-source/synthetic_replay_contract.json",
    }
    atomic_json(run_dir / "replay-acquisition.json", run_evidence)
    print(json.dumps({"phase": "capture", **result}, ensure_ascii=False, indent=2, sort_keys=True))
    build_args = argparse.Namespace(
        url=_canonical_build_url(args.url),
        site=args.site,
        run_dir=str(run_dir),
        build_mode="benchmark_synthetic_replay",
        profile=SYNTHETIC_REPLAY_PROFILE,
        synthetic_contract=result["contract_path"],
        viewport_width=args.viewport_width,
        viewport_height=args.viewport_height,
        max_pages=args.max_pages,
        max_assets=args.max_assets,
        timeout_minutes=args.timeout_minutes,
        agent_config=args.agent_config,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        requirements_file=args.requirements_file,
        supplemental_url=[],
        private_research_snapshot=False,
        no_reflection=args.no_reflection,
        skip_artifact=args.skip_artifact,
    )
    return command_build(build_args)


def _canonical_build_url(raw: str) -> str:
    parsed = urlsplit(require_public_url(raw))
    pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    return parsed._replace(path=parsed.path or "/", query=urlencode(pairs), fragment="").geturl()


def command_seal_auth_state(args: argparse.Namespace) -> int:
    try:
        result = seal_auth_state(
            storage_state_path=Path(args.storage_state).expanduser(),
            output_path=Path(args.output).expanduser(),
            key_path=Path(args.key_file).expanduser(),
            site=args.site,
            account=args.account,
            allowed_origins=args.allowed_origin,
            delete_plaintext=args.delete_plaintext,
        )
    except SyntheticVisualError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({"status": "sealed", **result, "plaintext_deleted": args.delete_plaintext}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_capture_synthetic_reference(args: argparse.Namespace) -> int:
    try:
        result = capture_visual_reference(
            spec_path=Path(args.spec).expanduser(),
            output_dir=Path(args.output_dir).expanduser(),
            auth_profile_path=Path(args.auth_profile).expanduser() if args.auth_profile else None,
            key_path=Path(args.key_file).expanduser() if args.key_file else None,
            browser_path=Path(args.browser_path).expanduser() if args.browser_path else None,
            dependency_path=Path(args.dependency_path).expanduser() if args.dependency_path else None,
            allow_private_network=args.allow_private_network,
            timeout_ms=args.timeout_ms,
        )
    except AuthExpiredError as exc:
        print(json.dumps({"status": "auth_expired", "message": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 20
    except SourceBlockedError as exc:
        print(json.dumps({"status": "source_blocked", "message": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 21
    except CaptureFailedError as exc:
        print(json.dumps({"status": "capture_failed", "message": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 22
    except SanitizeFailedError as exc:
        print(json.dumps({"status": "sanitize_failed", "message": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 23
    except SyntheticVisualError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_verify_synthetic_visual(args: argparse.Namespace) -> int:
    try:
        result = verify_synthetic_visual(
            site_path=Path(args.site_path).expanduser(),
            contract_path=Path(args.contract).expanduser(),
            reference_path=Path(args.visual_reference).expanduser(),
            freeze_path=Path(args.candidate_freeze).expanduser(),
            output_path=Path(args.output).expanduser(),
            repair_package_path=Path(args.repair_package).expanduser() if args.repair_package else None,
            base_url=args.base_url,
            browser_path=Path(args.browser_path).expanduser() if args.browser_path else None,
            dependency_path=Path(args.dependency_path).expanduser() if args.dependency_path else None,
            timeout_ms=args.timeout_ms,
        )
    except (SyntheticVisualError, SyntheticVisualVerificationError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["status"] == "passed":
        return 0
    return 2 if result.get("repairable") is True else 1


def command_verify_synthetic(args: argparse.Namespace) -> int:
    try:
        result = verify_synthetic_candidate(
            site_path=Path(args.site_path).expanduser(),
            contract_path=Path(args.contract).expanduser(),
            freeze_path=Path(args.candidate_freeze).expanduser(),
            output_path=Path(args.output).expanduser(),
            base_url=args.base_url,
            browser_path=Path(args.browser_path).expanduser() if args.browser_path else None,
            dependency_path=Path(args.dependency_path).expanduser() if args.dependency_path else None,
            timeout_ms=args.timeout_ms,
        )
    except SyntheticVerificationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


def command_verify_synthetic_replay(args: argparse.Namespace) -> int:
    try:
        result = verify_synthetic_replay_candidate(
            site_path=Path(args.site_path).expanduser(),
            contract_path=Path(args.contract).expanduser(),
            freeze_path=Path(args.candidate_freeze).expanduser(),
            output_path=Path(args.output).expanduser(),
            repair_package_path=Path(args.repair_package).expanduser() if args.repair_package else None,
            base_url=args.base_url,
            browser_path=args.browser_path,
            timeout_ms=args.timeout_ms,
        )
    except SyntheticReplayVerificationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["status"] == "passed":
        return 0
    return 2 if args.repair_package and result.get("repairable") is True else 1


def command_release_synthetic(args: argparse.Namespace) -> int:
    try:
        manifest = release_synthetic_candidate(
            site=args.site,
            site_path=Path(args.site_path).expanduser(),
            contract_path=Path(args.contract).expanduser(),
            verification_path=Path(args.verification).expanduser(),
            visual_reference_path=Path(args.visual_reference).expanduser(),
            visual_verification_path=Path(args.visual_verification).expanduser(),
            freeze_path=Path(args.candidate_freeze).expanduser(),
            output_dir=Path(args.output_dir).expanduser(),
        )
    except ReleaseError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_release_synthetic_replay(args: argparse.Namespace) -> int:
    try:
        manifest = release_synthetic_replay_candidate(
            site=args.site,
            site_path=Path(args.site_path).expanduser(),
            contract_path=Path(args.contract).expanduser(),
            verification_path=Path(args.verification).expanduser(),
            visual_reference_path=Path(args.visual_reference).expanduser(),
            visual_verification_path=Path(args.visual_verification).expanduser(),
            freeze_path=Path(args.candidate_freeze).expanduser(),
            output_dir=Path(args.output_dir).expanduser(),
        )
    except ReleaseError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    root = repository_root()
    terminal = root / "pipeline-tools/interaction-verifier-v1/scripts/terminal.py"
    command = [
        sys.executable,
        str(terminal),
        "--candidate-workspace",
        str(Path(args.candidate_workspace).expanduser().resolve()),
        "--site-path",
        str(Path(args.site_path).expanduser().resolve()),
        "--site",
        args.site,
        "--contract",
        str(Path(args.contract).expanduser().resolve()),
        "--evidence-dir",
        str(Path(args.evidence_dir).expanduser().resolve()),
        "--viewport-width",
        str(args.viewport_width),
        "--viewport-height",
        str(args.viewport_height),
        "--max-pages",
        str(args.max_pages),
        "--max-depth",
        str(args.max_depth),
        "--timeout-ms",
        str(args.timeout_ms),
    ]
    if args.base_url:
        command.extend(["--base-url", args.base_url])
    if args.launch_config:
        command.extend(["--launch-config", str(Path(args.launch_config).expanduser().resolve())])
    if args.browser_path:
        command.extend(["--browser-path", str(Path(args.browser_path).expanduser().resolve())])
    if args.dependency_path:
        command.extend(["--dependency-path", str(Path(args.dependency_path).expanduser().resolve())])
    return run(command, cwd=root)


def command_batch(args: argparse.Namespace) -> int:
    root = repository_root()
    command = [
        sys.executable,
        str(root / "scripts/batch_supervisor.py"),
        "--spec",
        str(Path(args.spec).expanduser().resolve()),
        "--root",
        str(root),
    ]
    if args.state_dir:
        command.extend(["--state-dir", str(Path(args.state_dir).expanduser().resolve())])
    if args.summary:
        command.extend(["--summary", str(Path(args.summary).expanduser().resolve())])
    if args.max_concurrency:
        command.extend(["--max-concurrency", str(args.max_concurrency)])
    if args.worker_timeout_seconds is not None:
        command.extend(["--worker-timeout-seconds", str(args.worker_timeout_seconds)])
    return run(command, cwd=root)


def command_synthetic_pipeline(args: argparse.Namespace) -> int:
    root = repository_root()
    return run([
        sys.executable,
        str(root / "scripts/synthetic_pipeline_supervisor.py"),
        "--spec", str(Path(args.spec).expanduser().resolve()),
        "--state-dir", str(Path(args.state_dir).expanduser().resolve()),
    ], cwd=root)


def command_deploy(args: argparse.Namespace) -> int:
    root = repository_root()
    manifest_path = Path(args.manifest).expanduser().resolve()
    try:
        deployment_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"invalid deployment manifest: {manifest_path}: {exc}") from exc
    deployment_sites = {str(item.get("id")) for item in deployment_manifest.get("sites", []) if isinstance(item, dict)}
    release_sites: set[str] = set()
    for value in args.release_manifest:
        path = Path(value).expanduser().resolve()
        try:
            release = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"invalid release manifest: {path}: {exc}") from exc
        if (
            release.get("release_status") != "frozen"
            or (release.get("verification") or {}).get("status") != "passed"
            or (release.get("seed_reset") or {}).get("status") != "passed"
            or (release.get("privacy_scan") or {}).get("status") != "passed"
        ):
            raise SystemExit(f"release manifest is not deployable: {path}")
        release_sites.add(str(release.get("site")))
    if deployment_sites != release_sites:
        raise SystemExit(
            "deployment sites must exactly match frozen release manifests: "
            f"deployment={sorted(deployment_sites)}, releases={sorted(release_sites)}"
        )
    command = [
        sys.executable,
        str(root / "scripts/deploy_preview_gallery_e2b.py"),
        "--manifest",
        str(manifest_path),
        "--output",
        str(Path(args.result).expanduser().resolve()),
        "--template",
        args.template,
        "--public",
    ]
    if args.env_file:
        command.extend(["--env-file", str(Path(args.env_file).expanduser().resolve())])
    if args.browser_path:
        command.extend(["--browser-path", str(Path(args.browser_path).expanduser().resolve())])
    if getattr(args, "sandbox_id", None):
        command.extend(["--sandbox-id", str(args.sandbox_id)])
    if getattr(args, "create_if_missing", False):
        command.append("--create-if-missing")
    return run(command, cwd=root)


def command_release(args: argparse.Namespace) -> int:
    try:
        manifest = release_candidate(
            site=args.site,
            profile=args.profile,
            site_path=Path(args.site_path).expanduser(),
            seed_db=Path(args.seed_db).expanduser(),
            runtime_db=Path(args.runtime_db).expanduser(),
            verification_paths=[Path(value).expanduser() for value in args.verification],
            output_dir=Path(args.output_dir).expanduser(),
            expected_seed_sha256=args.expected_seed_sha256,
            private_paths=[Path(value).expanduser() for value in args.private_path],
            private_allowed_root=Path(args.private_allowed_root).expanduser() if args.private_allowed_root else None,
        )
    except ReleaseError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_seal(args: argparse.Namespace) -> int:
    root = repository_root()
    command = ["node", str(root / "tools/codex-mirror-trace/seal.mjs"), str(Path(args.run_dir).expanduser().resolve())]
    return run(command, cwd=root)


def command_release_scan(args: argparse.Namespace) -> int:
    root = repository_root()
    target = Path(args.path).expanduser().resolve()
    return run([sys.executable, str(root / "scripts/release_scan.py"), str(target)], cwd=root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webanything", description=__doc__)
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0a1")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check local pipeline dependencies")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=command_doctor)

    build = subparsers.add_parser("build", help="build an isolated mirror or synthetic benchmark from a URL seed")
    build.add_argument("url")
    build.add_argument("--site", required=True)
    build.add_argument("--run-dir", required=True)
    build.add_argument("--build-mode", choices=BUILD_MODES, default="mirror")
    build.add_argument("--profile", choices=PROFILES + (SYNTHETIC_PROFILE, SYNTHETIC_REPLAY_PROFILE), default="task-driven")
    build.add_argument("--synthetic-contract")
    build.add_argument("--viewport-width", type=int, default=1440)
    build.add_argument("--viewport-height", type=int, default=1000)
    build.add_argument("--max-pages", type=int, default=12)
    build.add_argument("--max-assets", type=int, default=160)
    build.add_argument("--timeout-minutes", type=int, default=120)
    build.add_argument("--agent-config")
    build.add_argument("--model")
    build.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high", "xhigh"))
    build.add_argument("--requirements-file")
    build.add_argument("--supplemental-url", action="append", default=[])
    build.add_argument("--private-research-snapshot", action="store_true")
    build.add_argument("--no-reflection", action="store_true")
    build.add_argument("--skip-artifact", action="store_true")
    build.set_defaults(func=command_build)

    contract = subparsers.add_parser("contract", help="compile a discovery package into a Site Contract")
    contract.add_argument("--discovery", required=True)
    contract.add_argument("--output", required=True)
    contract.add_argument("--site", required=True)
    contract.add_argument("--profile", choices=PROFILES, default="task-driven")
    contract.add_argument("--viewport-width", type=int, default=1440)
    contract.add_argument("--viewport-height", type=int, default=1000)
    contract.set_defaults(func=command_contract)

    synthetic_contract = subparsers.add_parser(
        "synthetic-contract",
        help="compile and hash-bind a deterministic synthetic benchmark contract",
    )
    synthetic_contract.add_argument("--spec", required=True)
    synthetic_contract.add_argument("--output", required=True)
    synthetic_contract.set_defaults(func=command_synthetic_contract)

    replay_contract = subparsers.add_parser(
        "synthetic-replay-contract",
        help="compile and hash-bind a full-capture SQLite replay contract",
    )
    replay_contract.add_argument("--spec", required=True)
    replay_contract.add_argument("--output", required=True)
    replay_contract.set_defaults(func=command_synthetic_replay_contract)

    capture_replay = subparsers.add_parser(
        "capture-replay",
        help="capture a bounded URL surface and compile a frozen replay package",
    )
    capture_replay.add_argument("url")
    capture_replay.add_argument("--site", required=True)
    capture_replay.add_argument("--output-dir", required=True)
    capture_replay.add_argument("--route", action="append", default=[])
    capture_replay.add_argument("--product-profile", choices=PRODUCT_PROFILES)
    capture_replay.add_argument("--max-pages", type=int, default=12)
    capture_replay.add_argument("--max-assets", type=int, default=500)
    capture_replay.add_argument("--max-asset-bytes", type=int, default=8_000_000)
    capture_replay.add_argument("--max-actions-per-route", type=int, default=200)
    capture_replay.add_argument("--viewport-width", type=int, default=1440)
    capture_replay.add_argument("--viewport-height", type=int, default=1000)
    capture_replay.add_argument("--auth-profile")
    capture_replay.add_argument("--key-file")
    capture_replay.add_argument("--private-selector", action="append", default=[])
    capture_replay.add_argument("--authenticated-selector")
    capture_replay.add_argument("--browser-path")
    capture_replay.add_argument("--dependency-path")
    capture_replay.add_argument("--capture-timeout-ms", type=int, default=30_000)
    capture_replay.add_argument("--allow-private-network", action="store_true", help=argparse.SUPPRESS)
    capture_replay.set_defaults(func=command_capture_replay)

    browser_import = subparsers.add_parser(
        "import-authorized-browser-capture",
        help="merge a credential-free user-authorized browser export into a replay package",
    )
    browser_import.add_argument("url")
    browser_import.add_argument("--site", required=True)
    browser_import.add_argument("--baseline-dir", required=True)
    browser_import.add_argument("--browser-export-dir", required=True)
    browser_import.add_argument("--output-dir", required=True)
    browser_import.add_argument("--product-profile", choices=PRODUCT_PROFILES, required=True)
    browser_import.set_defaults(func=command_import_authorized_browser_capture)

    replay_build = subparsers.add_parser(
        "replay-build",
        help="capture a URL into a frozen replay package and dispatch the SDK Builder",
    )
    replay_build.add_argument("url")
    replay_build.add_argument("--site", required=True)
    replay_build.add_argument("--run-dir", required=True)
    replay_build.add_argument("--route", action="append", default=[])
    replay_build.add_argument("--product-profile", choices=PRODUCT_PROFILES)
    replay_build.add_argument("--max-pages", type=int, default=12)
    replay_build.add_argument("--max-assets", type=int, default=500)
    replay_build.add_argument("--max-asset-bytes", type=int, default=8_000_000)
    replay_build.add_argument("--max-actions-per-route", type=int, default=200)
    replay_build.add_argument("--viewport-width", type=int, default=1440)
    replay_build.add_argument("--viewport-height", type=int, default=1000)
    replay_build.add_argument("--auth-profile")
    replay_build.add_argument("--key-file")
    replay_build.add_argument("--private-selector", action="append", default=[])
    replay_build.add_argument("--authenticated-selector")
    replay_build.add_argument("--browser-path")
    replay_build.add_argument("--dependency-path")
    replay_build.add_argument("--capture-timeout-ms", type=int, default=30_000)
    replay_build.add_argument("--timeout-minutes", type=int, default=120)
    replay_build.add_argument("--agent-config")
    replay_build.add_argument("--model")
    replay_build.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high", "xhigh"))
    replay_build.add_argument("--requirements-file")
    replay_build.add_argument("--no-reflection", action="store_true")
    replay_build.add_argument("--skip-artifact", action="store_true")
    replay_build.add_argument("--allow-private-network", action="store_true", help=argparse.SUPPRESS)
    replay_build.set_defaults(func=command_replay_build)

    replay_capture = subparsers.add_parser(
        "freeze-replay-capture",
        help="hash and freeze an already sanitized DOM/CSS/font/media capture directory",
    )
    replay_capture.add_argument("--bundle-root", required=True)
    replay_capture.add_argument("--capture-dir", required=True)
    replay_capture.add_argument("--source-origin", required=True)
    replay_capture.add_argument("--routes", required=True)
    replay_capture.add_argument("--privacy-report", required=True)
    replay_capture.add_argument("--output", required=True)
    replay_capture.set_defaults(func=command_freeze_replay_capture)

    reconcile_replay = subparsers.add_parser(
        "reconcile-replay-boundary",
        help="restore immutable replay inputs and remove private candidate evidence",
    )
    reconcile_replay.add_argument("--run-dir", required=True)
    reconcile_replay.add_argument("--site", required=True)
    reconcile_replay.set_defaults(func=command_reconcile_replay_boundary)

    seal_auth = subparsers.add_parser(
        "seal-auth-state",
        help="encrypt an authorized Playwright storage state for an isolated capture worker",
    )
    seal_auth.add_argument("--storage-state", required=True)
    seal_auth.add_argument("--output", required=True)
    seal_auth.add_argument("--key-file", required=True)
    seal_auth.add_argument("--site", required=True)
    seal_auth.add_argument("--account", required=True)
    seal_auth.add_argument("--allowed-origin", action="append", required=True)
    seal_auth.add_argument("--delete-plaintext", action="store_true")
    seal_auth.set_defaults(func=command_seal_auth_state)

    capture_visual = subparsers.add_parser(
        "capture-synthetic-reference",
        help="capture and freeze a source-visible reference without exposing auth state to the Builder",
    )
    capture_visual.add_argument("--spec", required=True)
    capture_visual.add_argument("--output-dir", required=True)
    capture_visual.add_argument("--auth-profile")
    capture_visual.add_argument("--key-file")
    capture_visual.add_argument("--browser-path")
    capture_visual.add_argument("--dependency-path")
    capture_visual.add_argument("--timeout-ms", type=int, default=30000)
    capture_visual.add_argument("--allow-private-network", action="store_true", help=argparse.SUPPRESS)
    capture_visual.set_defaults(func=command_capture_synthetic_reference)

    repair = subparsers.add_parser("repair", help="continue the original Builder from a repair package")
    repair.add_argument("run_dir")
    repair.add_argument("--repair-package", required=True)
    repair.add_argument("--site", required=True)
    repair.add_argument("--phase")
    repair.add_argument("--timeout-minutes", type=int, default=120)
    repair.add_argument("--agent-config")
    repair.add_argument("--model")
    repair.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high", "xhigh"))
    repair.add_argument("--no-reflection", action="store_true")
    repair.add_argument("--reset-trace", action="store_true", help="archive an incomplete prior trace before repair")
    repair.add_argument("--skip-artifact", action="store_true")
    repair.set_defaults(func=command_repair)

    verify = subparsers.add_parser("verify", help="run the read-only interaction verifier")
    verify.add_argument("--candidate-workspace", required=True)
    verify.add_argument("--site-path", required=True)
    verify.add_argument("--site", required=True)
    verify.add_argument("--contract", required=True)
    verify.add_argument("--evidence-dir", required=True)
    endpoint = verify.add_mutually_exclusive_group(required=True)
    endpoint.add_argument("--base-url")
    endpoint.add_argument("--launch-config")
    verify.add_argument("--browser-path")
    verify.add_argument("--dependency-path")
    verify.add_argument("--viewport-width", type=int, default=1440)
    verify.add_argument("--viewport-height", type=int, default=1000)
    verify.add_argument("--max-pages", type=int, default=24)
    verify.add_argument("--max-depth", type=int, default=2)
    verify.add_argument("--timeout-ms", type=int, default=10000)
    verify.set_defaults(func=command_verify)

    verify_synthetic = subparsers.add_parser(
        "verify-synthetic",
        help="independently verify a frozen synthetic benchmark",
    )
    verify_synthetic.add_argument("--site-path", required=True)
    verify_synthetic.add_argument("--contract", required=True)
    verify_synthetic.add_argument("--candidate-freeze", required=True)
    verify_synthetic.add_argument("--output", required=True)
    verify_synthetic.add_argument("--base-url")
    verify_synthetic.add_argument("--browser-path")
    verify_synthetic.add_argument("--dependency-path")
    verify_synthetic.add_argument("--timeout-ms", type=int, default=10000)
    verify_synthetic.set_defaults(func=command_verify_synthetic)

    verify_replay = subparsers.add_parser(
        "verify-synthetic-replay",
        help="verify a frozen full-capture SQLite replay candidate",
    )
    verify_replay.add_argument("--site-path", required=True)
    verify_replay.add_argument("--contract", required=True)
    verify_replay.add_argument("--candidate-freeze", required=True)
    verify_replay.add_argument("--output", required=True)
    verify_replay.add_argument("--repair-package")
    verify_replay.add_argument("--base-url")
    verify_replay.add_argument("--browser-path")
    verify_replay.add_argument("--timeout-ms", type=int, default=10000)
    verify_replay.set_defaults(func=command_verify_synthetic_replay)

    verify_synthetic_visual = subparsers.add_parser(
        "verify-synthetic-visual",
        help="compare a frozen candidate with a frozen sanitized structural visual reference",
    )
    verify_synthetic_visual.add_argument("--site-path", required=True)
    verify_synthetic_visual.add_argument("--contract", required=True)
    verify_synthetic_visual.add_argument("--visual-reference", required=True)
    verify_synthetic_visual.add_argument("--candidate-freeze", required=True)
    verify_synthetic_visual.add_argument("--output", required=True)
    verify_synthetic_visual.add_argument("--repair-package")
    verify_synthetic_visual.add_argument("--base-url")
    verify_synthetic_visual.add_argument("--browser-path")
    verify_synthetic_visual.add_argument("--dependency-path")
    verify_synthetic_visual.add_argument("--timeout-ms", type=int, default=10000)
    verify_synthetic_visual.set_defaults(func=command_verify_synthetic_visual)

    batch = subparsers.add_parser("batch", help="run independent site workers concurrently")
    batch.add_argument("--spec", required=True)
    batch.add_argument("--state-dir")
    batch.add_argument("--summary")
    batch.add_argument("--max-concurrency", type=int)
    batch.add_argument("--worker-timeout-seconds", type=float)
    batch.set_defaults(func=command_batch)

    synthetic_pipeline = subparsers.add_parser(
        "synthetic-pipeline",
        help="run the checkpointed capture, Builder, verifier, bounded repair, and release state machine",
    )
    synthetic_pipeline.add_argument("--spec", required=True)
    synthetic_pipeline.add_argument("--state-dir", required=True)
    synthetic_pipeline.set_defaults(func=command_synthetic_pipeline)

    release = subparsers.add_parser("release", help="reset state and freeze an accepted release manifest")
    release.add_argument("--site", required=True)
    release.add_argument("--profile", choices=PROFILES, required=True)
    release.add_argument("--site-path", required=True)
    release.add_argument("--seed-db", required=True)
    release.add_argument("--runtime-db", required=True)
    release.add_argument("--verification", action="append", required=True)
    release.add_argument("--output-dir", required=True)
    release.add_argument("--expected-seed-sha256")
    release.add_argument("--private-path", action="append", default=[])
    release.add_argument("--private-allowed-root")
    release.set_defaults(func=command_release)

    release_synthetic = subparsers.add_parser(
        "release-synthetic",
        help="freeze a passing synthetic benchmark release",
    )
    release_synthetic.add_argument("--site", required=True)
    release_synthetic.add_argument("--site-path", required=True)
    release_synthetic.add_argument("--contract", required=True)
    release_synthetic.add_argument("--verification", required=True)
    release_synthetic.add_argument("--visual-reference", required=True)
    release_synthetic.add_argument("--visual-verification", required=True)
    release_synthetic.add_argument("--candidate-freeze", required=True)
    release_synthetic.add_argument("--output-dir", required=True)
    release_synthetic.set_defaults(func=command_release_synthetic)

    release_replay = subparsers.add_parser(
        "release-synthetic-replay",
        help="freeze a passing full-capture SQLite replay release",
    )
    release_replay.add_argument("--site", required=True)
    release_replay.add_argument("--site-path", required=True)
    release_replay.add_argument("--contract", required=True)
    release_replay.add_argument("--verification", required=True)
    release_replay.add_argument("--visual-reference", required=True)
    release_replay.add_argument("--visual-verification", required=True)
    release_replay.add_argument("--candidate-freeze", required=True)
    release_replay.add_argument("--output-dir", required=True)
    release_replay.set_defaults(func=command_release_synthetic_replay)

    deploy = subparsers.add_parser("deploy", help="deploy accepted candidates to a fresh E2B sandbox")
    deploy.add_argument("--manifest", required=True)
    deploy.add_argument("--release-manifest", action="append", required=True)
    deploy.add_argument("--result", required=True)
    deploy.add_argument("--template", required=True)
    deploy.add_argument("--env-file")
    deploy.add_argument("--browser-path")
    deploy.add_argument("--sandbox-id", help="Reuse an existing E2B sandbox.")
    deploy.add_argument(
        "--create-if-missing",
        action="store_true",
        help="Create a replacement when the requested E2B sandbox no longer exists.",
    )
    deploy.set_defaults(func=command_deploy)

    seal = subparsers.add_parser("seal", help="seal and rescan an SDK trace")
    seal.add_argument("run_dir")
    seal.set_defaults(func=command_seal)

    scan = subparsers.add_parser("release-scan", help="reject private or machine-specific release files")
    scan.add_argument("path", nargs="?", default=".")
    scan.set_defaults(func=command_release_scan)
    return parser


# Hardened release/freeze/checkpoint boundary. The original orchestration above
# remains in this module so the public CLI has one authoritative implementation.
# ``_base`` is retained as a compatibility alias for callers that patched the
# old helper module; it is this module, not a wrapper or duplicate implementation.
_base = sys.modules[__name__]
_ORIGINAL_BUILD = command_build
_ORIGINAL_REPAIR = command_repair
_ORIGINAL_DEPLOY = command_deploy
_BASE_BUILD_PARSER = build_parser


def _reconcile_replay_boundary(run_dir: Path, site: str, *, required: bool = False) -> dict[str, Any] | None:
    """Restore the SDK-visible replay bundle from its verified immutable workspace copy."""
    run_dir = run_dir.expanduser().resolve()
    frozen_root = run_dir / "workspace/.webanything/replay-input"
    frozen_contract = frozen_root / "synthetic_replay_contract.json"
    if not frozen_contract.is_file():
        if required:
            raise ReleaseError(f"immutable replay input is missing: {frozen_contract}")
        return None
    contract = load_synthetic_replay_contract(frozen_contract)
    if contract["site_id"] != site:
        raise ReleaseError("immutable replay input site_id differs from --site")
    replay_source = run_dir / "replay-source"
    if not replay_source.is_dir():
        raise ReleaseError(f"SDK-visible replay source is missing: {replay_source}")

    timestamp = utc_now().replace(":", "-")
    preserved_root = run_dir / "diagnostics/replay-source-restores" / timestamp
    restored: list[dict[str, Any]] = []
    verified_files = 0
    for pristine in sorted(path for path in frozen_root.rglob("*") if path.is_file()):
        relative = pristine.relative_to(frozen_root)
        target = replay_source / relative
        for parent in [target.parent, *target.parent.parents]:
            if parent == replay_source.parent:
                break
            if parent.is_symlink():
                raise ReleaseError(f"replay source contains a symlinked parent: {parent}")
        if target.is_symlink():
            raise ReleaseError(f"replay source artifact is a symlink: {target}")
        expected_sha256 = sha256_file(pristine)
        if target.is_file() and sha256_file(target) == expected_sha256:
            verified_files += 1
            continue
        previous_sha256 = sha256_file(target) if target.is_file() else None
        if target.exists() and not target.is_file():
            raise ReleaseError(f"replay source artifact is not a file: {target}")
        if target.is_file():
            preserved = preserved_root / relative
            preserved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, preserved)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pristine, target)
        restored.append({
            "path": relative.as_posix(),
            "previous_sha256": previous_sha256,
            "restored_sha256": expected_sha256,
        })

    external_contract = replay_source / "synthetic_replay_contract.json"
    load_synthetic_replay_contract(external_contract)
    site_path = run_dir / "workspace/sites" / site
    if not site_path.is_dir():
        raise ReleaseError(f"candidate site is missing: {site_path}")
    private_paths = private_evidence_scan(site_path)
    removed = remove_private_paths(
        [site_path / value for value in private_paths],
        allowed_root=site_path,
    )
    if private_evidence_scan(site_path):
        raise ReleaseError("private replay evidence remains after candidate cleanup")

    result = {
        "schema": "webanything-replay-boundary-reconciliation-v1",
        "created_at": utc_now(),
        "status": "passed",
        "site": site,
        "immutable_contract_sha256": sha256_file(frozen_contract),
        "verified_unchanged_files": verified_files,
        "restored_files": restored,
        "restored_file_count": len(restored),
        "private_candidate_paths_removed": removed,
    }
    audit_path = run_dir / "diagnostics/replay-boundary" / f"{timestamp}.json"
    atomic_json(audit_path, result)
    return {**result, "audit_path": str(audit_path)}


def command_reconcile_replay_boundary(args) -> int:
    try:
        result = _reconcile_replay_boundary(
            Path(args.run_dir),
            args.site,
            required=True,
        )
    except (ReleaseError, SyntheticReplayContractError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_build(args) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    freeze_path = run_dir / "candidate-freeze.json"
    if freeze_path.is_file():
        try:
            freeze = load_candidate_freeze(freeze_path)
        except ReleaseError as exc:
            raise SystemExit(str(exc)) from exc
        if freeze.get("state") == "frozen":
            raise SystemExit("run contains a frozen candidate; use a new run directory or webanything repair")
    if (run_dir / "release/release-manifest.json").is_file() or (run_dir / "release/release-gate-result.json").is_file():
        raise SystemExit("run contains release evidence; use a new run directory or webanything repair")
    return_code = _ORIGINAL_BUILD(args)
    if return_code == 0:
        try:
            _reconcile_replay_boundary(run_dir, args.site)
        except (ReleaseError, SyntheticReplayContractError) as exc:
            raise SystemExit(f"replay boundary reconciliation failed: {exc}") from exc
    return return_code


def command_repair(args) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    if getattr(args, "reset_trace", False):
        args.phase = _unique_repair_phase(run_dir, args.phase)
    freeze_path = run_dir / "candidate-freeze.json"
    if freeze_path.is_file():
        try:
            freeze = load_candidate_freeze(freeze_path)
        except ReleaseError as exc:
            raise SystemExit(str(exc)) from exc
        invalidated = invalidate_downstream_evidence(
            run_dir,
            reason=f"explicit repair transition: {args.phase or 'repair'}",
        )
        freeze_candidate(freeze_path, freeze.get("identity") or {}, state="invalidated")
        atomic_json(run_dir / "repair-transition.json", {
            "schema": "webanything-repair-transition-v1",
            "created_at": utc_now(),
            "phase": args.phase,
            "previous_candidate_identity_sha256": (freeze.get("identity") or {}).get("sha256"),
            "downstream_evidence_invalidated": invalidated,
            "status": "builder_writes_authorized",
        })
    return_code = _ORIGINAL_REPAIR(args)
    if return_code == 0:
        try:
            _reconcile_replay_boundary(run_dir, args.site)
        except (ReleaseError, SyntheticReplayContractError) as exc:
            raise SystemExit(f"replay boundary reconciliation failed: {exc}") from exc
    return return_code


def command_freeze(args) -> int:
    if not args.contract:
        raise SystemExit("at least one --contract input is required to freeze a candidate")
    mode_profile = {
        "benchmark_synthetic": SYNTHETIC_PROFILE,
        "benchmark_synthetic_replay": SYNTHETIC_REPLAY_PROFILE,
    }
    if args.build_mode in mode_profile and args.profile != mode_profile[args.build_mode]:
        raise SystemExit(f"{args.build_mode} freezes require --profile {mode_profile[args.build_mode]}")
    if args.build_mode == "mirror" and args.profile in mode_profile.values():
        raise SystemExit("mirror freezes cannot use a synthetic profile")
    try:
        contract_paths = [Path(value).expanduser().resolve() for value in args.contract]
        reference_paths: list[Path] = []
        if args.build_mode == "benchmark_synthetic":
            if len(contract_paths) != 1:
                raise SystemExit("benchmark_synthetic freeze requires exactly one synthetic contract")
            contract = load_synthetic_contract(contract_paths[0])
            visual = contract.get("visual_reference") or {}
            if visual.get("required") is not True:
                raise SystemExit("benchmark_synthetic freeze requires a frozen visual reference")
            reference_paths.append((contract_paths[0].parent / visual["path"]).resolve())
        elif args.build_mode == "benchmark_synthetic_replay":
            if len(contract_paths) != 1:
                raise SystemExit("benchmark_synthetic_replay freeze requires exactly one replay contract")
            contract = load_synthetic_replay_contract(contract_paths[0])
            reference_paths.append((contract_paths[0].parent / contract["visual_reference"]["path"]).resolve())
        identity = candidate_identity(
            Path(args.site_path).expanduser().resolve(),
            contract_paths=contract_paths,
            reference_paths=reference_paths,
            configuration={"site": args.site, "profile": args.profile, "build_mode": args.build_mode},
        )
        record = freeze_candidate(Path(args.output).expanduser().resolve(), identity)
    except ReleaseError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_verify(args) -> int:
    root = repository_root()
    terminal = root / "pipeline-tools/interaction-verifier-v1/scripts/terminal.py"
    command = [
        sys.executable, str(terminal),
        "--candidate-workspace", str(Path(args.candidate_workspace).expanduser().resolve()),
        "--site-path", str(Path(args.site_path).expanduser().resolve()),
        "--site", args.site,
        "--contract", str(Path(args.contract).expanduser().resolve()),
        "--candidate-freeze", str(Path(args.candidate_freeze).expanduser().resolve()),
        "--evidence-dir", str(Path(args.evidence_dir).expanduser().resolve()),
        "--viewport-width", str(args.viewport_width),
        "--viewport-height", str(args.viewport_height),
        "--max-pages", str(args.max_pages),
        "--max-depth", str(args.max_depth),
        "--timeout-ms", str(args.timeout_ms),
    ]
    if args.base_url:
        command.extend(["--base-url", args.base_url])
    if args.launch_config:
        command.extend(["--launch-config", str(Path(args.launch_config).expanduser().resolve())])
    if args.browser_path:
        command.extend(["--browser-path", str(Path(args.browser_path).expanduser().resolve())])
    if args.dependency_path:
        command.extend(["--dependency-path", str(Path(args.dependency_path).expanduser().resolve())])
    if args.batch_size:
        command.extend(["--batch-size", str(args.batch_size)])
    if args.checkpoint_dir:
        command.extend(["--checkpoint-dir", str(Path(args.checkpoint_dir).expanduser().resolve())])
    if args.resume:
        command.append("--resume")
    return run(command, cwd=root)


def command_release(args) -> int:
    try:
        manifest = release_candidate(
            site=args.site,
            profile=args.profile,
            site_path=Path(args.site_path).expanduser(),
            seed_db=Path(args.seed_db).expanduser(),
            runtime_db=Path(args.runtime_db).expanduser(),
            verification_paths=[Path(value).expanduser() for value in (args.verification or [])],
            output_dir=Path(args.output_dir).expanduser(),
            expected_seed_sha256=args.expected_seed_sha256,
            private_paths=[Path(value).expanduser() for value in args.private_path],
            private_allowed_root=Path(args.private_allowed_root).expanduser() if args.private_allowed_root else None,
            freeze_path=Path(args.candidate_freeze).expanduser(),
            verification_dir=Path(args.verification_dir).expanduser() if args.verification_dir else None,
        )
    except ReleaseError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_deploy(args) -> int:
    for value in args.release_manifest:
        path = Path(value).expanduser().resolve()
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"invalid release manifest: {path}: {exc}") from exc
        identity = manifest.get("candidate_identity") or {}
        binding = manifest.get("deployment_binding") or {}
        gate_reference = manifest.get("release_gate") or {}
        gate_path = path.parent / str(gate_reference.get("path") or "")
        if (
            manifest.get("schema") != "webanything-release-manifest-v2"
            or manifest.get("release_status") != "frozen"
            or binding.get("candidate_sha256") != manifest.get("candidate_sha256")
            or binding.get("candidate_identity_sha256") != identity.get("sha256")
            or not gate_path.is_file()
            or sha256_file(gate_path) != gate_reference.get("sha256")
        ):
            raise SystemExit(f"release manifest is not hash-bound and deployable: {path}")
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"invalid release gate: {gate_path}: {exc}") from exc
        if (
            gate.get("schema") != "webanything-release-gate-result-v1"
            or gate.get("status") != "passed"
            or gate.get("candidate_sha256") != binding.get("candidate_sha256")
            or gate.get("candidate_identity_sha256") != binding.get("candidate_identity_sha256")
        ):
            raise SystemExit(f"authoritative release gate is not current: {gate_path}")
        if manifest.get("build_mode") in {"benchmark_synthetic", "benchmark_synthetic_replay"} and (
            (manifest.get("visual_reference") or {}).get("status") != "passed"
            or (manifest.get("visual_reference") or {}).get("raw_material_persisted") is not False
            or (gate.get("required_gates") or {}).get("visual_fidelity", {}).get("status") != "passed"
            or not binding.get("synthetic_contract_sha256")
            or not binding.get("visual_reference_manifest_sha256")
            or not binding.get("visual_reference_sha256")
        ):
            raise SystemExit(f"synthetic release lacks a current visual fidelity binding: {path}")
        if manifest.get("build_mode") == "benchmark_synthetic_replay" and (
            not binding.get("capture_bundle_sha256")
            or not binding.get("seed_sha256")
            or (gate.get("required_gates") or {}).get("sqlite_reset", {}).get("status") != "passed"
            or (gate.get("required_gates") or {}).get("auth_workflows", {}).get("status") != "passed"
            or (gate.get("required_gates") or {}).get("proxy_prefix", {}).get("status") != "passed"
            or (gate.get("required_gates") or {}).get("concurrency_isolation", {}).get("status") != "passed"
        ):
            raise SystemExit(f"synthetic replay release lacks current state/deployment bindings: {path}")
    return _ORIGINAL_DEPLOY(args)


def command_coupled_pipeline(args: argparse.Namespace) -> int:
    root = repository_root()
    command = [
        sys.executable,
        str(root / "scripts/coupled_pipeline.py"),
        require_public_url(args.url),
        "--site",
        args.site,
        "--run-dir",
        str(Path(args.run_dir).expanduser().resolve()),
        "--model",
        args.model,
        "--reasoning-effort",
        args.reasoning_effort,
        "--timeout-minutes",
        str(args.timeout_minutes),
        "--max-cycles",
        str(args.max_cycles),
    ]
    if args.requirements_file:
        command.extend(["--requirements-file", str(Path(args.requirements_file).expanduser().resolve())])
    if args.task_query:
        command.extend(["--task-query", args.task_query])
    if args.storage_state:
        command.extend(["--storage-state", str(Path(args.storage_state).expanduser().resolve())])
    if args.env_file:
        command.extend(["--env-file", str(Path(args.env_file).expanduser().resolve())])
    if args.initialize_only:
        command.append("--initialize-only")
    return run(command, cwd=root)


def command_simple_create(args: argparse.Namespace) -> int:
    return simple.create(args, repository_root())


def command_simple_setup(args: argparse.Namespace) -> int:
    return simple.setup(args, repository_root())


def command_e2b_coupled_batch(args: argparse.Namespace) -> int:
    root = repository_root()
    command = [
        sys.executable,
        str(root / "scripts/e2b_coupled_batch.py"),
        "--spec",
        str(Path(args.spec).expanduser().resolve()),
    ]
    if args.state_dir:
        command.extend(["--state-dir", str(Path(args.state_dir).expanduser().resolve())])
    if args.env_file:
        command.extend(["--env-file", str(Path(args.env_file).expanduser().resolve())])
    if args.max_concurrency is not None:
        command.extend(["--max-concurrency", str(args.max_concurrency)])
    return run(command, cwd=root)


def build_parser() -> argparse.ArgumentParser:
    parser = _BASE_BUILD_PARSER()
    subparsers = next(
        action for action in parser._actions
        if isinstance(getattr(action, "choices", None), dict) and "build" in action.choices
    )
    choices = subparsers.choices
    configure = subparsers.add_parser("configure", help="save a private Responses API connection")
    configure.add_argument("--api-url", required=True, help="Responses API base URL, ending in /v1")
    configure.add_argument("--api-key-env", help="read the API key from this environment variable instead of prompting")
    configure.set_defaults(func=simple.configure)
    setup = subparsers.add_parser("setup", help="configure a model provider and install an agent skill")
    setup.add_argument("--agent", choices=("codex", "claude", "both"), default="codex")
    setup.add_argument("--api-url", help="Responses API base URL, ending in /v1; prompted if omitted")
    setup.add_argument("--api-key-env", help="read the API key from this environment variable instead of prompting")
    setup.add_argument("--model", help="provider model ID; prompted if omitted")
    setup.set_defaults(func=command_simple_setup)
    create = subparsers.add_parser("create", help="reconstruct a website with Full or task-focused JIT coverage")
    create.add_argument("url", help="public source website URL")
    create.add_argument("--mode", choices=("full", "jit"), required=True)
    create.add_argument("--task", help="required for JIT: the user task to reproduce")
    create.add_argument("--run-dir", help="optional output directory; default is runs/<derived-site-id>")
    create.add_argument("--model", help="override the model selected during setup")
    create.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high", "xhigh"), default="high")
    create.add_argument("--timeout-minutes", type=int, default=90)
    create.add_argument("--max-cycles", type=int, default=12)
    create.add_argument("--initialize-only", action="store_true", help="prepare the run without calling the model")
    create.set_defaults(func=command_simple_create)
    preview = subparsers.add_parser("preview", help="serve a generated candidate at a local URL")
    preview.add_argument("run_dir")
    preview.add_argument("--port", type=int, default=8000)
    preview.set_defaults(func=simple.preview)
    coupled = subparsers.add_parser(
        "coupled-pipeline",
        help="run coupled source exploration/build with independent verification",
    )
    coupled.add_argument("url")
    coupled.add_argument("--site", required=True)
    coupled.add_argument("--run-dir", required=True)
    coupled.add_argument("--requirements-file")
    coupled.add_argument("--task-query", help="user task to reconstruct through its complete interaction path")
    coupled.add_argument("--storage-state")
    coupled.add_argument("--env-file")
    coupled.add_argument("--model", default="gpt-5.6-sol")
    coupled.add_argument(
        "--reasoning-effort",
        choices=("minimal", "low", "medium", "high", "xhigh"),
        default="xhigh",
    )
    coupled.add_argument("--timeout-minutes", type=int, default=90)
    coupled.add_argument("--max-cycles", type=int, default=12)
    coupled.add_argument("--initialize-only", action="store_true")
    coupled.set_defaults(func=command_coupled_pipeline)
    e2b_coupled = subparsers.add_parser(
        "e2b-coupled-batch",
        help="run one complete source-coupled site pipeline per E2B sandbox",
    )
    e2b_coupled.add_argument("--spec", required=True)
    e2b_coupled.add_argument("--state-dir")
    e2b_coupled.add_argument("--env-file")
    e2b_coupled.add_argument("--max-concurrency", type=int)
    e2b_coupled.set_defaults(func=command_e2b_coupled_batch)
    freeze = subparsers.add_parser("freeze", help="hash and freeze a completed Builder candidate")
    freeze.add_argument("--site", required=True)
    freeze.add_argument("--profile", choices=PROFILES + (SYNTHETIC_PROFILE, SYNTHETIC_REPLAY_PROFILE), required=True)
    freeze.add_argument("--build-mode", choices=BUILD_MODES, default="mirror")
    freeze.add_argument("--site-path", required=True)
    freeze.add_argument("--contract", action="append", required=True)
    freeze.add_argument("--output", required=True)
    freeze.set_defaults(func=command_freeze)
    verify = choices["verify"]
    verify.add_argument("--candidate-freeze", required=True)
    verify.add_argument("--batch-size", type=int, default=64)
    verify.add_argument("--checkpoint-dir", required=True)
    verify.add_argument("--resume", action="store_true")
    release = choices["release"]
    release.add_argument("--candidate-freeze", required=True)
    release.add_argument("--verification-dir")
    release._option_string_actions["--verification"].required = False
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
