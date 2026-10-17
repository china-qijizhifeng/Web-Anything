#!/usr/bin/env python3
"""Deploy a multi-site WebHarbor preview gallery to one E2B sandbox.

The manifest is both the presentation catalog and the local packaging input.
Each entry names a workspace containing ``sites/<source_site>`` and the local
port that the gateway will proxy.  The gateway itself remains single-port;
adding a demo means adding one manifest entry and rerunning this command.

E2B is imported lazily so manifest validation and package creation can run in a
plain repository Python environment.  Secrets are read from the environment
and never written to the output JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import tarfile
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REMOTE_ROOT = "/tmp/webharbor-preview-gallery"
DEFAULT_GATEWAY_PORT = 8885
SITE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
EXCLUDED_DIRS = {"instance", "__pycache__", ".pytest_cache"}
E2B_ENV_NAMES = {"E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_e2b_env_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"E2B environment file is unavailable: {path}") from exc
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
        if name not in E2B_ENV_NAMES:
            continue
        try:
            values = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise SystemExit(f"invalid environment value at {path}:{number}") from exc
        if len(values) > 1:
            raise SystemExit(f"environment value must be one shell word at {path}:{number}")
        os.environ.setdefault(name, values[0] if values else "")


def require_e2b_env() -> None:
    missing = [name for name in ("E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN") if not os.getenv(name)]
    if missing:
        raise SystemExit(f"missing required E2B environment variables: {', '.join(missing)}")


def health_site_identity(payload: dict[str, Any]) -> Any:
    """Accept gallery v1 and replay-runtime health identity fields."""
    return payload.get("site") or payload.get("site_id")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"invalid gallery manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
        raise SystemExit("gallery manifest schema_version must be 1 or 2")
    sites = payload.get("sites")
    if not isinstance(sites, list) or not sites:
        raise SystemExit("gallery manifest must contain a non-empty sites list")
    seen_ports: set[int] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(sites):
        if not isinstance(item, dict):
            raise SystemExit(f"sites[{index}] must be an object")
        site_id = str(item.get("id") or "")
        if not SITE_ID_RE.fullmatch(site_id):
            raise SystemExit(f"sites[{index}].id must contain only letters, digits, and underscores")
        if site_id in seen_ids:
            raise SystemExit(f"duplicate site id: {site_id}")
        seen_ids.add(site_id)
        try:
            port = int(item["port"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"sites[{index}].port must be an integer") from exc
        if not (1 <= port <= 65535) or port == int(payload.get("gateway_port", DEFAULT_GATEWAY_PORT)):
            raise SystemExit(f"invalid or gateway-conflicting site port: {port}")
        if port in seen_ports:
            raise SystemExit(f"duplicate site port: {port}")
        seen_ports.add(port)
        workspace = item.get("source_workspace")
        if not workspace:
            raise SystemExit(f"sites[{index}].source_workspace is required")
        source_workspace = Path(str(workspace))
        if not source_workspace.is_absolute():
            source_workspace = ROOT / source_workspace
        source_workspace = source_workspace.resolve()
        source_site = str(item.get("source_site") or site_id)
        if not SITE_ID_RE.fullmatch(source_site):
            raise SystemExit(f"sites[{index}].source_site is invalid")
        source_dir = source_workspace / "sites" / source_site
        if not source_dir.is_dir():
            raise SystemExit(f"missing source directory for {site_id}: {source_dir}")
        runtime = str(item.get("runtime") or "flask")
        if runtime not in {"flask", "static"}:
            raise SystemExit(f"sites[{index}].runtime must be flask or static")
        required_entrypoint = source_dir / ("index.html" if runtime == "static" else "app.py")
        if not required_entrypoint.is_file():
            raise SystemExit(f"missing {runtime} entrypoint for {site_id}: {required_entrypoint}")
        replay_contract = source_dir / "synthetic_replay_contract.json"
        if replay_contract.is_file():
            try:
                replay_payload = json.loads(replay_contract.read_text(encoding="utf-8"))
                replay_routes = replay_payload.get("routes") or []
                browser_paths = [
                    str(route["path"])
                    for route in replay_routes
                    if isinstance(route, dict) and str(route.get("path") or "").startswith("/")
                ]
            except (OSError, ValueError, TypeError) as exc:
                raise SystemExit(f"invalid replay contract for {site_id}: {replay_contract}") from exc
            if not browser_paths:
                raise SystemExit(f"replay contract has no browser routes for {site_id}: {replay_contract}")
            item["_browser_paths"] = list(dict.fromkeys(browser_paths))
            task_path = (source_dir / str((replay_payload.get("tasks") or {}).get("path") or "tasks.jsonl")).resolve()
            try:
                task_path.relative_to(source_dir)
                replay_tasks = [
                    json.loads(line)
                    for line in task_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, ValueError, TypeError) as exc:
                raise SystemExit(f"invalid replay tasks for {site_id}: {task_path}") from exc
            login_task = next(
                (task for task in replay_tasks if task.get("workflow") == "login"),
                None,
            )
            # Authentication is optional. Some accepted mirrors intentionally
            # expose only public workflows, so they should still receive full
            # public-route verification without inventing a login task.
            if isinstance(login_task, dict):
                item["_browser_auth"] = {
                    "home_path": browser_paths[0],
                    "login_task": login_task,
                    "evaluator_path": str(replay_payload["evaluator"]["endpoint_template"]).replace(
                        "{evaluator_id}", str(login_task["evaluator_id"])
                    ),
                    "result_field": str(replay_payload["evaluator"]["result_field"]),
                    "reset_path": str(replay_payload["reset"]["endpoint"]),
                }
        item["runtime"] = runtime
        item["source_workspace"] = str(source_workspace)
        item["source_site"] = source_site
        expected_site = str(item.get("expected_site") or source_site)
        if not SITE_ID_RE.fullmatch(expected_site):
            raise SystemExit(f"sites[{index}].expected_site is invalid")
        item["expected_site"] = expected_site
        item.setdefault("name", site_id)
        item.setdefault("tier", "demo")
        item.setdefault("description", "")
    payload.setdefault("gateway_port", DEFAULT_GATEWAY_PORT)
    return payload


def _archive_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return None
    return info


def presentation_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Remove local filesystem fields before shipping the public catalog."""
    public_sites = []
    for item in manifest["sites"]:
        public_sites.append(
            {
                key: item[key]
                for key in ("port", "id", "name", "display_name", "tier", "description", "runtime")
                if key in item
            }
        )
    return {"schema_version": manifest.get("schema_version", 2), "sites": public_sites}


