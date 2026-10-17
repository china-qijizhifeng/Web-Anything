#!/usr/bin/env python3
"""Run high-fidelity source-coupled WebAnything jobs in isolated E2B sandboxes.

This is deliberately a coarse execution backend: one site owns one sandbox and
the existing coupled Explorer/Builder -> read-only Verifier repair loop runs
unchanged inside it.  The controller only schedules sandboxes and moves opaque
checkpoints; it never interprets or publishes browser authorization state.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
E2B_ENV_NAMES = {"E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"}
PROVIDER_ENV_NAMES = {
    "WEBANYTHING_RESPONSES_API_KEY",
    "WEBANYTHING_RESPONSES_BASE_URL",
    "NORTHCUA_OPENAI_RESPONSES_API_KEY",
    "NORTHCUA_OPENAI_RESPONSES_BASE_URL",
}
SOURCE_EXCLUDED_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
PRIVATE_RUN_PARTS = {".session"}
PRIVATE_FILE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
PRIVATE_STATE_NAMES = {"browser-state.json", "storage-state.json", "auth-state.json"}


class E2BCoupledBatchError(RuntimeError):
    """Fail-closed configuration, packaging, or sandbox error."""


@dataclass(frozen=True)
class SiteJob:
    worker_id: str
    source_url: str
    run_dir: Path
    requirements_file: Path
    storage_state: Path | None
    model: str
    reasoning_effort: str
    timeout_minutes: int
    max_cycles: int


@dataclass(frozen=True)
class BatchSpec:
    spec_path: Path
    template: str
    max_concurrency: int
    sandbox_timeout_seconds: int
    bootstrap: bool
    keep_failed_sandboxes: bool
    jobs: tuple[SiteJob, ...]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
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


def _resolve_path(value: Any, base: Path, field: str, *, required: bool = True) -> Path | None:
    if value in (None, ""):
        if required:
            raise E2BCoupledBatchError(f"{field} is required")
        return None
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise E2BCoupledBatchError(f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise E2BCoupledBatchError(f"{field} must be a positive integer") from exc
    if result < 1:
        raise E2BCoupledBatchError(f"{field} must be a positive integer")
    return result


def _public_url(value: Any, field: str) -> str:
    raw = str(value or "")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise E2BCoupledBatchError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise E2BCoupledBatchError(f"{field} must not contain credentials")
    return raw


def load_spec(path: Path) -> BatchSpec:
    path = path.expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise E2BCoupledBatchError(f"invalid E2B coupled batch spec {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise E2BCoupledBatchError("schema_version must be 1")
    template = str(payload.get("template") or "").strip()
    if not template:
        raise E2BCoupledBatchError("template is required")
    workers = payload.get("workers")
    if not isinstance(workers, list) or not workers:
        raise E2BCoupledBatchError("workers must be a non-empty list")
    base = path.parent
    default_model = str(payload.get("model") or "gpt-5.6-sol")
    default_effort = str(payload.get("reasoning_effort") or "xhigh")
    default_timeout = _positive_int(payload.get("timeout_minutes", 90), "timeout_minutes")
    default_cycles = _positive_int(payload.get("max_cycles", 12), "max_cycles")
    if default_effort not in REASONING_EFFORTS:
        raise E2BCoupledBatchError("reasoning_effort is invalid")
    jobs: list[SiteJob] = []
    worker_ids: set[str] = set()
    run_dirs: set[Path] = set()
    for index, item in enumerate(workers):
        if not isinstance(item, dict):
            raise E2BCoupledBatchError(f"workers[{index}] must be an object")
        worker_id = str(item.get("id") or "")
        if not WORKER_ID_RE.fullmatch(worker_id):
            raise E2BCoupledBatchError(f"workers[{index}].id is invalid")
        if worker_id in worker_ids:
            raise E2BCoupledBatchError(f"duplicate worker id: {worker_id}")
        worker_ids.add(worker_id)
        run_dir = _resolve_path(item.get("run_dir"), base, f"workers[{index}].run_dir")
        assert run_dir is not None
        if run_dir in run_dirs:
            raise E2BCoupledBatchError(f"duplicate run_dir: {run_dir}")
        run_dirs.add(run_dir)
        requirements = _resolve_path(
            item.get("requirements_file"), base, f"workers[{index}].requirements_file"
        )
        storage = _resolve_path(
            item.get("storage_state"), base, f"workers[{index}].storage_state", required=False
        )
        assert requirements is not None
        if not requirements.is_file():
            raise E2BCoupledBatchError(f"requirements file is missing: {requirements}")
        if storage is not None and not storage.is_file():
            raise E2BCoupledBatchError(f"storage state is missing: {storage}")
        effort = str(item.get("reasoning_effort") or default_effort)
        if effort not in REASONING_EFFORTS:
            raise E2BCoupledBatchError(f"workers[{index}].reasoning_effort is invalid")
        jobs.append(
            SiteJob(
                worker_id=worker_id,
                source_url=_public_url(item.get("url"), f"workers[{index}].url"),
                run_dir=run_dir,
                requirements_file=requirements,
                storage_state=storage,
                model=str(item.get("model") or default_model),
                reasoning_effort=effort,
                timeout_minutes=_positive_int(
                    item.get("timeout_minutes", default_timeout),
                    f"workers[{index}].timeout_minutes",
                ),
                max_cycles=_positive_int(
                    item.get("max_cycles", default_cycles), f"workers[{index}].max_cycles"
                ),
            )
        )
    return BatchSpec(
        spec_path=path,
        template=template,
        max_concurrency=_positive_int(payload.get("max_concurrency", 4), "max_concurrency"),
        sandbox_timeout_seconds=_positive_int(
            payload.get("sandbox_timeout_seconds", 21_600), "sandbox_timeout_seconds"
        ),
        bootstrap=bool(payload.get("bootstrap", True)),
        keep_failed_sandboxes=bool(payload.get("keep_failed_sandboxes", False)),
        jobs=tuple(jobs),
    )


def load_private_environment(path: Path | None) -> dict[str, str]:
    """Load only explicitly admitted names without ever returning unknown values."""
    if path is None:
        values = {name: os.environ[name] for name in E2B_ENV_NAMES | PROVIDER_ENV_NAMES if os.getenv(name)}
    else:
        try:
            lines = path.expanduser().resolve().read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise E2BCoupledBatchError(f"environment file is unavailable: {path}") from exc
        values: dict[str, str] = {}
        for number, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            name, raw_value = line.split("=", 1)
            name = name.strip()
            if name not in E2B_ENV_NAMES | PROVIDER_ENV_NAMES:
                continue
            try:
                words = shlex.split(raw_value, comments=True, posix=True)
            except ValueError as exc:
                raise E2BCoupledBatchError(f"invalid environment value at {path}:{number}") from exc
            if len(words) > 1:
                raise E2BCoupledBatchError(f"environment value must be one shell word at {path}:{number}")
            values[name] = words[0] if words else ""
    if not values.get("E2B_API_KEY"):
        raise E2BCoupledBatchError("E2B_API_KEY is required")
    api_key = values.get("WEBANYTHING_RESPONSES_API_KEY") or values.get(
        "NORTHCUA_OPENAI_RESPONSES_API_KEY"
    )
    base_url = values.get("WEBANYTHING_RESPONSES_BASE_URL") or values.get(
        "NORTHCUA_OPENAI_RESPONSES_BASE_URL"
    )
    if not api_key or not base_url:
        raise E2BCoupledBatchError("Responses provider credentials are required")
    os.environ.update({name: value for name, value in values.items() if name in E2B_ENV_NAMES})
    return {
        "WEBANYTHING_RESPONSES_API_KEY": api_key,
        "WEBANYTHING_RESPONSES_BASE_URL": base_url,
    }


def _git_source_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise E2BCoupledBatchError(f"cannot inventory repository files: {completed.stderr.decode(errors='replace')}")
    paths: list[Path] = []
    for encoded in completed.stdout.split(b"\0"):
        if not encoded:
            continue
        relative = Path(os.fsdecode(encoded))
        if relative.is_absolute() or ".." in relative.parts or any(
            part in SOURCE_EXCLUDED_PARTS for part in relative.parts
        ):
            continue
        if relative.name.startswith(".env") or relative.suffix.casefold() in PRIVATE_FILE_SUFFIXES:
            continue
        absolute = root / relative
        if absolute.is_symlink():
            raise E2BCoupledBatchError(f"repository package refuses symlink: {relative}")
        if absolute.is_file():
            paths.append(relative)
    return sorted(paths)


def build_source_package(root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for relative in _git_source_files(root):
            archive.add(root / relative, arcname=relative.as_posix(), recursive=False)


def build_checkpoint_package(run_dir: Path, output: Path) -> bool:
    if not run_dir.is_dir():
        return False
    included = False
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(run_dir.rglob("*")):
            relative = path.relative_to(run_dir)
            if any(part in PRIVATE_RUN_PARTS for part in relative.parts):
                continue
            if (
                relative.name.startswith(".env")
                or relative.name.casefold() in PRIVATE_STATE_NAMES
                or relative.suffix.casefold() in PRIVATE_FILE_SUFFIXES
            ):
                continue
            if path.is_symlink():
                raise E2BCoupledBatchError(f"checkpoint package refuses symlink: {path}")
            if path.is_file() and not path.name.endswith(".lock"):
                archive.add(path, arcname=relative.as_posix(), recursive=False)
                included = True
    if not included:
        output.unlink(missing_ok=True)
    return included


def safe_extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk():
                raise E2BCoupledBatchError(f"unsafe result archive member: {member.name}")
        archive.extractall(destination)


def _upload_file(sandbox: Any, local_path: Path, remote_path: str, timeout: int = 1800) -> None:
    with local_path.open("rb") as stream:
        sandbox.files.write(remote_path, stream, request_timeout=timeout)


def _download_file(sandbox: Any, remote_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with sandbox.files.read(remote_path, format="stream", stream_idle_timeout=120) as stream:
        with local_path.open("wb") as handle:
            for chunk in stream:
                handle.write(chunk)


def _checked(sandbox: Any, command: str, *, timeout: int) -> Any:
    result = sandbox.commands.run(command, timeout=timeout, request_timeout=timeout + 60)
    if result.exit_code != 0:
        raise E2BCoupledBatchError(
            f"sandbox bootstrap failed with exit {result.exit_code}: {(result.stderr or result.stdout or '')[-4000:]}"
        )
    return result


def _bootstrap_command(remote_root: str, bootstrap: bool) -> str:
    source = f"{remote_root}/source"
    commands = [
        "set -euo pipefail",
        f"mkdir -p {shlex.quote(source)}",
        f"tar -xzf {shlex.quote(remote_root + '/source.tar.gz')} -C {shlex.quote(source)}",
        f"cd {shlex.quote(source)}",
        "git init -q",
        "git config user.name 'WebAnything E2B'",
        "git config user.email 'webanything@invalid.local'",
        "git add -A",
        "git commit -qm 'sandbox source'",
    ]
    if bootstrap:
        commands.extend(
            [
                "if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs npm; fi",
                "node_major=$(node -p 'process.versions.node.split(\".\")[0]') && test \"$node_major\" -ge 18",
                "python3 -m venv .venv",
                ".venv/bin/python -m pip install -q -e '.[builder,browser,visual]'",
                "npm --prefix tools/codex-mirror-trace ci --omit=dev --no-audit --no-fund",
                ".venv/bin/python -m playwright install chromium",
            ]
        )
    else:
        commands.extend(
            [
                "test -x /opt/nexgarden-runtime/venv/bin/python",
                "test -f /opt/nexgarden-runtime/node_modules/@openai/codex-sdk/dist/index.js",
                "ln -s /opt/nexgarden-runtime/venv .venv",
                "ln -s /opt/nexgarden-runtime/node_modules tools/codex-mirror-trace/node_modules",
            ]
        )
    return " && ".join(commands)


class E2BCoupledBatch:
    def __init__(
        self,
        *,
        repository: Path,
        spec: BatchSpec,
        state_dir: Path,
        provider_env: Mapping[str, str],
        sandbox_factory: Any,
    ) -> None:
        self.repository = repository.resolve()
        self.spec = spec
        self.state_dir = state_dir.expanduser().resolve()
        self.provider_env = dict(provider_env)
        self.sandbox_factory = sandbox_factory
        self.status_dir = self.state_dir / "workers"
        self.summary_path = self.state_dir / "batch-summary.json"
        self.lock_path = self.state_dir / "batch.lock"
        self._guard = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {
            job.worker_id: {
                "worker_id": job.worker_id,
                "source_url": job.source_url,
                "run_dir": str(job.run_dir),
                "state": "pending",
                "sandbox_id": None,
                "started_at": None,
                "finished_at": None,
                "error": None,
            }
            for job in spec.jobs
        }

    def _write_state(self, worker_id: str, **changes: Any) -> None:
        with self._guard:
            self._states[worker_id].update(changes, updated_at=utc_now())
            atomic_json(self.status_dir / f"{worker_id}.json", self._states[worker_id])
            states = list(self._states.values())
            atomic_json(
                self.summary_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "template": self.spec.template,
                    "max_concurrency": self.spec.max_concurrency,
                    "updated_at": utc_now(),
                    "counts": {
                        state: sum(1 for item in states if item["state"] == state)
                        for state in sorted({str(item["state"]) for item in states})
                    },
                    "workers": states,
                },
            )

    def _result_state(self, job: SiteJob, exit_code: int) -> str:
        try:
            status = json.loads(
                (job.run_dir / "auto-supervisor-status.json").read_text(encoding="utf-8")
            )
            state = str(status.get("state") or "incomplete")
        except (OSError, ValueError):
            state = "incomplete"
        if exit_code == 0 and state == "complete":
            return "completed"
        if state in {"auth_required", "source_blocked", "blocked_retry_limit", "candidate_changed"}:
            return state
        return "failed"

    def _download_run(self, sandbox: Any, job: SiteJob, remote_root: str) -> None:
        remote_run = f"{remote_root}/run"
        _checked(
            sandbox,
            f"mkdir -p {shlex.quote(remote_run)} && cd {shlex.quote(remote_run)} && "
            f"tar --exclude='./.session' --exclude='*.lock' -czf "
            f"{shlex.quote(remote_root + '/result.tar.gz')} .",
            timeout=1800,
        )
        with tempfile.TemporaryDirectory(prefix=f"webanything-result-{job.worker_id}-") as temporary:
            archive_path = Path(temporary) / "result.tar.gz"
            _download_file(sandbox, f"{remote_root}/result.tar.gz", archive_path)
            safe_extract(archive_path, job.run_dir)

    def _run_job(self, job: SiteJob, source_package: Path) -> dict[str, Any]:
        sandbox = None
        exit_code = 1
        final_state = "failed"
        keep = False
        self._write_state(job.worker_id, state="creating_sandbox", started_at=utc_now())
        remote_root = f"/tmp/webanything-coupled/{job.worker_id}"
        try:
            sandbox = self.sandbox_factory.create(
                template=self.spec.template,
                timeout=self.spec.sandbox_timeout_seconds,
                secure=True,
                allow_internet_access=True,
                lifecycle={"on_timeout": "pause", "auto_resume": True},
                envs=self.provider_env,
                metadata={
                    "purpose": "webanything-e2b-coupled",
                    "site_id": job.worker_id,
                },
            )
            self._write_state(
                job.worker_id, state="uploading", sandbox_id=str(sandbox.sandbox_id)
            )
            sandbox.commands.run(f"mkdir -p {shlex.quote(remote_root)}", timeout=60)
            _upload_file(sandbox, source_package, f"{remote_root}/source.tar.gz")
            with tempfile.TemporaryDirectory(prefix=f"webanything-{job.worker_id}-") as temporary:
                temporary_root = Path(temporary)
                checkpoint = temporary_root / "checkpoint.tar.gz"
                has_checkpoint = build_checkpoint_package(job.run_dir, checkpoint)
                if has_checkpoint:
                    _upload_file(sandbox, checkpoint, f"{remote_root}/checkpoint.tar.gz")
                _upload_file(sandbox, job.requirements_file, f"{remote_root}/requirements.md")
                if job.storage_state is None:
                    anonymous = temporary_root / "browser-state.json"
                    anonymous.write_text('{"cookies":[],"origins":[]}\n', encoding="utf-8")
                    state_path = anonymous
                else:
                    state_path = job.storage_state
                _upload_file(sandbox, state_path, f"{remote_root}/browser-state.json")
            self._write_state(job.worker_id, state="bootstrapping")
            _checked(
                sandbox,
                _bootstrap_command(remote_root, self.spec.bootstrap),
                timeout=min(self.spec.sandbox_timeout_seconds - 60, 3600),
            )
            remote_run = f"{remote_root}/run"
            if has_checkpoint:
                _checked(
                    sandbox,
                    f"mkdir -p {shlex.quote(remote_run)} && tar -xzf "
                    f"{shlex.quote(remote_root + '/checkpoint.tar.gz')} -C {shlex.quote(remote_run)}",
                    timeout=900,
                )
            command = [
                f"{remote_root}/source/.venv/bin/python",
                "-m",
                "webanything.cli",
                "coupled-pipeline",
                job.source_url,
                "--site",
                job.worker_id,
                "--run-dir",
                remote_run,
                "--requirements-file",
                f"{remote_root}/requirements.md",
                "--storage-state",
                f"{remote_root}/browser-state.json",
                "--model",
                job.model,
                "--reasoning-effort",
                job.reasoning_effort,
                "--timeout-minutes",
                str(job.timeout_minutes),
                "--max-cycles",
                str(job.max_cycles),
            ]
            log_path = self.status_dir / f"{job.worker_id}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_state(job.worker_id, state="running")
            with log_path.open("a", encoding="utf-8") as log:
                result = sandbox.commands.run(
                    " ".join(shlex.quote(value) for value in command),
                    cwd=f"{remote_root}/source",
                    envs={"WEBANYTHING_ROOT": f"{remote_root}/source", "PYTHONPATH": f"{remote_root}/source/src"},
                    timeout=max(60, self.spec.sandbox_timeout_seconds - 120),
                    request_timeout=self.spec.sandbox_timeout_seconds,
                    on_stdout=lambda line: (log.write(line), log.flush()),
                    on_stderr=lambda line: (log.write(line), log.flush()),
                )
            exit_code = int(result.exit_code)
            self._write_state(job.worker_id, state="downloading", exit_code=exit_code)
            self._download_run(sandbox, job, remote_root)
            final_state = self._result_state(job, exit_code)
            keep = self.spec.keep_failed_sandboxes and final_state != "completed"
            self._write_state(
                job.worker_id,
                state=final_state,
                exit_code=exit_code,
                finished_at=utc_now(),
                sandbox_retained=keep,
            )
        except Exception as exc:
            final_state = "failed"
            keep = self.spec.keep_failed_sandboxes and sandbox is not None
            recovery_error = None
            if sandbox is not None:
                try:
                    self._download_run(sandbox, job, remote_root)
                except Exception as recovery_exc:
                    recovery_error = f"; checkpoint recovery failed: {type(recovery_exc).__name__}: {recovery_exc}"
            self._write_state(
                job.worker_id,
                state=final_state,
                error=f"{type(exc).__name__}: {exc}{recovery_error or ''}",
                finished_at=utc_now(),
                sandbox_retained=keep,
            )
        finally:
            if sandbox is not None and not keep:
                try:
                    sandbox.kill()
                except Exception:
                    pass
        return self._states[job.worker_id]

    def run(self) -> int:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open("r+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise E2BCoupledBatchError(f"another controller owns {self.state_dir}") from exc
            with tempfile.TemporaryDirectory(prefix="webanything-e2b-source-") as temporary:
                source_package = Path(temporary) / "source.tar.gz"
                build_source_package(self.repository, source_package)
                for job in self.spec.jobs:
                    self._write_state(job.worker_id)
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(self.spec.max_concurrency, len(self.spec.jobs))
                ) as pool:
                    futures = [pool.submit(self._run_job, job, source_package) for job in self.spec.jobs]
                    for future in concurrent.futures.as_completed(futures):
                        future.result()
        return 0 if all(item["state"] == "completed" for item in self._states.values()) else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--env-file")
    parser.add_argument("--max-concurrency", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_spec(Path(args.spec))
    if args.max_concurrency is not None:
        spec = BatchSpec(
            **{**spec.__dict__, "max_concurrency": _positive_int(args.max_concurrency, "max_concurrency")}
        )
    state_dir = (
        Path(args.state_dir).expanduser().resolve()
        if args.state_dir
        else spec.spec_path.parent / ".webanything-e2b" / spec.spec_path.stem
    )
    provider_env = load_private_environment(Path(args.env_file) if args.env_file else None)
    from e2b import Sandbox

    controller = E2BCoupledBatch(
        repository=Path(__file__).resolve().parents[1],
        spec=spec,
        state_dir=state_dir,
        provider_env=provider_env,
        sandbox_factory=Sandbox,
    )
    return controller.run()


if __name__ == "__main__":
    raise SystemExit(main())
