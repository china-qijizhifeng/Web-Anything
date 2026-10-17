#!/usr/bin/env python3
"""Run a bounded batch of WebHarbor workers without human continuation.

The workers in a generated SDK batch are long-running, stateful shell jobs.
This supervisor gives them a small, durable control plane: each worker is
started in its own process group, non-terminal exits and bounded wall-clock
timeouts are retried with exponential backoff, status and heartbeat files are
atomically replaced, and a batch summary is updated while the batch runs. A
worker may declare a separate retry command so recovery can preserve a Builder
workspace or resume Verifier checkpoints. The worker's own acceptance gate
remains authoritative; this process only schedules and records it.

The JSON format intentionally accepts command *arrays* only.  A shell can be
the first command when a worker needs one, but the supervisor never evaluates a
command string itself.  Relative paths are resolved from the spec's ``root``
or the command line ``--root``.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
DEFAULT_TERMINAL_STATES = frozenset(
    {
        "published",
        "succeeded",
        "success",
        "passed",
        "completed",
        "failed",
        "timed_out",
        "exhausted",
        "rejected",
        "cancelled",
        "canceled",
        "abandoned",
        # States emitted by the generated URL/SKD workers.  They are terminal
        # failures, not reasons to silently relax a site's acceptance gates.
        "evidence_failed",
        "capture_failed",
        "policy_blocked",
        "interrupted",
    }
)
# An SDK worker can exhaust its own bounded repair rounds while leaving a
# valid trace/workspace behind.  Treat that explicit state as recoverable at
# the batch layer, but keep the retry bounded by WorkerSpec.max_attempts.
# Other terminal states remain fail-closed unless a spec opts in explicitly.
DEFAULT_RETRYABLE_TERMINAL_STATES = frozenset({"exhausted", "timed_out"})
DEFAULT_SUCCESS_STATES = frozenset(
    {"published", "succeeded", "success", "passed", "completed"}
)
DEFAULT_RESTART_DELAY_SECONDS = 5.0
DEFAULT_MAX_RESTART_DELAY_SECONDS = 180.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0
DEFAULT_SHUTDOWN_GRACE_SECONDS = 8.0
WORKER_ID_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SupervisorError(RuntimeError):
    """A configuration or lifecycle error that should fail closed."""


def utc_now() -> str:
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write JSON such that readers never observe a partial document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # fsyncing the containing directory makes the rename durable on the
        # local filesystems used by the batch runners.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    # Overlay/network filesystems can reject directory fsync;
                    # the completed atomic rename remains the reader invariant.
                    pass
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def json_fingerprint(value: Any) -> str | None:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def file_fingerprint(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def resolve_path(value: str | os.PathLike[str] | None, base: Path) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def normalized_state(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip().casefold()
    return result or None


def worker_state(payload: Mapping[str, Any] | None) -> str | None:
    if not payload:
        return None
    return normalized_state(
        payload.get("state")
        or payload.get("status")
        or payload.get("latest_execution_status")
        or payload.get("execution_status")
    )


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    status_file: Path | None
    max_attempts: int
    terminal_states: frozenset[str]
    retryable_terminal_states: frozenset[str]
    success_states: frozenset[str]
    require_terminal_status: bool
    restart_delay_seconds: float
    max_restart_delay_seconds: float
    retry_command: tuple[str, ...] | None = None
    timeout_seconds: float | None = None
    log_file: Path | None = None


@dataclass
class WorkerRuntime:
    spec: WorkerSpec
    state: str = "pending"
    terminal: bool = False
    success: bool | None = None
    attempt: int = 0
    restart_count: int = 0
    process: subprocess.Popen[bytes] | None = None
    pid: int | None = None
    pgid: int | None = None
    started_at: str | None = None
    started_monotonic: float | None = None
    finished_at: str | None = None
    last_exit_code: int | None = None
    observed_worker_state: str | None = None
    last_error: str | None = None
    next_start_monotonic: float = 0.0
    next_start_at: str | None = None
    last_heartbeat_monotonic: float = 0.0
    last_heartbeat_at: str | None = None
    status_before_fingerprint: str | None = None
    status_before_mtime_ns: int | None = None
    log_path: Path | None = None
    restored: bool = False
    spec_fingerprint: str | None = None
    active_command_kind: str | None = None
    attempt_history: list[dict[str, Any]] = field(default_factory=list)

    def public_payload(self, supervisor_pid: int, heartbeat_at: str | None = None) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "worker_id": self.spec.worker_id,
            "spec_fingerprint": self.spec_fingerprint,
            "state": self.state,
            "terminal": self.terminal,
            "success": self.success,
            "attempt": self.attempt,
            "restart_count": self.restart_count,
            "pid": self.pid,
            "pgid": self.pgid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "active_command_kind": self.active_command_kind,
            "retry_command_configured": self.spec.retry_command is not None,
            "timeout_seconds": self.spec.timeout_seconds,
            "last_exit_code": self.last_exit_code,
            "observed_worker_state": self.observed_worker_state,
            "last_error": self.last_error,
            "next_start_at": self.next_start_at,
            "attempt_history": list(self.attempt_history),
            "heartbeat_at": heartbeat_at,
            "supervisor_pid": supervisor_pid,
            "worker_status_file": str(self.spec.status_file) if self.spec.status_file else None,
            "log_file": str(self.log_path) if self.log_path else None,
            "acceptance_gates_relaxed": False,
            "human_review_required": False,
        }


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise SupervisorError(f"{field_name} must be a non-empty list of strings")
    return tuple(value)


def _state_set(
    value: Any,
    field_name: str,
    fallback: frozenset[str],
    *,
    allow_empty: bool = False,
) -> frozenset[str]:
    if value is None:
        return fallback
    if not isinstance(value, list) or (not allow_empty and not value) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SupervisorError(f"{field_name} must be a list of non-empty strings")
    return frozenset(item.strip().casefold() for item in value)


def _positive_number(value: Any, field_name: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SupervisorError(f"{field_name} must be a number") from exc
    if number < 0 or (number == 0 and not allow_zero):
        comparator = "non-negative" if allow_zero else "positive"
        raise SupervisorError(f"{field_name} must be {comparator}")
    return number


def _attempts(value: Any, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SupervisorError(f"{field_name} must be a positive integer") from exc
    if number < 1:
        raise SupervisorError(f"{field_name} must be a positive integer")
    return number


def _optional_positive_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _positive_number(value, field_name)


def load_spec(spec_path: Path, *, root_override: Path | None = None) -> tuple[Path, dict[str, Any], list[WorkerSpec]]:
    """Load and validate a batch spec.

    The returned root is absolute and all worker paths are already resolved.
    Keeping this parser independent makes it useful to tests and to future
    generated batches without coupling it to one site's shell runner.
    """

    spec_path = spec_path.expanduser().resolve()
    payload = read_json(spec_path)
    if payload is None:
        raise SupervisorError(f"invalid or unreadable JSON spec: {spec_path}")
    raw_root = root_override or resolve_path(payload.get("root"), spec_path.parent) or spec_path.parent
    root = raw_root.expanduser().resolve()
    if not root.is_dir():
        raise SupervisorError(f"spec root does not exist: {root}")
    raw_workers = payload.get("workers")
    if not isinstance(raw_workers, list) or not raw_workers:
        raise SupervisorError("spec workers must be a non-empty list")
    top_terminal = _state_set(payload.get("terminal_states"), "terminal_states", DEFAULT_TERMINAL_STATES)
    top_retryable_terminal = _state_set(
        payload.get("retryable_terminal_states"),
        "retryable_terminal_states",
        DEFAULT_RETRYABLE_TERMINAL_STATES,
        allow_empty=True,
    )
    if not top_retryable_terminal.issubset(top_terminal):
        raise SupervisorError("retryable_terminal_states must be a subset of terminal_states")
    top_success = _state_set(payload.get("success_states"), "success_states", DEFAULT_SUCCESS_STATES)
    if not top_success.issubset(top_terminal):
        raise SupervisorError("success_states must be a subset of terminal_states")
    top_max_attempts = _attempts(payload.get("max_attempts", 3), "max_attempts")
    top_timeout = _optional_positive_number(
        payload.get("worker_timeout_seconds"),
        "worker_timeout_seconds",
    )
    top_delay = _positive_number(
        payload.get("restart_delay_seconds", DEFAULT_RESTART_DELAY_SECONDS),
        "restart_delay_seconds",
        allow_zero=True,
    )
    top_max_delay = _positive_number(
        payload.get("max_restart_delay_seconds", DEFAULT_MAX_RESTART_DELAY_SECONDS),
        "max_restart_delay_seconds",
        allow_zero=True,
    )
    if top_max_delay < top_delay:
        raise SupervisorError("max_restart_delay_seconds must be >= restart_delay_seconds")
    top_require_status = bool(payload.get("require_terminal_status", True))
    workers: list[WorkerSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_workers):
        if not isinstance(item, dict):
            raise SupervisorError(f"workers[{index}] must be an object")
        worker_id = item.get("id", item.get("worker_id"))
        if not isinstance(worker_id, str) or not WORKER_ID_RE.fullmatch(worker_id):
            raise SupervisorError(f"workers[{index}] has an invalid id")
        if worker_id in seen:
            raise SupervisorError(f"duplicate worker id: {worker_id}")
        seen.add(worker_id)
        command = _string_list(item.get("command"), f"workers[{index}].command")
        retry_command = (
            _string_list(item.get("retry_command"), f"workers[{index}].retry_command")
            if item.get("retry_command") is not None
            else None
        )
        raw_env = item.get("env", {})
        if not isinstance(raw_env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()):
            raise SupervisorError(f"workers[{index}].env must map strings to strings")
        cwd = resolve_path(item.get("cwd"), root) or root
        if not cwd.is_dir():
            raise SupervisorError(f"worker {worker_id} cwd does not exist: {cwd}")
        status_file = resolve_path(item.get("status_file"), root)
        log_file = resolve_path(item.get("log_file"), root)
        max_attempts = _attempts(item.get("max_attempts", top_max_attempts), f"workers[{index}].max_attempts")
        timeout_seconds = _optional_positive_number(
            item.get("timeout_seconds", top_timeout),
            f"workers[{index}].timeout_seconds",
        )
        delay = _positive_number(item.get("restart_delay_seconds", top_delay), f"workers[{index}].restart_delay_seconds", allow_zero=True)
        max_delay = _positive_number(item.get("max_restart_delay_seconds", top_max_delay), f"workers[{index}].max_restart_delay_seconds", allow_zero=True)
        if max_delay < delay:
            raise SupervisorError(f"worker {worker_id} max restart delay is below its base delay")
        terminal_states = _state_set(
            item.get("terminal_states"),
            f"workers[{index}].terminal_states",
            top_terminal,
        )
        retryable_terminal_states = _state_set(
            item.get("retryable_terminal_states"),
            f"workers[{index}].retryable_terminal_states",
            top_retryable_terminal,
            allow_empty=True,
        )
        success_states = _state_set(
            item.get("success_states"),
            f"workers[{index}].success_states",
            top_success,
        )
        if not success_states.issubset(terminal_states):
            raise SupervisorError(f"worker {worker_id} success_states must be a subset of terminal_states")
        if not retryable_terminal_states.issubset(terminal_states):
            raise SupervisorError(f"worker {worker_id} retryable_terminal_states must be a subset of terminal_states")
        workers.append(
            WorkerSpec(
                worker_id=worker_id,
                command=command,
                retry_command=retry_command,
                cwd=cwd,
                env=dict(raw_env),
                status_file=status_file,
                max_attempts=max_attempts,
                terminal_states=terminal_states,
                retryable_terminal_states=retryable_terminal_states,
                success_states=success_states,
                require_terminal_status=bool(item.get("require_terminal_status", top_require_status)),
                restart_delay_seconds=delay,
                max_restart_delay_seconds=max_delay,
                timeout_seconds=timeout_seconds,
                log_file=log_file,
            )
        )
    normalized = dict(payload)
    normalized["schema_version"] = payload.get("schema_version", SCHEMA_VERSION)
    return root, normalized, workers


class BatchSupervisor:
    """A durable, bounded scheduler for :class:`WorkerSpec` values."""

    def __init__(
        self,
        *,
        root: Path,
        spec_payload: Mapping[str, Any],
        workers: Iterable[WorkerSpec],
        state_dir: Path,
        status_dir: Path,
        heartbeat_dir: Path,
        log_dir: Path,
        summary_path: Path,
        pid_file: Path,
        lock_file: Path,
        max_concurrency: int,
        poll_interval_seconds: float,
        heartbeat_interval_seconds: float,
        shutdown_grace_seconds: float,
    ) -> None:
        if max_concurrency < 1:
            raise SupervisorError("max_concurrency must be positive")
        if poll_interval_seconds <= 0 or heartbeat_interval_seconds <= 0:
            raise SupervisorError("poll and heartbeat intervals must be positive")
        self.root = root
        self.spec_payload = dict(spec_payload)
        self.runtimes = [WorkerRuntime(spec=worker) for worker in workers]
        self.state_dir = state_dir
        self.status_dir = status_dir
        self.heartbeat_dir = heartbeat_dir
        self.log_dir = log_dir
        self.summary_path = summary_path
        self.pid_file = pid_file
        self.lock_file = lock_file
        self.max_concurrency = max_concurrency
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.pid = os.getpid()
        self.started_at = utc_now()
        self.updated_at = self.started_at
        self.stopping = False
        self.stop_signal: int | None = None
        self._lock_handle: Any = None
        self.spec_fingerprint = json_fingerprint(self.spec_payload)

        # A supervisor restart should reclaim an in-flight worker, while a
        # previously accepted worker should remain accepted.  Only status
        # documents carrying this exact spec fingerprint are eligible; stale
        # files from another batch cannot short-circuit a new run.
        for runtime in self.runtimes:
            runtime.spec_fingerprint = self.spec_fingerprint
            self._restore_runtime(runtime)

    def _restore_runtime(self, runtime: WorkerRuntime) -> None:
        payload = read_json(self._status_path(runtime))
        if not payload or payload.get("spec_fingerprint") != self.spec_fingerprint:
            return
        try:
            attempt = max(0, int(payload.get("attempt", 0)))
            restart_count = max(0, int(payload.get("restart_count", 0)))
        except (TypeError, ValueError):
            return
        previous_state = normalized_state(payload.get("state"))
        previous_terminal = bool(payload.get("terminal"))
        previous_success = payload.get("success") is True
        runtime.attempt = attempt
        runtime.restart_count = restart_count
        runtime.started_at = payload.get("started_at") if isinstance(payload.get("started_at"), str) else None
        runtime.finished_at = payload.get("finished_at") if isinstance(payload.get("finished_at"), str) else None
        runtime.last_exit_code = payload.get("last_exit_code") if isinstance(payload.get("last_exit_code"), int) else None
        runtime.observed_worker_state = normalized_state(payload.get("observed_worker_state"))
        runtime.last_error = payload.get("last_error") if isinstance(payload.get("last_error"), str) else None
        history = payload.get("attempt_history")
        if isinstance(history, list):
            runtime.attempt_history = [dict(item) for item in history if isinstance(item, dict)][
                -runtime.spec.max_attempts:
            ]
        runtime.active_command_kind = (
            payload.get("active_command_kind")
            if payload.get("active_command_kind") in {"initial", "retry"}
            else None
        )
        if previous_terminal and previous_success and previous_state in runtime.spec.success_states:
            runtime.state = "succeeded"
            runtime.terminal = True
            runtime.success = True
            runtime.restored = True
            return
        if (
            previous_terminal
            and not previous_success
            and runtime.observed_worker_state in runtime.spec.terminal_states
            and runtime.observed_worker_state not in runtime.spec.retryable_terminal_states
        ):
            runtime.state = "failed"
            runtime.terminal = True
            runtime.success = False
            runtime.restored = True
            return
        # Retryable terminal or infrastructure failures can resume after the
        # supervisor restarts, but their attempt budget and history survive.
        if attempt >= runtime.spec.max_attempts:
            runtime.state = "failed"
            runtime.terminal = True
            runtime.success = False
            runtime.last_error = "attempt limit already consumed by a prior supervisor run"
            runtime.restored = True
            return
        runtime.state = "pending"
        runtime.terminal = False
        runtime.success = None
        runtime.next_start_monotonic = 0.0
        runtime.next_start_at = None
        runtime.restored = True

    def _status_path(self, runtime: WorkerRuntime) -> Path:
        return self.status_dir / f"{runtime.spec.worker_id}.json"

    def _heartbeat_path(self, runtime: WorkerRuntime) -> Path:
        return self.heartbeat_dir / f"{runtime.spec.worker_id}.json"

    def _write_runtime(self, runtime: WorkerRuntime, *, force_heartbeat: bool = True) -> None:
        now = utc_now()
        runtime.last_heartbeat_at = now
        runtime.last_heartbeat_monotonic = time.monotonic()
        atomic_json(self._status_path(runtime), runtime.public_payload(self.pid, now))
        if force_heartbeat:
            atomic_json(
                self._heartbeat_path(runtime),
                {
                    "schema_version": SCHEMA_VERSION,
                    "worker_id": runtime.spec.worker_id,
                    "supervisor_pid": self.pid,
                    "state": runtime.state,
                    "terminal": runtime.terminal,
                    "attempt": runtime.attempt,
                    "restart_count": runtime.restart_count,
                    "active_command_kind": runtime.active_command_kind,
                    "timeout_seconds": runtime.spec.timeout_seconds,
                    "pid": runtime.pid,
                    "pgid": runtime.pgid,
                    "alive": runtime.process is not None and runtime.process.poll() is None,
                    "heartbeat_at": now,
                    "acceptance_gates_relaxed": False,
                    "human_review_required": False,
                },
            )

    def _worker_payload(self, runtime: WorkerRuntime) -> dict[str, Any]:
        return runtime.public_payload(self.pid, self.updated_at)

    def _summary(self, state: str | None = None) -> dict[str, Any]:
        states = [runtime.state for runtime in self.runtimes]
        terminal = [runtime for runtime in self.runtimes if runtime.terminal]
        successes = [runtime for runtime in terminal if runtime.success is True]
        failures = [runtime for runtime in terminal if runtime.success is False]
        if state is None:
            if self.stopping:
                state = "interrupted"
            elif len(terminal) == len(self.runtimes):
                state = "completed" if not failures else "failed"
            else:
                state = "running"
        counts = {name: states.count(name) for name in sorted(set(states))}
        return {
            "schema_version": SCHEMA_VERSION,
            "batch_id": self.spec_payload.get("batch_id") or self.spec_payload.get("id") or self.summary_path.stem,
            "state": state,
            "success": state == "completed" and len(successes) == len(self.runtimes),
            "supervisor_pid": self.pid,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "max_concurrency": self.max_concurrency,
            "worker_count": len(self.runtimes),
            "counts": counts,
            "terminal_count": len(terminal),
            "successful_count": len(successes),
            "failed_count": len(failures),
            "human_review_required": False,
            "manual_intervention_required": False,
            "acceptance_gates_relaxed": False,
            "workers": [self._worker_payload(runtime) for runtime in self.runtimes],
        }

    def _write_summary(self, state: str | None = None) -> None:
        self.updated_at = utc_now()
        atomic_json(self.summary_path, self._summary(state))

    def _acquire_lock(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = self.lock_file.open("a+", encoding="ascii")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_handle.close()
            self._lock_handle = None
            raise SupervisorError(f"another supervisor owns lock: {self.lock_file}") from exc
        self._lock_handle.seek(0)
        self._lock_handle.truncate()
        self._lock_handle.write(f"{self.pid}\n")
        self._lock_handle.flush()
        atomic_json(
            self.pid_file,
            {"schema_version": SCHEMA_VERSION, "pid": self.pid, "started_at": self.started_at},
        )

    def _release_lock(self) -> None:
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_handle.close()
                self._lock_handle = None
        try:
            current = read_json(self.pid_file)
            if current and int(current.get("pid", -1)) == self.pid:
                self.pid_file.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            pass

    def request_stop(self, signum: int) -> None:
        self.stopping = True
        self.stop_signal = signum

    def _running(self) -> list[WorkerRuntime]:
        return [runtime for runtime in self.runtimes if runtime.process is not None]

    def _log_path(self, runtime: WorkerRuntime) -> Path:
        if runtime.spec.log_file is not None:
            return runtime.spec.log_file
        return self.log_dir / runtime.spec.worker_id / f"attempt-{runtime.attempt}.log"

    @staticmethod
    def _attempt_command(runtime: WorkerRuntime) -> tuple[tuple[str, ...], str]:
        if runtime.attempt > 1 and runtime.spec.retry_command is not None:
            return runtime.spec.retry_command, "retry"
        return runtime.spec.command, "initial"

    @staticmethod
    def _record_attempt(
        runtime: WorkerRuntime,
        *,
        outcome: str,
        reason: str | None = None,
        timed_out: bool = False,
    ) -> None:
        if runtime.attempt < 1:
            return
        record = {
            "attempt": runtime.attempt,
            "command_kind": runtime.active_command_kind,
            "started_at": runtime.started_at,
            "finished_at": runtime.finished_at or utc_now(),
            "outcome": outcome,
            "timed_out": timed_out,
            "exit_code": runtime.last_exit_code,
            "observed_worker_state": runtime.observed_worker_state,
            "reason": reason,
        }
        if runtime.attempt_history and runtime.attempt_history[-1].get("attempt") == runtime.attempt:
            runtime.attempt_history[-1] = record
        else:
            runtime.attempt_history.append(record)
        runtime.attempt_history = runtime.attempt_history[-runtime.spec.max_attempts:]

    def _launch(self, runtime: WorkerRuntime, now: float) -> None:
        runtime.attempt += 1
        command, command_kind = self._attempt_command(runtime)
        runtime.state = "running"
        runtime.terminal = False
        runtime.success = None
        runtime.started_at = utc_now()
        runtime.started_monotonic = now
        runtime.finished_at = None
        runtime.last_exit_code = None
        runtime.last_error = None
        runtime.observed_worker_state = None
        runtime.active_command_kind = command_kind
        runtime.next_start_at = None
        runtime.status_before_fingerprint = file_fingerprint(runtime.spec.status_file)
        try:
            runtime.status_before_mtime_ns = (
                runtime.spec.status_file.stat().st_mtime_ns
                if runtime.spec.status_file is not None
                else None
            )
        except OSError:
            runtime.status_before_mtime_ns = None
        log_path = self._log_path(runtime)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(runtime.spec.env)
        environment.update(
            {
                "WEBHARBOR_BATCH_WORKER_ID": runtime.spec.worker_id,
                "WEBHARBOR_BATCH_ATTEMPT": str(runtime.attempt),
                "WEBHARBOR_BATCH_COMMAND_KIND": command_kind,
                "WEBHARBOR_BATCH_SUPERVISOR_PID": str(self.pid),
            }
        )
        try:
            log = log_path.open("ab")
            try:
                process = subprocess.Popen(
                    list(command),
                    cwd=runtime.spec.cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                log.close()
        except OSError as exc:
            runtime.last_error = f"launch failed: {exc}"
            runtime.finished_at = utc_now()
            runtime.started_monotonic = None
            runtime.pid = None
            runtime.pgid = None
            runtime.log_path = log_path
            self._schedule_retry_or_fail(runtime, now, None)
            return
        runtime.process = process
        runtime.pid = process.pid
        runtime.pgid = process.pid
        runtime.log_path = log_path
        self._write_runtime(runtime)

    def _schedule_retry_or_fail(
        self,
        runtime: WorkerRuntime,
        now: float,
        returncode: int | None,
        *,
        timed_out: bool = False,
    ) -> None:
        runtime.process = None
        runtime.pid = None
        runtime.pgid = None
        runtime.started_monotonic = None
        runtime.finished_at = utc_now()
        runtime.last_exit_code = returncode
        self._record_attempt(
            runtime,
            outcome="timed_out" if timed_out else "failed",
            reason=runtime.last_error,
            timed_out=timed_out,
        )
        if runtime.attempt < runtime.spec.max_attempts:
            runtime.restart_count += 1
            delay = min(
                runtime.spec.restart_delay_seconds * (2 ** max(0, runtime.restart_count - 1)),
                runtime.spec.max_restart_delay_seconds,
            )
            runtime.next_start_monotonic = now + delay
            runtime.next_start_at = (
                _datetime.datetime.now(_datetime.timezone.utc)
                + _datetime.timedelta(seconds=delay)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            runtime.state = "retrying"
            runtime.terminal = False
            runtime.success = None
        else:
            runtime.state = "failed"
            runtime.terminal = True
            runtime.success = False
            if not runtime.last_error:
                runtime.last_error = "attempt limit reached without an accepted terminal worker state"
        self._write_runtime(runtime)

    def _finish_process(self, runtime: WorkerRuntime, now: float) -> None:
        process = runtime.process
        if process is None:
            return
        returncode = process.poll()
        if returncode is None:
            return
        runtime.process = None
        runtime.pid = None
        runtime.pgid = None
        runtime.started_monotonic = None
        runtime.finished_at = utc_now()
        runtime.last_exit_code = int(returncode)
        payload = read_json(runtime.spec.status_file)
        fingerprint = file_fingerprint(runtime.spec.status_file)
        try:
            mtime_ns = (
                runtime.spec.status_file.stat().st_mtime_ns
                if runtime.spec.status_file is not None
                else None
            )
        except OSError:
            mtime_ns = None
        fresh = (
            fingerprint is not None
            and (
                fingerprint != runtime.status_before_fingerprint
                or (
                    mtime_ns is not None
                    and mtime_ns != runtime.status_before_mtime_ns
                )
            )
        )
        observed = worker_state(payload) if fresh else None
        runtime.observed_worker_state = observed
        if observed in runtime.spec.terminal_states:
            if observed in runtime.spec.retryable_terminal_states:
                runtime.last_error = runtime.last_error or (
                    f"retryable terminal state {observed!r}; starting a bounded fresh worker attempt"
                )
                self._schedule_retry_or_fail(
                    runtime,
                    now,
                    int(returncode),
                    timed_out=observed == "timed_out",
                )
                return
            if returncode == 0 and observed in runtime.spec.success_states:
                runtime.state = "succeeded"
                runtime.terminal = True
                runtime.success = True
                self._record_attempt(runtime, outcome="succeeded")
            else:
                runtime.state = "failed"
                runtime.terminal = True
                runtime.success = False
                runtime.last_error = runtime.last_error or (
                    f"worker terminal state {observed!r} with exit code {returncode}"
                )
                self._record_attempt(runtime, outcome="failed", reason=runtime.last_error)
            self._write_runtime(runtime)
            return
        if returncode == 0 and not runtime.spec.require_terminal_status:
            runtime.state = "succeeded"
            runtime.terminal = True
            runtime.success = True
            runtime.observed_worker_state = observed or "completed_without_status"
            self._record_attempt(runtime, outcome="succeeded")
            self._write_runtime(runtime)
            return
        if observed is None and returncode == 0:
            runtime.last_error = runtime.last_error or "worker exited 0 without a fresh terminal status"
        elif observed is not None:
            runtime.last_error = runtime.last_error or f"worker exited in non-terminal state {observed!r}"
        self._schedule_retry_or_fail(runtime, now, int(returncode))

    def _stop_process_group(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.shutdown_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self.shutdown_grace_seconds)
            except subprocess.TimeoutExpired:
                pass

    def _expire(self, runtime: WorkerRuntime, now: float) -> None:
        process = runtime.process
        if process is None:
            return
        self._stop_process_group(process)
        runtime.last_exit_code = process.returncode
        runtime.finished_at = utc_now()
        runtime.observed_worker_state = "timed_out"
        runtime.last_error = (
            f"worker exceeded wall timeout of {runtime.spec.timeout_seconds:g} seconds"
        )
        self._schedule_retry_or_fail(
            runtime,
            now,
            process.returncode,
            timed_out=True,
        )

    def _terminate(self, runtime: WorkerRuntime) -> None:
        process = runtime.process
        if process is None:
            return
        if process.poll() is not None:
            # Reconcile a child that exited just before SIGTERM arrived.  This
            # preserves a genuine terminal success and otherwise lets the
            # caller mark the row interrupted instead of leaving a phantom
            # running process in the summary.
            self._finish_process(runtime, time.monotonic())
            return
        self._stop_process_group(process)
        runtime.process = None
        runtime.pid = None
        runtime.pgid = None
        runtime.started_monotonic = None
        runtime.finished_at = utc_now()
        runtime.last_exit_code = process.returncode
        runtime.state = "interrupted"
        runtime.terminal = True
        runtime.success = False
        runtime.last_error = runtime.last_error or "supervisor stopped the worker"
        self._record_attempt(runtime, outcome="interrupted", reason=runtime.last_error)
        self._write_runtime(runtime)

    def _mark_not_started_interrupted(self) -> None:
        """Make pending/retry rows terminal when the supervisor is stopped."""

        for runtime in self.runtimes:
            if runtime.terminal:
                continue
            if runtime.process is not None:
                continue
            runtime.state = "interrupted"
            runtime.terminal = True
            runtime.success = False
            runtime.finished_at = utc_now()
            runtime.last_error = runtime.last_error or "supervisor stopped before the worker was started"
            self._write_runtime(runtime)

    def _observe(self, now: float) -> None:
        for runtime in self.runtimes:
            if runtime.process is not None:
                if runtime.process.poll() is not None:
                    self._finish_process(runtime, now)
                elif (
                    runtime.spec.timeout_seconds is not None
                    and runtime.started_monotonic is not None
                    and now - runtime.started_monotonic >= runtime.spec.timeout_seconds
                ):
                    self._expire(runtime, now)
                elif now - runtime.last_heartbeat_monotonic >= self.heartbeat_interval_seconds:
                    payload = read_json(runtime.spec.status_file)
                    runtime.observed_worker_state = worker_state(payload) or runtime.observed_worker_state
                    self._write_runtime(runtime)

    def _schedule(self, now: float) -> None:
        slots = self.max_concurrency - len(self._running())
        if slots <= 0 or self.stopping:
            return
        for runtime in self.runtimes:
            if slots <= 0:
                break
            if runtime.terminal or runtime.process is not None or runtime.state not in {"pending", "retrying"}:
                continue
            if now < runtime.next_start_monotonic:
                continue
            self._launch(runtime, now)
            slots -= 1

    def run(self) -> int:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.status_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._write_summary("running")
            for runtime in self.runtimes:
                self._write_runtime(runtime)
            while True:
                now = time.monotonic()
                if self.stopping:
                    for runtime in self.runtimes:
                        self._terminate(runtime)
                    self._mark_not_started_interrupted()
                    self._write_summary("interrupted")
                    return 128 + (self.stop_signal or signal.SIGTERM)
                self._observe(now)
                self._schedule(now)
                self._write_summary()
                if all(runtime.terminal for runtime in self.runtimes):
                    final_state = "completed" if all(runtime.success for runtime in self.runtimes) else "failed"
                    self._write_summary(final_state)
                    return 0 if final_state == "completed" else 2
                time.sleep(self.poll_interval_seconds)
        finally:
            # Reap every still-running process even if an unexpected
            # supervisor exception occurs; otherwise a detached SDK child can
            # outlive the batch and corrupt the next run's evidence.
            for runtime in self.runtimes:
                if runtime.process is not None:
                    self._terminate(runtime)
            if self.stopping:
                self._mark_not_started_interrupted()
            self._release_lock()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--root", type=Path, help="Override the spec's relative path root")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--status-dir", type=Path)
    parser.add_argument("--heartbeat-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--summary", dest="summary_path", type=Path)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--max-concurrency", type=int)
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        help="Override the wall-clock timeout for every worker attempt.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        help="Override the bounded per-worker attempt budget without changing the persisted spec fingerprint.",
    )
    parser.add_argument("--poll-interval-seconds", type=float)
    parser.add_argument("--heartbeat-interval-seconds", type=float)
    parser.add_argument("--shutdown-grace-seconds", type=float)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        root, payload, workers = load_spec(args.spec, root_override=args.root)
        if args.max_attempts is not None:
            if args.max_attempts < 1:
                raise SupervisorError("max-attempts must be positive")
            workers = [replace(worker, max_attempts=args.max_attempts) for worker in workers]
        if args.worker_timeout_seconds is not None:
            if args.worker_timeout_seconds <= 0:
                raise SupervisorError("worker-timeout-seconds must be positive")
            workers = [
                replace(worker, timeout_seconds=float(args.worker_timeout_seconds))
                for worker in workers
            ]
            payload = dict(payload)
            effective_overrides = dict(payload.get("effective_overrides") or {})
            effective_overrides["worker_timeout_seconds"] = float(args.worker_timeout_seconds)
            payload["effective_overrides"] = effective_overrides
        raw_state_dir = args.state_dir or payload.get("state_dir") or "batch-supervisor-state"
        state_dir = resolve_path(raw_state_dir, root)
        assert state_dir is not None
        status_dir = resolve_path(args.status_dir or payload.get("status_dir") or (state_dir / "status"), root)
        heartbeat_dir = resolve_path(args.heartbeat_dir or payload.get("heartbeat_dir") or (state_dir / "heartbeats"), root)
        log_dir = resolve_path(args.log_dir or payload.get("log_dir") or (state_dir / "logs"), root)
        summary_path = resolve_path(args.summary_path or payload.get("summary_path") or (state_dir / "batch-summary.json"), root)
        pid_file = resolve_path(args.pid_file or payload.get("pid_file") or (state_dir / "supervisor.pid"), root)
        lock_file = resolve_path(args.lock_file or payload.get("lock_file") or (state_dir / "supervisor.lock"), root)
        try:
            max_concurrency = int(
                args.max_concurrency
                if args.max_concurrency is not None
                else payload.get("max_concurrency", 1)
            )
            poll_interval = float(
                args.poll_interval_seconds
                if args.poll_interval_seconds is not None
                else payload.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
            )
            heartbeat_interval = float(
                args.heartbeat_interval_seconds
                if args.heartbeat_interval_seconds is not None
                else payload.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
            )
            shutdown_grace = float(
                args.shutdown_grace_seconds
                if args.shutdown_grace_seconds is not None
                else payload.get("shutdown_grace_seconds", DEFAULT_SHUTDOWN_GRACE_SECONDS)
            )
        except (TypeError, ValueError) as exc:
            raise SupervisorError("numeric supervisor settings are invalid") from exc
        if max_concurrency < 1:
            raise SupervisorError("max_concurrency must be positive")
        if poll_interval <= 0 or heartbeat_interval <= 0 or shutdown_grace <= 0:
            raise SupervisorError("poll, heartbeat, and shutdown grace intervals must be positive")
        supervisor = BatchSupervisor(
            root=root,
            spec_payload=payload,
            workers=workers,
            state_dir=state_dir,
            status_dir=status_dir,
            heartbeat_dir=heartbeat_dir,
            log_dir=log_dir,
            summary_path=summary_path,
            pid_file=pid_file,
            lock_file=lock_file,
            max_concurrency=max_concurrency,
            poll_interval_seconds=poll_interval,
            heartbeat_interval_seconds=heartbeat_interval,
            shutdown_grace_seconds=shutdown_grace,
        )
    except SupervisorError as exc:
        print(f"batch supervisor: {exc}", file=sys.stderr)
        return 64

    def handle_signal(signum: int, _frame: object) -> None:
        supervisor.request_stop(signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        return supervisor.run()
    except SupervisorError as exc:
        print(f"batch supervisor: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
