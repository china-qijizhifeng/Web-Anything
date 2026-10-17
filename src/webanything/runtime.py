"""Small, deterministic runtime for WebAnything mirror sites.

The ``serve`` command runs one generated Flask application.  The ``control``
command is used by batch verification to start, reset, and inspect a selected
set of sites.  Keeping both paths here avoids a shell bootstrap and a second
supervisor process per site.
"""

from __future__ import annotations

import argparse
import atexit
import http.client
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_CONTROL_PORT = 8101


class RuntimeErrorWithContext(RuntimeError):
    """A runtime error whose message is safe to show directly to a user."""


class _ShutdownRequested(BaseException):
    pass


def _default_root() -> Path:
    configured = os.environ.get("WEBANYTHING_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    source = Path(__file__).resolve()
    if source.parent.name == "webanything" and source.parent.parent.name == "src":
        return source.parents[2]
    return Path.cwd().resolve()


def _sites_root(value: str | Path | None, root: Path) -> Path:
    configured = value or os.environ.get("WEBANYTHING_SITES_ROOT")
    return Path(configured).expanduser().resolve() if configured else root / "sites"


def _load_site_app(site_dir: Path) -> Any:
    app_path = site_dir / "app.py"
    if not app_path.is_file():
        raise RuntimeErrorWithContext(f"site app not found: {app_path}")

    module_name = f"webanything_site_{site_dir.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    if spec is None or spec.loader is None:
        raise RuntimeErrorWithContext(f"cannot load site app: {app_path}")

    os.chdir(site_dir)
    sys.path.insert(0, str(site_dir))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    app = getattr(module, "app", None)
    if app is None:
        raise RuntimeErrorWithContext(f"{app_path} must export a Flask app named 'app'")
    return app


def serve_site(site: str, host: str, port: int, sites_root: Path) -> int:
    from werkzeug.serving import make_server

    app = _load_site_app(sites_root / site)
    server = make_server(host, port, app, threaded=True)

    def request_shutdown(_signum: int, _frame: object) -> None:
        raise _ShutdownRequested()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_shutdown)

    print(f"WebAnything serving {site} at http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except _ShutdownRequested:
        pass
    finally:
        server.server_close()
    return 0


def _read_catalog(path: Path, selection: str | None) -> list[dict[str, Any]]:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
        base_port = int(catalog["base_port"])
        records = catalog["sites"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeErrorWithContext(f"invalid site catalog {path}: {exc}") from exc

    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise RuntimeErrorWithContext(f"invalid site entry at index {index}")
        normalized.append({**record, "port": base_port + index})

    if not selection:
        return normalized
    requested = [value.strip() for value in selection.split(",") if value.strip()]
    if not requested or len(requested) != len(set(requested)):
        raise RuntimeErrorWithContext("site selection must contain unique site ids")
    by_id = {record["id"]: record for record in normalized}
    unknown = [site for site in requested if site not in by_id]
    if unknown:
        raise RuntimeErrorWithContext(f"unknown site ids: {', '.join(unknown)}")
    return [by_id[site] for site in requested]


class SiteManager:
    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        sites_root: Path,
        host: str,
        runtime_path: Path,
    ) -> None:
        self.records = {record["id"]: record for record in records}
        self.sites_root = sites_root
        self.host = host
        self.runtime_path = runtime_path
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: dict[str, Any] = {}
        self.locks = {site: threading.Lock() for site in self.records}

    def _record(self, site: str) -> dict[str, Any]:
        try:
            return self.records[site]
        except KeyError as exc:
            raise RuntimeErrorWithContext(f"unknown site: {site}") from exc

    def _stop_unlocked(self, site: str) -> None:
        process = self.processes.pop(site, None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log = self.logs.pop(site, None)
        if log is not None:
            log.close()

    def stop(self, site: str) -> None:
        with self.locks[site]:
            self._stop_unlocked(site)

    def stop_all(self) -> None:
        for site in list(self.records):
            self.stop(site)

    def restore(self, site: str) -> None:
        site_dir = self.sites_root / site
        seed = site_dir / "instance_seed"
        runtime = site_dir / "instance"
        if not seed.is_dir():
            raise RuntimeErrorWithContext(f"seed directory not found: {seed}")
        shutil.rmtree(runtime, ignore_errors=True)
        shutil.copytree(seed, runtime)

    def _start_unlocked(self, site: str) -> subprocess.Popen[bytes]:
        record = self._record(site)
        log_path = Path(os.environ.get("WEBANYTHING_LOG_DIR", "/tmp")) / f"webanything-{site}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab", buffering=0)
        process = subprocess.Popen(
            [
                sys.executable,
                str(self.runtime_path),
                "serve",
                "--site",
                site,
                "--port",
                str(record["port"]),
                "--host",
                self.host,
                "--sites-root",
                str(self.sites_root),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.processes[site] = process
        self.logs[site] = log
        return process

    def wait_ready(self, site: str, timeout: float = 30.0) -> bool:
        record = self._record(site)
        process = self.processes.get(site)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process is None or process.poll() is not None:
                return False
            connection: http.client.HTTPConnection | None = None
            try:
                connection = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=2)
                connection.request("GET", "/")
                response = connection.getresponse()
                ready = response.status < 500
                response.read(1)
                if ready:
                    return True
            except (OSError, http.client.HTTPException):
                time.sleep(0.1)
            finally:
                if connection is not None:
                    connection.close()
        return False

    def restart(self, site: str, *, reset: bool) -> dict[str, Any]:
        with self.locks[site]:
            self._stop_unlocked(site)
            if reset:
                self.restore(site)
            process = self._start_unlocked(site)
            ready = self.wait_ready(site)
            pid = process.pid
            if not ready:
                self._stop_unlocked(site)
            return {"site": site, "pid": pid, "ready": ready}

    def health(self) -> tuple[dict[str, Any], bool]:
        sites: dict[str, Any] = {}
        all_ready = True
        for site, record in self.records.items():
            process = self.processes.get(site)
            alive = process is not None and process.poll() is None
            sites[site] = {
                "pid": process.pid if alive else None,
                "alive": alive,
                "port": record["port"],
            }
            all_ready = all_ready and alive
        return sites, all_ready


def create_control_app(manager: SiteManager) -> Any:
    from flask import Flask, jsonify

    app = Flask("webanything-runtime")

    @app.get("/health")
    def health() -> Any:
        sites, ok = manager.health()
        return jsonify({"ok": ok, "sites": sites}), 200 if ok else 503

    @app.post("/reset/<site>")
    def reset(site: str) -> Any:
        if site not in manager.records:
            return jsonify({"error": f"unknown site: {site}", "valid_sites": list(manager.records)}), 404
        result = manager.restart(site, reset=True)
        return jsonify(result), 200 if result["ready"] else 503

    @app.post("/restart/<site>")
    def restart(site: str) -> Any:
        if site not in manager.records:
            return jsonify({"error": f"unknown site: {site}", "valid_sites": list(manager.records)}), 404
        result = manager.restart(site, reset=False)
        return jsonify(result), 200 if result["ready"] else 503

    @app.post("/reset-all")
    def reset_all() -> Any:
        workers = max(1, len(manager.records))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda site: manager.restart(site, reset=True), manager.records))
        sites = {result["site"]: result for result in results}
        ok = all(result["ready"] for result in results)
        return jsonify({"ok": ok, "sites": sites}), 200 if ok else 503

    return app


