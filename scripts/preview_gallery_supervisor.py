#!/usr/bin/env python3
"""Keep a WebHarbor preview gateway and its site processes alive.

The E2B command API starts a process asynchronously.  This supervisor owns the
long-lived children so a transient child exit does not leave a running sandbox
with an unusable preview port.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


SITE_RE = re.compile(r"^[A-Za-z0-9_]+$")
RESTART_DELAY_SECONDS = 1.0
MAX_RESTART_DELAY_SECONDS = 30.0


class Child:
    def __init__(self, name: str, command: list[str], log_path: Path) -> None:
        self.name = name
        self.command = command
        self.log_path = log_path
        self.process: subprocess.Popen[bytes] | None = None
        self.restart_delay = RESTART_DELAY_SECONDS
        self.restart_at = 0.0

    def start(self, root: Path) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log.close()
        self.restart_delay = RESTART_DELAY_SECONDS

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

    def tick(self, root: Path, now: float) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            return
        if process is not None:
            exit_code = process.returncode
            self.restart_at = now + self.restart_delay
            self.restart_delay = min(self.restart_delay * 2, MAX_RESTART_DELAY_SECONDS)
            print(f"{self.name} exited with {exit_code}; restarting", flush=True)
            self.process = None
        if now >= self.restart_at:
            self.start(root)
            print(f"started {self.name} pid={self.process.pid if self.process else '?'}", flush=True)


def parse_site(value: str) -> tuple[str, int, str]:
    parts = value.split("=")
    if len(parts) not in {2, 3}:
        raise argparse.ArgumentTypeError("site must be SOURCE_SITE=PORT or SOURCE_SITE=PORT=RUNTIME")
    site, raw_port = parts[:2]
    runtime = parts[2] if len(parts) == 3 else "flask"
    if not SITE_RE.fullmatch(site):
        raise argparse.ArgumentTypeError("site name contains unsupported characters")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("site port must be an integer") from exc
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("site port is out of range")
    if runtime not in {"flask", "static"}:
        raise argparse.ArgumentTypeError("site runtime must be flask or static")
    return site, port, runtime


def build_children(root: Path, gateway_port: int, sites: list[tuple[str, int, str]], log_dir: Path) -> list[Child]:
    children: list[Child] = []
    for site, port, runtime in sites:
        if runtime == "static":
            command = [
                sys.executable,
                "scripts/static_site_server.py",
                "--root",
                f"sites/{site}",
                "--site-id",
                site,
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
            ]
        else:
            command = [
                sys.executable,
                "-m",
                "flask",
                "--app",
                f"sites/{site}/app.py",
                "run",
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
                "--no-debugger",
                "--no-reload",
            ]
        children.append(
            Child(
                site,
                command,
                log_dir / f"{site}.log",
            )
        )
    children.append(
        Child(
            "gateway",
            [sys.executable, "scripts/preview_gateway.py", "--host", "0.0.0.0", "--port", str(gateway_port)],
            log_dir / "gateway.log",
        )
    )
    return children


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--site", action="append", type=parse_site, required=True)
    parser.add_argument("--log-dir", type=Path, default=Path("/tmp/webharbor-gallery-supervisor"))
    parser.add_argument("--pid-file", type=Path, default=Path("/tmp/webharbor-gallery-supervisor.pid"))
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"root does not exist: {root}")
    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    args.pid_file.write_text(f"{os.getpid()}\n", encoding="ascii")
    children = build_children(root, args.gateway_port, args.site, args.log_dir)
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(f"WebHarbor gallery supervisor pid={os.getpid()} root={root}", flush=True)
    try:
        while not stopping:
            now = time.monotonic()
            for child in children:
                child.tick(root, now)
            time.sleep(0.5)
    finally:
        for child in reversed(children):
            child.stop()
        try:
            args.pid_file.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
