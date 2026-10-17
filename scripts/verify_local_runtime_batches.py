#!/usr/bin/env python3
"""Verify catalog sites through the local control plane in bounded batches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.mirror_factory import (  # noqa: E402
    DEFAULT_EVIDENCE_ROOT,
    DEFAULT_MANIFEST,
    load_targets,
    verify_local_runtime_target,
)
from local_http import request_loopback_url  # noqa: E402
from site_catalog import load_site_catalog  # noqa: E402


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def port_is_available(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def request_json(url: str, method: str = "GET", timeout: float = 45.0) -> tuple[int, dict]:
    status, body = request_loopback_url(url, method=method, timeout=timeout)
    return status, json.loads(body)


def wait_for_control(control_url: str, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"control plane exited with status {process.returncode}")
        try:
            request_json(f"{control_url}/health", timeout=2)
            return
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
    raise RuntimeError("control plane did not become ready")


def dirty_database(site_id: str) -> tuple[str, str]:
    seed = ROOT / "sites" / site_id / "instance_seed" / f"{site_id}.db"
    runtime = ROOT / "sites" / site_id / "instance" / f"{site_id}.db"
    clean_hash = sha256_file(seed)
    with sqlite3.connect(runtime, timeout=30) as connection:
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute(f"PRAGMA user_version = {current + 1}")
        connection.commit()
    dirty_hash = sha256_file(runtime)
    if dirty_hash == clean_hash:
        raise RuntimeError(f"{site_id}: SQLite mutation did not change runtime bytes")
    return clean_hash, dirty_hash


def stop_process_group(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def stop_matching_stale_runner(site_id: str, port: int) -> bool:
    """Stop an exact stale WebAnything runtime match on Linux."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    runtime = str(ROOT / "src/webanything/runtime.py")
    matched_pid = None
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = [
                value.decode("utf-8", errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if runtime in command and "serve" in command and site_id in command and str(port) in command:
            matched_pid = int(entry.name)
            break
    if matched_pid is None:
        return False
    try:
        os.kill(matched_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not port_is_available(port):
        time.sleep(0.05)
    if not port_is_available(port):
        try:
            os.kill(matched_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return port_is_available(port)


def stop_batch(control: subprocess.Popen | None) -> None:
    if control is not None and control.poll() is None:
        stop_process_group(control.pid)
        try:
            control.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def verify_batch(
    site_ids: list[str],
    targets: dict[str, dict],
    dependency_path: Path | None,
    startup_timeout: float,
    take_over_stale: bool,
) -> list[dict]:
    if dependency_path:
        dependency_path = dependency_path.expanduser().resolve()
    catalog = load_site_catalog()
    catalog_by_id = {site["id"]: site for site in catalog["sites"]}
    occupied = [
        site_id
        for site_id in site_ids
        if not port_is_available(catalog_by_id[site_id]["port"])
    ]
    if occupied and take_over_stale:
        for site_id in occupied:
            stop_matching_stale_runner(site_id, catalog_by_id[site_id]["port"])
        occupied = [
            site_id
            for site_id in site_ids
            if not port_is_available(catalog_by_id[site_id]["port"])
        ]
    if occupied:
        raise RuntimeError(f"catalog ports already in use: {', '.join(occupied)}")

    control_port = available_port()
    control_url = f"http://127.0.0.1:{control_port}"
    env = os.environ.copy()
    if dependency_path:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(dependency_path) + (os.pathsep + existing if existing else "")

    control = None
    results = []
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log:
        try:
            control = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "src/webanything/runtime.py"),
                    "control",
                    "--port", str(control_port),
                    "--sites", ",".join(site_ids),
                    "--sites-root", str(ROOT / "sites"),
                    "--catalog", str(ROOT / "site_catalog.json"),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            wait_for_control(control_url, control, startup_timeout)
            for site_id in site_ids:
                status = 503
                payload = {}
                for attempt in range(2):
                    status, payload = request_json(
                        f"{control_url}/reset/{site_id}", method="POST"
                    )
                    if status == 200 and payload.get("ready") is True:
                        break
                    if attempt == 0:
                        time.sleep(0.5)
                if status != 200 or payload.get("ready") is not True:
                    raise RuntimeError(
                        f"{site_id}: initial reset failed with HTTP {status}: {payload}"
                    )
            dirty_hashes = {site_id: dirty_database(site_id) for site_id in site_ids}
            for site_id in site_ids:
                result = verify_local_runtime_target(
                    targets[site_id],
                    DEFAULT_MANIFEST,
                    DEFAULT_EVIDENCE_ROOT,
                    control_url=control_url,
                    require_dirty=True,
                    mutation_evidence="SQLite PRAGMA user_version increment",
                    write=True,
                )
                clean_hash, dirty_hash = dirty_hashes[site_id]
                results.append(
                    {
                        "site": site_id,
                        "port": result["port"],
                        "http": 200,
                        "health_alive": result["checks"]["site_alive"],
                        "dirty_sha256": dirty_hash,
                        "seed_sha256": clean_hash,
                        "reset_sha256": result["after"]["runtime_db_sha256"],
                        "reset_matches_seed": result["checks"][
                            "runtime_matches_seed_after_reset"
                        ],
                    }
                )
        except Exception as exc:
            log.seek(0)
            output = log.read().strip()
            if output:
                raise RuntimeError(f"{exc}\ncontrol log:\n{output[-12000:]}") from exc
            raise
        finally:
            stop_batch(control)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sites", nargs="*", help="target ids; defaults to all manifest targets")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--dependency-path", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=15.0)
    parser.add_argument("--start-at", help="resume at this selected target id")
    parser.add_argument(
        "--take-over-stale",
        action="store_true",
        help="stop only exact WebAnything runtime command/port matches before a batch",
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    target_rows = load_targets(DEFAULT_MANIFEST)
    targets = {target["id"]: target for target in target_rows}
    selected = args.sites or [target["id"] for target in target_rows]
    unknown = sorted(set(selected) - set(targets))
    if unknown:
        parser.error(f"unknown target ids: {', '.join(unknown)}")
    if args.start_at:
        if args.start_at not in selected:
            parser.error(f"--start-at is not selected: {args.start_at}")
        selected = selected[selected.index(args.start_at) :]

    all_results = []
    for offset in range(0, len(selected), args.batch_size):
        batch = selected[offset : offset + args.batch_size]
        print(
            f"[runtime-local] batch {offset // args.batch_size + 1}: "
            f"{', '.join(batch)}",
            flush=True,
        )
        results = verify_batch(
            batch,
            targets,
            args.dependency_path,
            args.startup_timeout,
            args.take_over_stale,
        )
        all_results.extend(results)
        for result in results:
            print(
                f"{result['site']}\tHTTP {result['http']}\talive="
                f"{str(result['health_alive']).lower()}\tdirty=true\treset_hash_equal="
                f"{str(result['reset_matches_seed']).lower()}",
                flush=True,
            )

    print(
        json.dumps(
            {
                "sites": len(all_results),
                "http_200": sum(result["http"] == 200 for result in all_results),
                "health_alive": sum(result["health_alive"] for result in all_results),
                "dirty_before_reset": len(all_results),
                "reset_hash_equal": sum(
                    result["reset_matches_seed"] for result in all_results
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
