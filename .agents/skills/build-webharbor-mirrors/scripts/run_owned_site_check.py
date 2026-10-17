#!/usr/bin/env python3
"""Run one bounded check against a temporary, process-owned mirror server."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[4]


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def stop_group(process: subprocess.Popen[bytes] | None) -> None:
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


def run_bounded_check(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
) -> tuple[int, str, str, str | None]:
    """Run a check in its own group and always reap that group on timeout."""

    child = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=timeout)
        return child.returncode, stdout, stderr, None
    except subprocess.TimeoutExpired as exc:
        stop_group(child)
        stdout, stderr = child.communicate()
        return 124, stdout or (exc.stdout or ""), stderr or (exc.stderr or ""), "timed_out"


def wait_for_health(base_url: str, expected_site: str, process: subprocess.Popen[bytes], timeout: float) -> None:
    parsed = urlsplit(base_url)
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"site process exited with {process.returncode}")
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            connection.request("GET", "/_health")
            response = connection.getresponse()
            payload = json.loads(response.read())
            if response.status == 200 and payload.get("site") == expected_site:
                return
            last_error = f"unexpected health payload: {payload!r}"
        except Exception as exc:
            last_error = repr(exc)
        finally:
            if connection is not None:
                connection.close()
        time.sleep(0.2)
    raise RuntimeError(f"site did not become healthy: {last_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--source-site", required=True)
    parser.add_argument("--expected-site")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--runtime-python", default=sys.executable)
    parser.add_argument("--startup-timeout-seconds", type=float, default=45)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--log", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a check command is required after --")
    if args.port < 0 or args.port > 65535:
        parser.error("--port must be 0 or a valid TCP port")
    if args.timeout_seconds <= 0 or args.startup_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    return args


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    expected_site = args.expected_site or args.source_site
    port = args.port or available_port()
    base_url = f"http://127.0.0.1:{port}"
    runner = workspace / "src/webanything/runtime.py"
    app = workspace / "sites" / args.source_site / "app.py"
    if not runner.is_file() or not app.is_file():
        raise SystemExit("workspace must contain the WebAnything runtime and the requested site app.py")

    dependency_candidates = [
        workspace / ".assets" / "builder-python",
        ROOT / ".assets" / "builder-python",
    ]
    dependency_path = next((path for path in dependency_candidates if path.is_dir()), None)
    environment = {
        **os.environ,
        "WEBANYTHING_OWNED_BASE_URL": base_url,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if dependency_path is not None:
        environment["PYTHONPATH"] = str(dependency_path) + (
            os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
        )

    command = [token.replace("{base_url}", base_url) for token in args.command]
    if args.log:
        log_path = args.log.resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        handle, raw_path = tempfile.mkstemp(prefix="webanything-owned-site-", suffix=".log")
        os.close(handle)
        log_path = Path(raw_path)

    process: subprocess.Popen[bytes] | None = None
    result: dict[str, object] = {
        "schema_version": 1,
        "site": expected_site,
        "source_site": args.source_site,
        "base_url": base_url,
        "command": command,
        "server_log": str(log_path),
    }
    returncode = 70
    try:
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [
                    args.runtime_python,
                    str(runner),
                    "serve",
                    "--site", args.source_site,
                    "--port", str(port),
                    "--host", "127.0.0.1",
                    "--sites-root", str(workspace / "sites"),
                ],
                cwd=workspace,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        wait_for_health(base_url, expected_site, process, args.startup_timeout_seconds)
        returncode, stdout, stderr, check_status = run_bounded_check(
            command,
            cwd=workspace,
            environment=environment,
            timeout=args.timeout_seconds,
        )
        result.update({
            "status": (
                check_status
                if check_status
                else "passed" if returncode == 0 else "check_failed"
            ),
            "check_returncode": returncode,
            "stdout_tail": stdout[-8000:],
            "stderr_tail": stderr[-8000:],
        })
    except Exception as exc:
        result.update({"status": "server_failed", "check_returncode": returncode, "error": repr(exc)})
    finally:
        owner_pid = process.pid if process else None
        stop_group(process)
        result["owner_pid"] = owner_pid
        result["server_reaped"] = process is None or process.poll() is not None
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
