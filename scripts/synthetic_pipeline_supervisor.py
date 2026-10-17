#!/usr/bin/env python3
"""Checkpointed three-pool supervisor for synthetic visual production sites."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_STAGES = ("capture", "build", "freeze", "functional_verify", "visual_verify", "repair", "refreeze", "release")
STAGES = (*REQUIRED_STAGES, "deploy")
POOLS = {
    "capture": "capture",
    "build": "builder",
    "repair": "builder",
    "freeze": "verifier",
    "refreeze": "verifier",
    "functional_verify": "verifier",
    "visual_verify": "verifier",
    "release": "verifier",
    "deploy": "verifier",
}
TERMINAL_STATES = {
    "released", "deployed", "auth_expired", "source_blocked", "capture_failed", "sanitize_failed",
    "builder_timeout", "verifier_timeout", "visual_review_required", "verification_review_required", "failed",
}
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


class SyntheticPipelineError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _path(value: str | None, root: Path) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass
class SiteRuntime:
    site: str
    spec: dict[str, Any]
    checkpoint: dict[str, Any]
    process: subprocess.Popen[bytes] | None = None
    pool: str | None = None


def load_spec(path: Path) -> tuple[Path, dict[str, Any]]:
    payload = read_json(path.resolve())
    if payload is None or payload.get("schema") != "webanything-synthetic-pipeline-spec-v1":
        raise SyntheticPipelineError("unsupported synthetic pipeline spec")
    root = _path(payload.get("root"), path.resolve().parent) or path.resolve().parent
    if not root.is_dir():
        raise SyntheticPipelineError(f"pipeline root does not exist: {root}")
    pools = payload.get("pool_limits") or {}
    if set(pools) != {"capture", "builder", "verifier"} or any(not isinstance(value, int) or value < 1 for value in pools.values()):
        raise SyntheticPipelineError("pool_limits must define positive capture, builder, and verifier limits")
    sites = payload.get("sites")
    if not isinstance(sites, list) or not sites:
        raise SyntheticPipelineError("pipeline sites must be nonempty")
    seen: set[str] = set()
    for index, site in enumerate(sites):
        if not isinstance(site, dict) or not SITE_RE.fullmatch(str(site.get("id") or "")):
            raise SyntheticPipelineError(f"sites[{index}] has an invalid id")
        if site["id"] in seen:
            raise SyntheticPipelineError(f"duplicate pipeline site: {site['id']}")
        seen.add(site["id"])
        if site.get("build_mode", "benchmark_synthetic") not in {"benchmark_synthetic", "benchmark_synthetic_replay"}:
            raise SyntheticPipelineError(f"site {site['id']} has an unsupported synthetic build_mode")
        commands = site.get("commands") or {}
        if not set(REQUIRED_STAGES).issubset(commands) or not set(commands).issubset(STAGES):
            raise SyntheticPipelineError(f"site {site['id']} must define every required stage and only supported optional stages")
        for stage, command in commands.items():
            if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
                raise SyntheticPipelineError(f"site {site['id']} command {stage} must be a nonempty string array")
        if not isinstance(site.get("max_visual_repairs", 3), int) or not 1 <= site.get("max_visual_repairs", 3) <= 10:
            raise SyntheticPipelineError(f"site {site['id']} max_visual_repairs must be between 1 and 10")
        if not isinstance(site.get("max_functional_repairs", 3), int) or not 1 <= site.get("max_functional_repairs", 3) <= 10:
            raise SyntheticPipelineError(f"site {site['id']} max_functional_repairs must be between 1 and 10")
        if not isinstance(site.get("max_stage_attempts", 2), int) or not 1 <= site.get("max_stage_attempts", 2) <= 10:
            raise SyntheticPipelineError(f"site {site['id']} max_stage_attempts must be between 1 and 10")
        timeouts = site.get("timeouts_seconds") or {}
        if not isinstance(timeouts, dict) or any(
            stage not in STAGES or not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
            for stage, value in timeouts.items()
        ):
            raise SyntheticPipelineError(f"site {site['id']} timeouts_seconds is invalid")
    return root, payload


def initial_checkpoint(site: dict[str, Any], spec_sha256: str, root: Path) -> dict[str, Any]:
    auth_profile = _path((site.get("artifacts") or {}).get("auth_profile"), root)
    needs_auth = bool(site.get("auth_required")) and (auth_profile is None or not auth_profile.is_file())
    state = "auth_required" if needs_auth else "capture"
    return {
        "schema": "webanything-synthetic-pipeline-checkpoint-v1",
        "site": site["id"],
        "build_mode": site.get("build_mode", "benchmark_synthetic"),
        "spec_sha256": spec_sha256,
        "state": state,
        "terminal": False,
        "repair_round": 0,
        "repair_kind": None,
        "repair_counts": {"functional": 0, "visual": 0},
        "stage_attempts": {},
        "active_stage": None,
        "active_token": None,
        "updated_at": utc_now(),
        "history": [{"state": state, "at": utc_now()}],
    }


def _record_state(checkpoint: dict[str, Any], state: str, **details: Any) -> None:
    checkpoint["state"] = state
    checkpoint["terminal"] = state in TERMINAL_STATES
    checkpoint["updated_at"] = utc_now()
    checkpoint.setdefault("history", []).append({"state": state, "at": checkpoint["updated_at"], **details})


def _timeout_state(stage: str) -> str:
    return "builder_timeout" if POOLS[stage] == "builder" else "verifier_timeout"


def _attempt_key(checkpoint: dict[str, Any], stage: str) -> str:
    if stage in {"functional_verify", "visual_verify", "repair", "refreeze"}:
        kind = checkpoint.get("repair_kind") or "initial"
        return f"{stage}:{kind}-repair-{int(checkpoint.get('repair_round', 0))}"
    return stage


def _repair_counts(checkpoint: dict[str, Any]) -> dict[str, int]:
    counts = checkpoint.get("repair_counts")
    if not isinstance(counts, dict) or any(not isinstance(counts.get(kind), int) for kind in ("functional", "visual")):
        # Old v1 checkpoints used repair_round as a visual-only budget. Preserve
        # that conservative interpretation when resuming them.
        counts = {"functional": 0, "visual": int(checkpoint.get("repair_round", 0))}
        checkpoint["repair_counts"] = counts
    return counts


def _verified_deployment_result(command: list[str], *, root: Path, site: str) -> tuple[Path, dict[str, Any]] | None:
    try:
        result_index = command.index("--result") + 1
        result_value = command[result_index]
    except (ValueError, IndexError):
        return None
    result_path = _path(result_value, root)
    if result_path is None:
        return None
    result = read_json(result_path)
    sites = result.get("sites") if result is not None else None
    if (
        result is None
        or result.get("external_error") is not None
        or result.get("verification_failures") != []
        or not isinstance(result.get("sandbox_id"), str)
        or not str(result.get("gateway_url") or "").startswith("https://")
        or not isinstance(sites, list)
        or not any(isinstance(item, dict) and item.get("id") == site for item in sites)
        or any(not str(item.get("url") or "").startswith("https://") for item in sites if isinstance(item, dict))
    ):
        return None
    return result_path, result


def render_stage_command(command: list[str], *, site: str, stage: str, repair_round: int) -> list[str]:
    """Expand the bounded supervisor placeholders without invoking a shell."""
    replacements = {
        "{site}": site,
        "{stage}": stage,
        "{repair_round}": str(repair_round),
        "{failed_visual_round}": str(max(repair_round - 1, 0)),
    }
    rendered: list[str] = []
    for value in command:
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        rendered.append(value)
    return rendered


def apply_stage_result(runtime: SiteRuntime, result: dict[str, Any]) -> None:
    checkpoint = runtime.checkpoint
    stage = result.get("stage")
    if stage != checkpoint.get("active_stage") or result.get("token") != checkpoint.get("active_token"):
        raise SyntheticPipelineError(f"stale stage result for {runtime.site}")
    returncode = int(result.get("returncode", 1))
    timed_out = result.get("timed_out") is True
    checkpoint["active_stage"] = None
    checkpoint["active_token"] = None
    checkpoint["last_result"] = result
    max_stage_attempts = int(runtime.spec.get("max_stage_attempts", 2))
    attempt_key = _attempt_key(checkpoint, stage)
    attempts = int((checkpoint.get("stage_attempts") or {}).get(attempt_key, 0))
    if timed_out:
        if attempts < max_stage_attempts:
            _record_state(checkpoint, stage, outcome="timeout_restart", attempt=attempts)
        else:
            _record_state(checkpoint, _timeout_state(stage), outcome="timeout_exhausted", attempt=attempts)
        return
    if stage == "capture" and returncode in {20, 21, 22, 23}:
        if returncode == 20:
            checkpoint["expired_auth_profile_sha256"] = checkpoint.get("active_auth_profile_sha256")
        _record_state(checkpoint, {20: "auth_expired", 21: "source_blocked", 22: "capture_failed", 23: "sanitize_failed"}[returncode])
        return
    if stage in {"functional_verify", "visual_verify"} and returncode == 2:
        kind = "visual" if stage == "visual_verify" else "functional"
        counts = _repair_counts(checkpoint)
        maximum_key = "max_visual_repairs" if kind == "visual" else "max_functional_repairs"
        maximum = int(runtime.spec.get(maximum_key, 3))
        if counts[kind] >= maximum:
            review_state = "visual_review_required" if kind == "visual" else "verification_review_required"
            _record_state(checkpoint, review_state, repair_kind=kind, repair_count=counts[kind])
        else:
            counts[kind] += 1
            checkpoint["repair_round"] = int(checkpoint.get("repair_round", 0)) + 1
            checkpoint["repair_kind"] = kind
            _record_state(
                checkpoint,
                "repair",
                repair_round=checkpoint["repair_round"],
                repair_kind=kind,
                repair_count=counts[kind],
            )
        return
    if returncode != 0:
        if attempts < max_stage_attempts:
            _record_state(
                checkpoint,
                stage,
                outcome="error_restart",
                attempt=attempts,
                returncode=returncode,
            )
        else:
            _record_state(
                checkpoint,
                "failed",
                stage=stage,
                outcome="error_exhausted",
                attempt=attempts,
                returncode=returncode,
            )
        return
    transitions = {
        "capture": "sanitize",
        "build": "freeze",
        "freeze": "functional_verify",
        "functional_verify": "visual_verify",
        "visual_verify": "release",
        "repair": "refreeze",
        "refreeze": "functional_verify",
        "release": "deploy" if "deploy" in runtime.spec["commands"] else "released",
        "deploy": "deployed",
    }
    if stage == "capture":
        _record_state(checkpoint, "sanitize", outcome="worker_sanitized_in_memory")
        _record_state(checkpoint, "reference_frozen", outcome="sanitized_reference_hash_bound")
        _record_state(checkpoint, "build")
    else:
        _record_state(checkpoint, transitions[stage])


def _worker(task_path: Path) -> int:
    task = read_json(task_path.resolve())
    if task is None:
        raise SyntheticPipelineError("stage worker task is invalid")
    lock_path = Path(task["lock_path"])
    result_path = Path(task["result_path"])
    stdout_path = Path(task["stdout_path"])
    stderr_path = Path(task["stderr_path"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(stdout_path.parent, 0o700)
    with lock_path.open("a+", encoding="ascii") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if result_path.is_file():
            return 0
        environment = os.environ.copy()
        environment.update({
            "WEBANYTHING_SYNTHETIC_SITE": task["site"],
            "WEBANYTHING_SYNTHETIC_STAGE": task["stage"],
            "WEBANYTHING_SYNTHETIC_REPAIR_ROUND": str(task["repair_round"]),
        })
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            process = subprocess.Popen(
                task["command"],
                cwd=task["cwd"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            timed_out = False
            try:
                returncode = process.wait(timeout=task["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=8)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                returncode = process.returncode if process.returncode is not None else -signal.SIGKILL
        atomic_json(result_path, {
            "schema": "webanything-synthetic-stage-result-v1",
            "site": task["site"],
            "stage": task["stage"],
            "token": task["token"],
            "returncode": int(returncode),
            "timed_out": timed_out,
            "finished_at": utc_now(),
            "logs": {
                "stdout": {"path": str(stdout_path), "bytes": stdout_path.stat().st_size},
                "stderr": {"path": str(stderr_path), "bytes": stderr_path.stat().st_size},
            },
        })
    return 0


class Supervisor:
    def __init__(self, *, root: Path, spec: dict[str, Any], state_dir: Path) -> None:
        self.root = root
        self.spec = spec
        self.state_dir = state_dir.resolve()
        self.spec_sha256 = fingerprint(spec)
        self.checkpoint_dir = self.state_dir / "checkpoints"
        self.task_dir = self.state_dir / "stage-tasks"
        self.result_dir = self.state_dir / "stage-results"
        self.log_dir = self.state_dir / "stage-logs"
        self.lock_dir = self.state_dir / "leases"
        self.summary_path = self.state_dir / "summary.json"
        self.global_lock_path = self.state_dir / "supervisor.lock"
        self.global_lock: Any = None
        self.runtimes: list[SiteRuntime] = []
        for site in spec["sites"]:
            checkpoint_path = self.checkpoint_dir / f"{site['id']}.json"
            checkpoint = read_json(checkpoint_path)
            if checkpoint is None:
                if checkpoint_path.exists():
                    raise SyntheticPipelineError(f"checkpoint is corrupt for {site['id']}: {checkpoint_path}")
                checkpoint = initial_checkpoint(site, self.spec_sha256, root)
            elif checkpoint.get("spec_sha256") != self.spec_sha256:
                raise SyntheticPipelineError(f"checkpoint spec hash differs for {site['id']}")
            self.runtimes.append(SiteRuntime(site=site["id"], spec=site, checkpoint=checkpoint))

    def _checkpoint_path(self, runtime: SiteRuntime) -> Path:
        return self.checkpoint_dir / f"{runtime.site}.json"

    def _write_checkpoint(self, runtime: SiteRuntime) -> None:
        atomic_json(self._checkpoint_path(runtime), runtime.checkpoint)

    def _acquire(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.global_lock = self.global_lock_path.open("a+", encoding="ascii")
        try:
            fcntl.flock(self.global_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyntheticPipelineError("another synthetic pipeline supervisor owns this state directory") from exc

    def _release(self) -> None:
        if self.global_lock is not None:
            fcntl.flock(self.global_lock.fileno(), fcntl.LOCK_UN)
            self.global_lock.close()
            self.global_lock = None

    def _result_path(self, runtime: SiteRuntime) -> Path | None:
        token = runtime.checkpoint.get("active_token")
        return self.result_dir / f"{token}.json" if token else None

    def _reconcile(self, runtime: SiteRuntime) -> None:
        if runtime.process is not None and runtime.process.poll() is None:
            return
        if runtime.process is not None:
            runtime.process.wait()
            runtime.process = None
        result_path = self._result_path(runtime)
        if result_path is None:
            return
        result = read_json(result_path)
        if result_path.exists() and result is None:
            raise SyntheticPipelineError(f"stage result is corrupt for {runtime.site}: {result_path}")
        if result is not None:
            apply_stage_result(runtime, result)
            runtime.pool = None
            self._write_checkpoint(runtime)

    def _auth_resume(self, runtime: SiteRuntime) -> None:
        if runtime.checkpoint["state"] not in {"auth_required", "auth_expired"}:
            return
        profile = _path((runtime.spec.get("artifacts") or {}).get("auth_profile"), self.root)
        current_sha256 = hashlib.sha256(profile.read_bytes()).hexdigest() if profile is not None and profile.is_file() else None
        expired_sha256 = runtime.checkpoint.get("expired_auth_profile_sha256")
        if current_sha256 is not None and (runtime.checkpoint["state"] == "auth_required" or current_sha256 != expired_sha256):
            runtime.checkpoint["terminal"] = False
            runtime.checkpoint["active_auth_profile_sha256"] = current_sha256
            _record_state(runtime.checkpoint, "capture", outcome="encrypted_auth_profile_available")
            self._write_checkpoint(runtime)

    def _deploy_resume(self, runtime: SiteRuntime) -> None:
        checkpoint = runtime.checkpoint
        if checkpoint.get("state") != "failed" or (checkpoint.get("last_result") or {}).get("stage") != "deploy":
            return
        command = render_stage_command(
            runtime.spec.get("commands", {}).get("deploy", []),
            site=runtime.site,
            stage="deploy",
            repair_round=int(checkpoint.get("repair_round", 0)),
        )
        resolved = _verified_deployment_result(
            command,
            root=_path(runtime.spec.get("cwd"), self.root) or self.root,
            site=runtime.site,
        )
        if resolved is None:
            return
        result_path, result = resolved
        checkpoint["deployment_result"] = {
            "path": str(result_path),
            "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "sandbox_id": result["sandbox_id"],
            "gateway_url": result["gateway_url"],
        }
        _record_state(
            checkpoint,
            "deployed",
            outcome="verified_deployment_result_reconciled",
            sandbox_id=result["sandbox_id"],
        )
        self._write_checkpoint(runtime)

    def _pool_counts(self) -> dict[str, int]:
        counts = {"capture": 0, "builder": 0, "verifier": 0}
        for runtime in self.runtimes:
            if runtime.checkpoint.get("active_stage"):
                counts[POOLS[runtime.checkpoint["active_stage"]]] += 1
        return counts

    def _launch(self, runtime: SiteRuntime, stage: str) -> None:
        attempts = runtime.checkpoint.setdefault("stage_attempts", {})
        attempt_key = _attempt_key(runtime.checkpoint, stage)
        attempts[attempt_key] = int(attempts.get(attempt_key, 0)) + 1
        token = fingerprint({
            "spec": self.spec_sha256,
            "site": runtime.site,
            "stage": stage,
            "attempt": attempts[attempt_key],
            "repair_round": runtime.checkpoint.get("repair_round", 0),
            "repair_kind": runtime.checkpoint.get("repair_kind"),
        })
        result_path = self.result_dir / f"{token}.json"
        task_path = self.task_dir / f"{token}.json"
        timeout_values = runtime.spec.get("timeouts_seconds") or {}
        default_timeout = 7200 if POOLS[stage] == "builder" else 900
        task = {
            "schema": "webanything-synthetic-stage-task-v1",
            "site": runtime.site,
            "stage": stage,
            "token": token,
            "repair_round": runtime.checkpoint.get("repair_round", 0),
            "command": render_stage_command(
                runtime.spec["commands"][stage],
                site=runtime.site,
                stage=stage,
                repair_round=int(runtime.checkpoint.get("repair_round", 0)),
            ),
            "cwd": str(_path(runtime.spec.get("cwd"), self.root) or self.root),
            "timeout_seconds": float(timeout_values.get(stage, default_timeout)),
            "lock_path": str(self.lock_dir / f"{runtime.site}.lock"),
            "result_path": str(result_path),
            "stdout_path": str(self.log_dir / runtime.site / f"{token}.stdout.log"),
            "stderr_path": str(self.log_dir / runtime.site / f"{token}.stderr.log"),
        }
        atomic_json(task_path, task)
        runtime.checkpoint["active_stage"] = stage
        runtime.checkpoint["active_token"] = token
        if stage == "capture":
            profile = _path((runtime.spec.get("artifacts") or {}).get("auth_profile"), self.root)
            runtime.checkpoint["active_auth_profile_sha256"] = (
                hashlib.sha256(profile.read_bytes()).hexdigest()
                if profile is not None and profile.is_file()
                else None
            )
        runtime.checkpoint["updated_at"] = utc_now()
        self._write_checkpoint(runtime)
        runtime.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--stage-worker", str(task_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        runtime.pool = POOLS[stage]

    def _write_summary(self) -> None:
        states = [runtime.checkpoint["state"] for runtime in self.runtimes]
        successful = bool(states) and all(state in {"released", "deployed"} for state in states)
        atomic_json(self.summary_path, {
            "schema": "webanything-synthetic-pipeline-summary-v1",
            "spec_sha256": self.spec_sha256,
            "status": "deployed" if states and all(state == "deployed" for state in states) else "released" if successful else "running" if any(state not in TERMINAL_STATES for state in states) else "attention_required",
            "updated_at": utc_now(),
            "pool_limits": self.spec["pool_limits"],
            "counts": {state: states.count(state) for state in sorted(set(states))},
            "sites": [{
                "site": runtime.site,
                "state": runtime.checkpoint["state"],
                "repair_round": runtime.checkpoint.get("repair_round", 0),
                "repair_kind": runtime.checkpoint.get("repair_kind"),
                "repair_counts": _repair_counts(runtime.checkpoint),
                "active_stage": runtime.checkpoint.get("active_stage"),
            } for runtime in self.runtimes],
        })

    def run(self, *, poll_seconds: float = 0.5) -> int:
        self._acquire()
        try:
            for runtime in self.runtimes:
                self._deploy_resume(runtime)
                self._write_checkpoint(runtime)
            while True:
                for runtime in self.runtimes:
                    self._auth_resume(runtime)
                    self._reconcile(runtime)
                    self._deploy_resume(runtime)
                counts = self._pool_counts()
                for runtime in self.runtimes:
                    state = runtime.checkpoint["state"]
                    if state in TERMINAL_STATES or state in {"auth_required", "sanitize", "reference_frozen"}:
                        continue
                    if runtime.checkpoint.get("active_stage"):
                        result_path = self._result_path(runtime)
                        if result_path is not None and not result_path.exists() and runtime.process is None:
                            task_path = self.task_dir / f"{runtime.checkpoint['active_token']}.json"
                            if not task_path.is_file():
                                _record_state(runtime.checkpoint, "failed", outcome="stage_task_missing")
                                runtime.checkpoint["active_stage"] = None
                                runtime.checkpoint["active_token"] = None
                                self._write_checkpoint(runtime)
                                continue
                            runtime.process = subprocess.Popen(
                                [sys.executable, str(Path(__file__).resolve()), "--stage-worker", str(task_path)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            )
                        continue
                    stage = state
                    if stage not in STAGES:
                        raise SyntheticPipelineError(f"unsupported checkpoint state for {runtime.site}: {stage}")
                    pool = POOLS[stage]
                    if counts[pool] < self.spec["pool_limits"][pool]:
                        self._launch(runtime, stage)
                        counts[pool] += 1
                self._write_summary()
                if all(runtime.checkpoint["state"] in TERMINAL_STATES or runtime.checkpoint["state"] == "auth_required" for runtime in self.runtimes):
                    break
                time.sleep(poll_seconds)
            self._write_summary()
            return 0 if all(runtime.checkpoint["state"] in {"released", "deployed"} for runtime in self.runtimes) else 2
        finally:
            self._release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--stage-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.stage_worker:
        return _worker(args.stage_worker)
    if args.spec is None or args.state_dir is None:
        parser.error("--spec and --state-dir are required")
    root, spec = load_spec(args.spec)
    return Supervisor(root=root, spec=spec, state_dir=args.state_dir).run()


if __name__ == "__main__":
    raise SystemExit(main())