def build_package(
    output: Path,
    manifest: dict[str, Any],
    gateway: Path,
    supervisor: Path | None = None,
) -> None:
    if output.exists():
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for item in manifest["sites"]:
            source_workspace = Path(item["source_workspace"])
            source_site = item["source_site"]
            source_dir = source_workspace / "sites" / source_site
            archive.add(source_dir, arcname=f"sites/{source_site}", filter=_archive_filter)
        archive.add(gateway, arcname="scripts/preview_gateway.py")
        supervisor_path = supervisor or ROOT / "scripts" / "preview_gallery_supervisor.py"
        if supervisor_path.is_file():
            archive.add(supervisor_path, arcname="scripts/preview_gallery_supervisor.py")
        static_server = ROOT / "scripts" / "static_site_server.py"
        if static_server.is_file():
            archive.add(static_server, arcname="scripts/static_site_server.py")
        manifest_path = output.with_name(output.name + ".preview_manifest.json")
        manifest_path.write_text(
            json.dumps(presentation_manifest(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            archive.add(manifest_path, arcname="preview_manifest.json")
        finally:
            manifest_path.unlink(missing_ok=True)


def run_checked(sandbox: Any, command: str, *, timeout: float = 120) -> str:
    result = sandbox.commands.run(command, timeout=timeout, request_timeout=max(timeout + 30, 60))
    if result.exit_code != 0:
        raise RuntimeError(
            f"remote command failed with exit={result.exit_code}\n"
            f"command={command}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result.stdout or ""


def upload_archive(
    sandbox: Any,
    package_path: Path,
    remote_archive: str,
    *,
    request_timeout: int,
    chunk_bytes: int,
    concurrency: int,
) -> None:
    """Upload a large archive in bounded parallel chunks, then assemble it remotely."""

    if chunk_bytes <= 0:
        with package_path.open("rb") as stream:
            sandbox.files.write(remote_archive, stream, request_timeout=request_timeout)
        return
    if concurrency < 1:
        raise ValueError("upload concurrency must be positive")
    total_bytes = package_path.stat().st_size
    chunk_count = (total_bytes + chunk_bytes - 1) // chunk_bytes

    def send_chunk(index: int) -> None:
        offset = index * chunk_bytes
        length = min(chunk_bytes, total_bytes - offset)
        with package_path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(length)
        part_path = f"{remote_archive}.part-{index:05d}"
        sandbox.files.write(part_path, data, request_timeout=request_timeout)

    with ThreadPoolExecutor(max_workers=min(concurrency, chunk_count)) as pool:
        futures = [pool.submit(send_chunk, index) for index in range(chunk_count)]
        for future in as_completed(futures):
            future.result()
    part_prefix = shell_quote(remote_archive) + ".part-*"
    run_checked(
        sandbox,
        f"cat {part_prefix} > {shell_quote(remote_archive)} && rm -f {part_prefix}",
        timeout=300,
    )


def run_background(sandbox: Any, command: str) -> None:
    sandbox.commands.run(command, background=True, timeout=0, request_timeout=60)


def shell_quote(value: str) -> str:
    # All generated arguments are simple paths/IDs, but quote them for spaces
    # and to keep the remote command safe if a future manifest adds them.
    import shlex

    return shlex.quote(value)


def external_check_failures(result: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    """Return fail-closed public gateway verification failures."""
    failures: list[str] = []
    if result.get("external_error"):
        failures.append(f"public gateway check failed: {result['external_error']}")
    checks = result.get("external_checks")
    if not isinstance(checks, dict):
        return failures + ["public gateway checks are missing"]
    health = checks.get("gateway_health") or {}
    if health.get("status") != 200:
        failures.append("public gateway health did not return HTTP 200")
    gallery = checks.get("gallery") or {}
    if gallery.get("status") != 200:
        failures.append("public gallery did not return HTTP 200")
    if gallery.get("missing_entries"):
        failures.append(
            "public gallery is missing entries: " + ", ".join(gallery["missing_entries"])
        )
    for item in manifest["sites"]:
        check = checks.get(item["id"]) or {}
        if check.get("status") != 200:
            failures.append(f"{item['id']} public homepage did not return HTTP 200")
        if check.get("contains_expected") is not True:
            failures.append(f"{item['id']} public homepage lacks expected text")
        if item.get("_browser_paths") and check.get("browser", {}).get("passed") is not True:
            failures.append(f"{item['id']} public replay browser verification failed")
    return failures


def _is_benign_request_failure(resource_type: str, failure: str) -> bool:
    return resource_type == "image" and failure == "net::ERR_ABORTED"


def _is_asset_html_mismatch(resource_type: str, content_type: str) -> bool:
    """Reject HTML only when Chromium expected a concrete static asset."""
    asset_types = {"font", "image", "manifest", "media", "script", "stylesheet"}
    return resource_type in asset_types and "text/html" in content_type.lower()


def verify_public_replay_browser(
    base_url: str,
    paths: list[str],
    *,
    browser_path: str | None = None,
    auth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Exercise every replay route through the public E2B gateway."""
    from playwright.sync_api import sync_playwright

    allowed_host = urlsplit(base_url).netloc
    routes: list[dict[str, Any]] = []
    auth_result: dict[str, Any] | None = None
    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {"headless": True}
        if browser_path:
            launch_options["executable_path"] = browser_path
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            for path in paths:
                page = context.new_page()
                external: list[str] = []
                request_failures: list[str] = []
                http_errors: list[str] = []
                mime_errors: list[str] = []
                console_errors: list[str] = []

                def on_request(request: Any) -> None:
                    parsed = urlsplit(request.url)
                    if parsed.scheme in {"http", "https"} and parsed.netloc != allowed_host:
                        external.append(request.url)

                def on_response(response: Any) -> None:
                    request = response.request
                    if response.status >= 400:
                        http_errors.append(f"{response.status} {request.resource_type} {response.url}")
                    content_type = response.headers.get("content-type", "").lower()
                    if _is_asset_html_mismatch(request.resource_type, content_type):
                        mime_errors.append(
                            f"{request.resource_type} received {content_type}: {response.url}"
                        )

                def on_request_failed(request: Any) -> None:
                    failure = str(request.failure or "unknown failure")
                    # Chromium may start the fallback <img> in a <picture> and
                    # then cancel it after selecting a supported <source>.
                    # That image-only cancellation is not a transport failure.
                    if _is_benign_request_failure(request.resource_type, failure):
                        return
                    request_failures.append(
                        f"{request.resource_type} {request.url}: {failure}"
                    )

                page.on("request", on_request)
                page.on("requestfailed", on_request_failed)
                page.on("response", on_response)
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text) if message.type == "error" else None,
                )
                url = base_url.rstrip("/") + "/" + path.lstrip("/")
                status: int | None = None
                navigation_error: str | None = None
                try:
                    response = page.goto(url, wait_until="networkidle", timeout=30_000)
                    status = response.status if response else None
                except Exception as exc:
                    navigation_error = repr(exc)
                finally:
                    page.close()
                route_result = {
                    "path": path,
                    "url": url,
                    "status": status,
                    "external_requests": sorted(set(external)),
                    "request_failures": sorted(set(request_failures)),
                    "http_errors": sorted(set(http_errors)),
                    "mime_errors": sorted(set(mime_errors)),
                    "console_errors": sorted(set(console_errors)),
                    "navigation_error": navigation_error,
                }
                route_result["passed"] = (
                    status == 200
                    and navigation_error is None
                    and not any(
                        route_result[key]
                        for key in (
                            "external_requests",
                            "request_failures",
                            "http_errors",
                            "mime_errors",
                            "console_errors",
                        )
                    )
                )
                routes.append(route_result)
            if auth:
                page = context.new_page()
                auth_errors: list[str] = []
                task = auth["login_task"]
                try:
                    home_url = base_url.rstrip("/") + "/" + str(auth["home_path"]).lstrip("/")
                    login_url = base_url.rstrip("/") + "/" + str(task["start_url"]).lstrip("/")
                    page.goto(home_url, wait_until="networkidle", timeout=30_000)
                    entry = page.locator('[data-webanything-auth-entry]:visible')
                    if entry.count() == 0:
                        auth_errors.append("home route lacks a visible login entry")
                    else:
                        entry.first.click()
                        page.wait_for_load_state("networkidle")
                        if urlsplit(page.url).path != urlsplit(login_url).path:
                            auth_errors.append(
                                f"login entry ended at {urlsplit(page.url).path}, expected {urlsplit(login_url).path}"
                            )
                    if urlsplit(page.url).path != urlsplit(login_url).path:
                        page.goto(login_url, wait_until="networkidle", timeout=30_000)
                    for action in task["actions"]:
                        locator = page.locator(action["selector"]).first
                        kind = action["type"]
                        if kind == "click":
                            locator.click()
                        elif kind == "fill":
                            locator.fill(action["value"])
                        elif kind == "select":
                            locator.select_option(action["value"])
                        elif kind == "check":
                            locator.check()
                        elif kind == "uncheck":
                            locator.uncheck()
                        elif kind == "press":
                            locator.press(action["value"])
                    page.wait_for_load_state("networkidle")
                    if urlsplit(page.url).path != urlsplit(home_url).path:
                        auth_errors.append(
                            f"login ended at {urlsplit(page.url).path}, expected {urlsplit(home_url).path}"
                        )
                    if page.locator('[data-webanything-auth-state="authenticated"]:visible').count() == 0:
                        auth_errors.append("login did not expose visible authenticated user state")
                    evaluator_url = base_url.rstrip("/") + "/" + str(auth["evaluator_path"]).lstrip("/")
                    evaluation_response = context.request.get(evaluator_url)
                    evaluation = evaluation_response.json()
                    if evaluation.get(auth["result_field"]) is not True:
                        auth_errors.append("login evaluator returned a non-passing result")
                except Exception as exc:
                    auth_errors.append(f"{type(exc).__name__}: {exc}")
                finally:
                    try:
                        reset_url = base_url.rstrip("/") + "/" + str(auth["reset_path"]).lstrip("/")
                        context.request.post(reset_url)
                    except Exception as exc:
                        auth_errors.append(f"reset after login verification failed: {exc}")
                    page.close()
                auth_result = {"passed": not auth_errors, "errors": auth_errors}
            context.close()
        finally:
            browser.close()
    return {
        "passed": (
            bool(routes)
            and all(route["passed"] for route in routes)
            and (auth_result is None or auth_result["passed"])
        ),
        "routes": routes,
        "auth": auth_result,
    }


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    load_e2b_env_file(args.env_file)
    require_e2b_env()
    manifest = load_manifest(Path(args.manifest).resolve())
    gateway_port = int(args.gateway_port or manifest.get("gateway_port", DEFAULT_GATEWAY_PORT))
    remote_root = args.remote_root
    package_is_temporary = not args.package_path
    if args.package_path:
        package_path = Path(args.package_path)
    else:
        with tempfile.NamedTemporaryFile(
            prefix="webharbor-preview-gallery-", suffix=".tar.gz", delete=False
        ) as handle:
            package_path = Path(handle.name)
        package_path.unlink(missing_ok=True)
    build_package(package_path, manifest, ROOT / "scripts/preview_gateway.py")

    # Import only after local validation/package construction succeeds.
    from e2b import Sandbox, SandboxNotFoundException

    requested_sandbox_id = args.sandbox_id
    connected_existing = False
    replaced_missing_sandbox = False
    if requested_sandbox_id:
        try:
            sandbox = Sandbox.connect(requested_sandbox_id, timeout=args.timeout_seconds)
            connected_existing = True
        except SandboxNotFoundException:
            if not args.create_if_missing:
                raise
            replaced_missing_sandbox = True
            sandbox = Sandbox.create(
                template=args.template,
                timeout=args.timeout_seconds,
                secure=not args.public,
                lifecycle={"on_timeout": "pause", "auto_resume": True},
                metadata={"purpose": "webanything-preview-gallery"},
            )
    else:
        sandbox = Sandbox.create(
            template=args.template,
            timeout=args.timeout_seconds,
            secure=not args.public,
            lifecycle={"on_timeout": "pause", "auto_resume": True},
            metadata={"purpose": "webanything-preview-gallery"},
        )
    success = False
    try:
        gateway_host = sandbox.get_host(gateway_port)
        gateway_url = f"https://{gateway_host}/"
        remote_archive = f"{remote_root}/preview.tar.gz"
        supervisor_pid = remote_root + "/gallery-supervisor.pid"
        kill_commands = [
            f"if [ -s {shell_quote(supervisor_pid)} ]; then kill -TERM \"$(cat {shell_quote(supervisor_pid)})\" 2>/dev/null || true; fi",
            "sleep 1",
            f"if [ -s {shell_quote(supervisor_pid)} ]; then kill -KILL \"$(cat {shell_quote(supervisor_pid)})\" 2>/dev/null || true; fi",
            f"(pkill -TERM -f '[p]review_gateway.py --host 0.0.0.0 --port {gateway_port}' || true)",
            "(pkill -TERM -f '[f]lask --app sites/.*/app.py run --host 0.0.0.0 --port' || true)",
            "(pkill -TERM -f '[s]tatic_site_server.py --root sites/' || true)",
        ]
        run_checked(
            sandbox,
            f"mkdir -p {shell_quote(remote_root)} {shell_quote(remote_root + '/scripts')} && "
            + " && ".join(kill_commands)
            + " && sleep 1"
            + f" && (pkill -KILL -f '[p]review_gateway.py --host 0.0.0.0 --port {gateway_port}' || true)"
            + " && (pkill -KILL -f '[f]lask --app sites/.*/app.py run --host 0.0.0.0 --port' || true)"
            + " && (pkill -KILL -f '[s]tatic_site_server.py --root sites/' || true)"
            + f" && rm -rf {shell_quote(remote_root + '/sites')} {shell_quote(remote_root + '/scripts/preview_gateway.py')} {shell_quote(remote_root + '/preview_manifest.json')}",
            timeout=90,
        )
        upload_archive(
            sandbox,
            package_path,
            remote_archive,
            request_timeout=max(600, int(args.upload_timeout_seconds)),
            chunk_bytes=int(args.upload_chunk_bytes),
            concurrency=int(args.upload_concurrency),
        )
        python_entrypoints = [
            f"sites/{item['source_site']}/app.py"
            for item in manifest["sites"]
            if item["runtime"] == "flask"
        ]
        run_checked(
            sandbox,
            f"cd {shell_quote(remote_root)} && tar -xzf preview.tar.gz --overwrite && "
            "python3.12 -m py_compile scripts/preview_gateway.py scripts/preview_gallery_supervisor.py scripts/static_site_server.py"
            + (" " + " ".join(python_entrypoints) if python_entrypoints else ""),
            timeout=300,
        )
        site_args = " ".join(
            f"--site {shell_quote(str(item['source_site']) + '=' + str(int(item['port'])) + '=' + item['runtime'])}"
            for item in manifest["sites"]
        )
        run_background(
            sandbox,
            f"cd {shell_quote(remote_root)} && python3.12 scripts/preview_gallery_supervisor.py "
            f"--root {shell_quote(remote_root)} --gateway-port {gateway_port} {site_args} "
            f"--log-dir /tmp/webharbor-gallery-supervisor --pid-file {shell_quote(supervisor_pid)} "
            f">/tmp/webharbor-gallery-supervisor.log 2>&1",
        )
        checks = [{"url": f"http://127.0.0.1:{gateway_port}/__health", "site": None}] + [
            {
                "url": f"http://127.0.0.1:{int(item['port'])}/_health",
                "site": item["expected_site"],
            }
            for item in manifest["sites"]
        ]
        checks_literal = json.dumps(checks)
        run_checked(
            sandbox,
            "python3.12 - <<'PY'\n"
            "import json, time, urllib.request\n"
            f"checks = json.loads({checks_literal!r})\n"
            "deadline = time.time() + 120\nlast = None\n"
            "while time.time() < deadline:\n"
            "    try:\n"
            "        for check in checks:\n"
            "            url = check['url']\n"
            "            with urllib.request.urlopen(url, timeout=5) as resp:\n"
            "                if resp.status >= 400: raise RuntimeError(f'{url} -> {resp.status}')\n"
            "                payload = __import__('json').loads(resp.read())\n"
            "            identity = payload.get('site') or payload.get('site_id')\n"
            "            if check['site'] and identity != check['site']:\n"
            "                raise RuntimeError(f\"{url} site identity {identity!r} != {check['site']!r}\")\n"
            "        print('gallery-preview-ready')\n"
            "        raise SystemExit(0)\n"
            "    except Exception as exc:\n"
            "        last = exc\n"
            "        time.sleep(2)\n"
            "raise SystemExit(f'gallery preview not ready: {last}')\n"
            "PY",
            timeout=160,
        )

        external_checks: dict[str, Any] = {}
        external_error: str | None = None
        try:
            with urllib.request.urlopen(gateway_url + "__health", timeout=30) as response:
                external_checks["gateway_health"] = {"status": response.status, "url": gateway_url + "__health"}
            with urllib.request.urlopen(gateway_url, timeout=30) as response:
                gallery_body = response.read().decode("utf-8", errors="replace")
                missing_gallery_entries = [
                    item["id"]
                    for item in manifest["sites"]
                    if item["id"] not in gallery_body or f"/p/{int(item['port'])}/" not in gallery_body
                ]
                external_checks["gallery"] = {
                    "status": response.status,
                    "url": gateway_url,
                    "missing_entries": missing_gallery_entries,
                }
                if response.status != 200 or missing_gallery_entries:
                    raise RuntimeError(f"gallery index missing entries: {missing_gallery_entries}")
            for item in manifest["sites"]:
                url = gateway_url + f"p/{int(item['port'])}/"
                with urllib.request.urlopen(url, timeout=45) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    expected = str(item.get("expected_text") or "")
                    external_checks[item["id"]] = {
                        "status": response.status,
                        "url": url,
                        "contains_expected": (not expected or expected in body),
                    }
                    if response.status != 200 or (expected and expected not in body):
                        raise RuntimeError(
                            f"{item['id']} public homepage verification failed"
                        )
                if item.get("_browser_paths"):
                    external_checks[item["id"]]["browser"] = verify_public_replay_browser(
                        url,
                        item["_browser_paths"],
                        browser_path=args.browser_path,
                        auth=item.get("_browser_auth"),
                    )
                    if external_checks[item["id"]]["browser"]["passed"] is not True:
                        raise RuntimeError(f"{item['id']} public replay browser verification failed")
        except Exception as exc:  # Keep a live sandbox if local DNS blocks public egress.
            external_error = repr(exc)

        result: dict[str, Any] = {
            "created_at": utc_now(),
            "sandbox_id": sandbox.sandbox_id,
            "requested_sandbox_id": requested_sandbox_id,
            "replaced_missing_sandbox": replaced_missing_sandbox,
            "template": args.template,
            "secure": not args.public,
            "lifecycle": {"on_timeout": "pause", "auto_resume": True},
            "gateway_port": gateway_port,
            "gateway_url": gateway_url,
            "sites": [
                {
                    "id": item["id"],
                    "source_site": item["source_site"],
                    "runtime": item["runtime"],
                    "port": int(item["port"]),
                    "url": gateway_url + f"p/{int(item['port'])}/",
                }
                for item in manifest["sites"]
            ],
            "package": {"path": str(package_path), "bytes": package_path.stat().st_size},
            "external_checks": external_checks,
            "external_error": external_error,
            "remote_logs": {
                "supervisor": "/tmp/webharbor-gallery-supervisor.log",
                "gateway": "/tmp/webharbor-gallery-supervisor/gateway.log",
            },
            "supervision": {
                "enabled": True,
                "pid_file": supervisor_pid,
                "log_dir": "/tmp/webharbor-gallery-supervisor",
            },
            "note": "Do not kill this sandbox until the preview is no longer needed.",
        }
        result["verification_failures"] = external_check_failures(result, manifest)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if result["verification_failures"] and not args.allow_external_check_failure:
            raise RuntimeError(
                "E2B public verification failed: "
                + "; ".join(result["verification_failures"])
            )
        success = True
        return result
    finally:
        if not success and not connected_existing:
            sandbox.kill()
        if package_is_temporary:
            package_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Gallery manifest with source_workspace for each site.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(os.environ["WEBANYTHING_E2B_ENV_FILE"]) if os.environ.get("WEBANYTHING_E2B_ENV_FILE") else None,
        help="Environment file containing only the E2B settings used by this command.",
    )
    parser.add_argument("--template", required=True, help="E2B template with Python; Flask is required only for Flask sites.")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument(
        "--upload-timeout-seconds",
        type=int,
        default=1800,
        help="Timeout for uploading the preview archive to E2B; large galleries may need more than 600 seconds.",
    )
    parser.add_argument(
        "--upload-chunk-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="Upload archives in chunks of this size; set to 0 to use one request.",
    )
    parser.add_argument(
        "--upload-concurrency",
        type=int,
        default=4,
        help="Maximum number of archive chunks uploaded concurrently.",
    )
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--gateway-port", type=int)
    parser.add_argument(
        "--browser-path",
        default=os.environ.get("WEBANYTHING_BROWSER_PATH"),
        help="Optional Chromium executable used for public replay route verification.",
    )
    parser.add_argument("--package-path")
    parser.add_argument("--sandbox-id", help="Reuse an existing E2B sandbox.")
    parser.add_argument(
        "--create-if-missing",
        action="store_true",
        help="Create a replacement sandbox when --sandbox-id no longer exists.",
    )
    parser.add_argument("--output", default="runs/deployment.json")
    parser.add_argument("--public", action="store_true", help="Create a public E2B traffic endpoint.")
    parser.add_argument(
        "--allow-external-check-failure",
        action="store_true",
        help="Diagnostic only: return success when the public URL cannot be verified.",
    )
    deploy(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
