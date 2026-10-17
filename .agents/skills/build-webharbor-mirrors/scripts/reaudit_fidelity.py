#!/usr/bin/env python3
"""Re-run the current fidelity contract for reconstructed queue sites."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fidelity_contract import (
    FIDELITY_CONTRACT_VERSION,
    TOOL_VERSION as FIDELITY_TOOL_VERSION,
    fidelity_evidence_issues,
    fidelity_evidence_state,
)


TOOL_VERSION = "webharbor-fidelity-reaudit-v1"
QUEUE_RELATIVE = Path("mirror_factory/evidence/generated-replacement/queue.json")
REPORT_RELATIVE = Path("mirror_factory/evidence/fidelity-v19-reaudit.json")
ELIGIBLE_QUEUE_STATES = {"passed", "stale_fidelity", "fidelity_failed"}


class ReauditError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_catalog_ports(root: Path) -> dict[str, int]:
    catalog = json.loads((root / "site_catalog.json").read_text(encoding="utf-8"))
    base_port = catalog.get("base_port")
    sites = catalog.get("sites")
    if not isinstance(base_port, int) or not isinstance(sites, list):
        raise ReauditError("invalid site catalog")
    return {record["id"]: base_port + index for index, record in enumerate(sites)}


def select_sites(root: Path, requested: list[str]) -> list[str]:
    queue = json.loads((root / QUEUE_RELATIVE).read_text(encoding="utf-8"))
    entries = queue.get("entries")
    if not isinstance(entries, list):
        raise ReauditError("replacement queue has no entries")
    eligible = [
        entry["site"] for entry in entries
        if entry.get("status") in ELIGIBLE_QUEUE_STATES
    ]
    if not requested:
        return eligible
    unknown = sorted(set(requested) - {entry.get("site") for entry in entries})
    if unknown:
        raise ReauditError("unknown queue sites: " + ", ".join(unknown))
    ineligible = sorted(set(requested) - set(eligible))
    if ineligible:
        raise ReauditError(
            "sites are not reconstructed queue entries: " + ", ".join(ineligible)
        )
    requested_set = set(requested)
    return [site for site in eligible if site in requested_set]


def spec_uses_port(path: Path, site: str, port: int) -> None:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        routes = spec["routes"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReauditError(f"{site}: invalid fidelity spec: {exc}") from exc
    if spec.get("site") != site or not isinstance(routes, list) or not routes:
        raise ReauditError(f"{site}: fidelity spec has the wrong site or no routes")
    expected = f"127.0.0.1:{port}"
    mismatches = [
        route.get("local_url") for route in routes
        if not isinstance(route, dict)
        or not isinstance(route.get("local_url"), str)
        or expected not in route["local_url"]
    ]
    if mismatches:
        raise ReauditError(
            f"{site}: fidelity local URLs do not use stable catalog port {port}"
        )


def loopback_health(port: int) -> str | None:
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
        connection.request("GET", "/_health")
        response = connection.getresponse()
        value = json.loads(response.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()
    return value.get("site") if response.status == 200 and isinstance(value, dict) else None


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_site(site: str, port: int, process: subprocess.Popen[Any], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ReauditError(f"{site}: site runner exited {process.returncode}")
        if loopback_health(port) == site:
            return
        time.sleep(0.15)
    raise ReauditError(f"{site}: site runner did not become healthy")


def stop_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def evidence_result(root: Path, site: str, duration: float, output: str, returncode: int) -> dict[str, Any]:
    path = root / "mirror_factory" / "evidence" / site / "fidelity.json"
    if returncode not in {0, 2}:
        return {
            "site": site,
            "status": "error",
            "duration_seconds": round(duration, 3),
            "returncode": returncode,
            "error": "fidelity verifier did not reach a contract result",
            "output": output[-4000:],
        }
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "site": site,
            "status": "error",
            "duration_seconds": round(duration, 3),
            "returncode": returncode,
            "error": f"fidelity evidence is unreadable: {exc}",
            "output": output[-4000:],
        }
    state = fidelity_evidence_state(evidence, root=root)
    if (returncode == 0) != (state == "passed_current"):
        state = "stale"
    return {
        "site": site,
        "status": {
            "passed_current": "passed",
            "failed_current": "failed",
            "stale": "error",
        }[state],
        "evidence_state": state,
        "duration_seconds": round(duration, 3),
        "returncode": returncode,
        "comparisons": len(evidence.get("results", [])),
        "failure_count": len(evidence.get("failures", [])),
        "failures": evidence.get("failures", [])[:50],
        "contract_issues": fidelity_evidence_issues(evidence, root=root)[:50],
        "evidence": str(path.relative_to(root)),
        "tool": evidence.get("tool"),
        "fidelity_contract_version": evidence.get("fidelity_contract_version"),
        "output": output[-4000:],
    }


def run_site(
    root: Path,
    site: str,
    port: int,
    *,
    runtime_python: str,
    skill_python: str,
    timeout: float,
    env: dict[str, str],
) -> dict[str, Any]:
    started = time.monotonic()
    spec_path = root / "mirror_factory" / "fidelity_specs" / f"{site}.json"
    process: subprocess.Popen[Any] | None = None
    server_log_path = root / ".assets" / "fidelity-reaudit" / "server-logs" / f"{site}.log"
    verifier_log_path = root / ".assets" / "fidelity-reaudit" / "verifier-logs" / f"{site}.log"
    server_log_path.parent.mkdir(parents=True, exist_ok=True)
    verifier_log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        spec_uses_port(spec_path, site, port)
        running_site = loopback_health(port)
        if running_site is None:
            if not port_available(port):
                raise ReauditError(f"{site}: port {port} is occupied by an unknown service")
            server_log = server_log_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                [
                    runtime_python,
                    str(root / "src/webanything/runtime.py"),
                    "serve",
                    "--site", site,
                    "--port", str(port),
                    "--host", "127.0.0.1",
                    "--sites-root", str(root / "sites"),
                ],
                cwd=root,
                env=env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            server_log.close()
            wait_site(site, port, process)
        elif running_site != site:
            raise ReauditError(
                f"{site}: port {port} serves {running_site!r}, not the requested site"
            )
        command = [
            skill_python,
            str(Path(__file__).with_name("verify_fidelity.py")),
            str(spec_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            output = completed.stdout or ""
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            output += f"\nfidelity verifier timed out after {timeout:g} seconds\n"
            returncode = 124
        verifier_log_path.write_text(output, encoding="utf-8")
        return evidence_result(
            root, site, time.monotonic() - started, output, returncode
        )
    except Exception as exc:
        return {
            "site": site,
            "status": "error",
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": str(exc),
            "server_log": str(server_log_path.relative_to(root)),
            "verifier_log": str(verifier_log_path.relative_to(root)),
        }
    finally:
        if process is not None:
            stop_process_group(process)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("sites", nargs="*", help="optional reconstructed queue site IDs")
    value.add_argument("--jobs", type=int, default=2)
    value.add_argument("--timeout", type=float, default=180.0, help="per-site verifier timeout")
    value.add_argument("--resume", action="store_true", help="skip sites with a current passing proof")
    value.add_argument("--output", type=Path, default=REPORT_RELATIVE)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.jobs < 1 or args.timeout <= 0:
        raise ReauditError("jobs and timeout must be positive")
    root = repository_root()
    sites = select_sites(root, args.sites)
    ports = load_catalog_ports(root)
    missing = sorted(set(sites) - set(ports))
    if missing:
        raise ReauditError("queue sites are absent from the catalog: " + ", ".join(missing))
    if args.resume:
        selected = []
        for site in sites:
            path = root / "mirror_factory" / "evidence" / site / "fidelity.json"
            try:
                evidence = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                evidence = None
            if fidelity_evidence_state(evidence, root=root) != "passed_current":
                selected.append(site)
        sites = selected
    output_path = args.output if args.output.is_absolute() else root / args.output
    report = {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "phase": "fidelity_reaudit",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "status": "running",
        "fidelity_contract": {
            "tool": FIDELITY_TOOL_VERSION,
            "version": FIDELITY_CONTRACT_VERSION,
        },
        "selection": sites,
        "jobs": args.jobs,
        "timeout_seconds": args.timeout,
        "results": [],
        "summary": {},
    }
    atomic_json(output_path, report)
    env = os.environ.copy()
    runtime_python = env.get("WEBHARBOR_RUNTIME_PYTHON", sys.executable)
    skill_python = env.get("WEBHARBOR_SKILL_PYTHON", sys.executable)
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(
                run_site,
                root,
                site,
                ports[site],
                runtime_python=runtime_python,
                skill_python=skill_python,
                timeout=args.timeout,
                env=env,
            ): site
            for site in sites
        }
        completed_count = 0
        for future in as_completed(futures):
            result = future.result()
            completed_count += 1
            report["results"].append(result)
            counts: dict[str, int] = {}
            for item in report["results"]:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
            report["summary"] = counts
            report["updated_at"] = utc_now()
            atomic_json(output_path, report)
            print(
                f"[fidelity {completed_count}/{len(sites)}] {result['site']} "
                f"{result['status']} failures={result.get('failure_count', '-')} "
                f"seconds={result['duration_seconds']}",
                flush=True,
            )
    report["results"].sort(key=lambda item: sites.index(item["site"]))
    report["status"] = (
        "passed" if report["summary"].get("passed") == len(sites)
        else "completed_with_failures"
    )
    report["completed_at"] = utc_now()
    atomic_json(output_path, report)
    print(json.dumps({
        "status": report["status"],
        "sites": len(sites),
        "summary": report["summary"],
        "report": str(output_path.relative_to(root)),
    }, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReauditError as exc:
        print(f"reaudit-fidelity: {exc}", file=sys.stderr)
        raise SystemExit(3)