def run_control(args: argparse.Namespace) -> int:
    records = _read_catalog(args.catalog.resolve(), args.sites)
    manager = SiteManager(
        records,
        sites_root=args.sites_root.resolve(),
        host=args.site_host,
        runtime_path=Path(__file__).resolve(),
    )
    atexit.register(manager.stop_all)
    if args.start:
        failures = [
            result["site"]
            for result in (manager.restart(record["id"], reset=True) for record in records)
            if not result["ready"]
        ]
        if failures:
            raise RuntimeErrorWithContext(f"sites failed to start: {', '.join(failures)}")
    app = create_control_app(manager)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = _default_root()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run one mirror site")
    serve.add_argument("--site", default=os.environ.get("WEBANYTHING_SITE"))
    serve.add_argument("--host", default=os.environ.get("WEBANYTHING_HOST", DEFAULT_HOST))
    serve.add_argument("--port", type=int, default=int(os.environ.get("WEBANYTHING_PORT", DEFAULT_PORT)))
    serve.add_argument("--sites-root", type=Path, default=_sites_root(None, root))

    control = commands.add_parser("control", help="run the batch reset and health API")
    control.add_argument("--catalog", type=Path, default=root / "site_catalog.json")
    control.add_argument("--sites-root", type=Path, default=_sites_root(None, root))
    control.add_argument("--sites", default=os.environ.get("WEBANYTHING_SITES"))
    control.add_argument("--host", default="127.0.0.1")
    control.add_argument("--port", type=int, default=DEFAULT_CONTROL_PORT)
    control.add_argument("--site-host", default=os.environ.get("WEBANYTHING_HOST", "127.0.0.1"))
    control.add_argument("--start", action="store_true", help="reset and start sites before serving the API")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "serve":
        if not args.site:
            raise SystemExit("--site or WEBANYTHING_SITE is required")
        return serve_site(args.site, args.host, args.port, args.sites_root.resolve())
    if args.command == "control":
        return run_control(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeErrorWithContext as exc:
        raise SystemExit(f"error: {exc}") from exc
