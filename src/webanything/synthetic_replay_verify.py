"""Independent read-only verifier for full-capture synthetic replay sites."""

from __future__ import annotations

import json
import os
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .release import ReleaseError, atomic_json, load_candidate_freeze, sha256_file, tree_identity
from .synthetic_replay import (
    SyntheticReplayContractError,
    contract_artifact_paths,
    load_synthetic_replay_contract,
)


class SyntheticReplayVerificationError(RuntimeError):
    """The replay verifier could not execute."""


AUTH_ENTRY_SELECTOR = '[data-webanything-auth-entry]'
AUTHENTICATED_SELECTOR = '[data-webanything-auth-state="authenticated"]'
LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _captured_target_kind(path: str) -> str:
    value = urlsplit(path).path.casefold()
    if value in {"", "/"}:
        return "home"
    if any(token in value for token in ("/video/", "/watch", "/play/")):
        return "detail"
    if "search" in value:
        return "search"
    if any(token in value for token in ("favorite", "favlist", "collection", "bookmark")):
        return "favorites"
    if "history" in value:
        return "history"
    if any(token in value for token in ("profile", "account", "member", "space/")):
        return "profile"
    return "browse"


def _captured_dom_requirements(contract_path: Path, contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    requirements: dict[str, dict[str, Any]] = {}
    for route in contract.get("routes") or []:
        if route.get("source") != "captured":
            continue
        route_id = route.get("id")
        route_path = route.get("path")
        if not isinstance(route_id, str) or not isinstance(route_path, str):
            continue
        html_path = contract_path.parent / "capture" / "pages" / f"{route_id}.html"
        metadata_path = contract_path.parent / "capture" / "pages" / f"{route_id}.json"
        if not html_path.is_file() or not metadata_path.is_file():
            continue
        markup = html_path.read_text(encoding="utf-8", errors="replace")
        if 'data-webanything-computed-style-freeze="v1"' not in markup:
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        computed = metadata.get("computed_styles") if isinstance(metadata, dict) else None
        if not isinstance(computed, list) or not computed:
            continue
        expected_visible = len(
            {
                item.get("node_id")
                for item in computed
                if isinstance(item, dict) and isinstance(item.get("node_id"), str)
            }
        )
        if expected_visible:
            requirement: dict[str, Any] = {
                "expected_visible_nodes": expected_visible,
                "minimum_visible_nodes": max(20, (expected_visible * 3 + 4) // 5),
            }
            if metadata.get("dom_topology"):
                requirement["dom_topology_required"] = True
            action_target_kinds = {
                action["id"]: _captured_target_kind(action["target_local_path"])
                for action in metadata.get("actions") or []
                if isinstance(action, dict)
                and isinstance(action.get("id"), str)
                and isinstance(action.get("target_local_path"), str)
            }
            if action_target_kinds:
                requirement["action_target_kinds"] = action_target_kinds
            requirements[route_path] = requirement
    return requirements


def _site_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _local_redirect_target(response: Any, request_url: str, base_origin: str) -> str | None:
    """Return a same-origin redirect destination without following it."""
    if not 300 <= int(response.status) < 400:
        return None
    location = response.headers.get("location")
    if not isinstance(location, str) or not location.strip():
        return None
    target = urljoin(request_url, location)
    parts = urlsplit(target)
    if f"{parts.scheme}://{parts.netloc}" != base_origin:
        return None
    return target


def _http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with LOOPBACK_OPENER.open(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        raise SyntheticReplayVerificationError(f"HTTP JSON request failed: {method} {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SyntheticReplayVerificationError(f"HTTP JSON object required: {method} {url}")
    return payload


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch(site_path: Path, contract: dict[str, Any]) -> tuple[subprocess.Popen[bytes], str]:
    entrypoint = (site_path / contract["runtime"]["entrypoint"]).resolve()
    try:
        entrypoint.relative_to(site_path)
    except ValueError as exc:
        raise SyntheticReplayVerificationError("runtime entrypoint escapes candidate") from exc
    if not entrypoint.is_file():
        raise SyntheticReplayVerificationError(f"runtime entrypoint is missing: {entrypoint}")
    port = _free_port()
    environment = dict(os.environ)
    environment.update({"PORT": str(port), "HOST": "127.0.0.1", "WEBANYTHING_VERIFY": "1"})
    # A replay with hundreds of local resources can emit enough Werkzeug access
    # log lines to fill an unread PIPE. Once full, the child blocks while
    # logging and Playwright reports a misleading navigation timeout. Spool the
    # combined runtime output to an anonymous temporary file instead: launch
    # failures remain diagnosable and a healthy long-running child never blocks
    # on verifier-owned logging.
    runtime_log = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            [sys.executable, str(entrypoint)],
            cwd=site_path,
            env=environment,
            stdout=runtime_log,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        runtime_log.close()
        raise
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            runtime_log.flush()
            runtime_log.seek(0)
            output = runtime_log.read().decode("utf-8", errors="replace")
            runtime_log.close()
            raise SyntheticReplayVerificationError(f"runtime exited during launch: {output[-2000:]}")
        try:
            _http_json(_site_url(base_url, contract["runtime"]["health_path"]))
            runtime_log.close()
            return process, base_url
        except SyntheticReplayVerificationError:
            time.sleep(0.1)
    process.terminate()
    runtime_log.close()
    raise SyntheticReplayVerificationError("runtime health check timed out")


def _perform_action(page: Any, action: dict[str, Any]) -> None:
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


def _wait_for_evaluation(
    url: str,
    *,
    result_field: str,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> dict[str, Any]:
    """Wait briefly for asynchronous browser action recording to reach SQLite."""
    deadline = time.monotonic() + timeout_seconds
    evaluation: dict[str, Any] = {}
    while True:
        evaluation = _http_json(url)
        if evaluation.get(result_field) is True or time.monotonic() >= deadline:
            return evaluation
        time.sleep(poll_seconds)


def _postcondition_failures(
    page: Any,
    runtime_db: Path,
    conditions: list[dict[str, Any]],
) -> list[str]:
    failures: list[str] = []
    current = urlsplit(page.url)
    for index, condition in enumerate(conditions):
        surface = condition["surface"]
        if surface == "url":
            if current.path != condition["path_equals"]:
                failures.append(
                    f"success[{index}] URL path is {current.path}, expected {condition['path_equals']}"
                )
            if "query_equals" in condition and current.query != condition["query_equals"]:
                failures.append(
                    f"success[{index}] URL query is {current.query!r}, expected {condition['query_equals']!r}"
                )
        elif surface == "dom":
            locator = page.locator(condition["selector"])
            if "visible" in condition:
                visible = locator.count() > 0 and locator.first.is_visible()
                if visible != condition["visible"]:
                    failures.append(
                        f"success[{index}] DOM visibility for {condition['selector']} is {visible}"
                    )
            elif "text_equals" in condition:
                actual = locator.first.inner_text().strip() if locator.count() else None
                if actual != condition["text_equals"]:
                    failures.append(
                        f"success[{index}] DOM text for {condition['selector']} is {actual!r}"
                    )
            else:
                actual = locator.first.get_attribute(condition["attribute"]) if locator.count() else None
                if actual != condition["equals"]:
                    failures.append(
                        f"success[{index}] DOM attribute {condition['attribute']} for {condition['selector']} is {actual!r}"
                    )
        elif surface == "sqlite":
            if not runtime_db.is_file():
                failures.append(f"success[{index}] runtime SQLite database is missing")
                continue
            where = condition["where"]
            clause = " AND ".join(f'"{key}" = ?' for key in where)
            query = f'SELECT COUNT(*) FROM "{condition["table"]}" WHERE {clause}'
            try:
                with sqlite3.connect(f"file:{runtime_db}?mode=ro", uri=True) as connection:
                    row = connection.execute(query, list(where.values())).fetchone()
            except sqlite3.Error as exc:
                failures.append(f"success[{index}] SQLite assertion failed to execute: {exc}")
                continue
            actual = int(row[0]) if row else 0
            if actual != condition["count_equals"]:
                failures.append(
                    f"success[{index}] SQLite count in {condition['table']} is {actual}, expected {condition['count_equals']}"
                )
    return failures


def _wait_for_postconditions(
    page: Any,
    runtime_db: Path,
    conditions: list[dict[str, Any]],
    *,
    timeout_seconds: float,
) -> list[str]:
    deadline = time.monotonic() + timeout_seconds
    failures = _postcondition_failures(page, runtime_db, conditions)
    while failures and time.monotonic() < deadline:
        time.sleep(0.05)
        failures = _postcondition_failures(page, runtime_db, conditions)
    return failures


def _static_failures(site_path: Path, contract: dict[str, Any], contract_path: Path) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    contract_copy = site_path / "synthetic_replay_contract.json"
    if not contract_copy.is_file() or contract_copy.read_bytes() != contract_path.read_bytes():
        failures.append({"code": "contract_copy_mismatch", "message": "candidate contract copy is missing or changed"})
    entrypoint = site_path / contract["runtime"]["entrypoint"]
    if not entrypoint.is_file():
        failures.append({"code": "runtime_entrypoint_missing", "message": "Flask runtime entrypoint is missing"})
    seed = site_path / contract["storage"]["seed_path"]
    if not seed.is_file() or sha256_file(seed) != contract["storage"]["seed_sha256"]:
        failures.append({"code": "seed_copy_mismatch", "message": "candidate SQLite seed is missing or changed"})
    provenance_path = site_path / "replay-provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        provenance = {}
    if (
        provenance.get("schema") != "webanything-replay-provenance-v1"
        or provenance.get("capture_bundle_sha256") != contract["capture_manifest"]["bundle_sha256"]
        or provenance.get("seed_sha256") != contract["storage"]["seed_sha256"]
        or not isinstance(provenance.get("candidate_files"), list)
    ):
        failures.append({"code": "replay_provenance_invalid", "message": "candidate lacks capture/seed provenance"})
    for path in site_path.rglob("*"):
        if path.is_symlink():
            failures.append({"code": "candidate_symlink", "message": f"candidate contains symlink: {path.relative_to(site_path)}"})
    return failures


def _repair_package(*, site: str, candidate: dict[str, Any], failures: list[dict[str, Any]]) -> dict[str, Any]:
    items = [dict(item) for item in failures]
    groups = []
    if items:
        groups.append({
            "route_family": "runtime",
            "page_archetype": "synthetic_replay_runtime",
            "control_type": "functional",
            "failure_codes": sorted({str(item.get("code") or "functional_verification_failed") for item in items}),
            "items": items,
        })
    return {
        "schema_version": 1,
        "repair_package": "webharbor-builder-repair-package-v1",
        "site": site,
        "status": "repair_required" if groups else "empty",
        "candidate_fingerprint": {
            "algorithm": candidate["algorithm"],
            "sha256": candidate["sha256"],
        },
        "failure_count": len(items),
        "group_count": len(groups),
        "groups": groups,
    }


def verify_synthetic_replay_candidate(
    *,
    site_path: Path,
    contract_path: Path,
    freeze_path: Path,
    output_path: Path,
    repair_package_path: Path | None = None,
    base_url: str | None = None,
    browser_path: str | None = None,
    timeout_ms: int = 10000,
) -> dict[str, Any]:
    site_path = site_path.resolve()
    contract_path = contract_path.resolve()
    failures: list[dict[str, Any]] = []
    try:
        contract = load_synthetic_replay_contract(contract_path)
    except SyntheticReplayContractError as exc:
        raise SyntheticReplayVerificationError(str(exc)) from exc
    try:
        freeze = load_candidate_freeze(freeze_path.resolve())
    except ReleaseError as exc:
        raise SyntheticReplayVerificationError(str(exc)) from exc
    identity = freeze.get("identity") or {}
    configuration = identity.get("configuration") or {}
    if freeze.get("state") != "frozen":
        failures.append({"code": "candidate_not_frozen", "message": "candidate freeze is not frozen"})
    if configuration.get("site") not in (None, contract["site_id"]) or configuration.get("build_mode") != "benchmark_synthetic_replay":
        failures.append({"code": "candidate_configuration_mismatch", "message": "freeze is bound to another mode or site"})
    if sha256_file(contract_path) not in {item.get("sha256") for item in identity.get("contracts") or []}:
        failures.append({"code": "contract_identity_mismatch", "message": "freeze does not bind the replay contract"})
    candidate_before = tree_identity(site_path)
    if candidate_before != (identity.get("tree") or {}):
        failures.append({"code": "candidate_identity_mismatch", "message": "candidate differs from frozen identity"})
    failures.extend(_static_failures(site_path, contract, contract_path))
    try:
        contract_artifact_paths(contract_path)
    except SyntheticReplayContractError as exc:
        failures.append({"code": "capture_bundle_invalid", "message": str(exc)})

    process: subprocess.Popen[bytes] | None = None
    route_ok = interaction_ok = auth_ok = task_ok = semantic_ok = persistence_ok = network_ok = proxy_ok = True
    reset_result: dict[str, Any] = {
        "seed_sha256": contract["storage"]["seed_sha256"],
        "first_sha256": None,
        "second_sha256": None,
        "byte_identical": False,
    }
    task_results: list[dict[str, Any]] = []
    tasks = _load_tasks(contract_path.parent / contract["tasks"]["path"])
    captured_dom_requirements = _captured_dom_requirements(contract_path, contract)
    declared_route_kinds = {
        urlsplit(route["path"]).path: route["kind"]
        for route in contract["routes"]
    }
    external_requests: list[str] = []
    http_errors: list[str] = []
    console_errors: list[str] = []
    resource_errors: list[str] = []

    def inspect_response(response: Any) -> None:
        if response.status >= 400:
            http_errors.append(f"{response.status} {response.url}")
            return
        resource_type = response.request.resource_type
        content_type = (response.headers.get("content-type") or "").casefold()
        if resource_type in {"image", "stylesheet", "script", "font", "media"} and content_type.startswith("text/html"):
            resource_errors.append(f"{resource_type} received {content_type or 'unknown'} {response.url}")

    def inspect_console(message: Any) -> None:
        if message.type != "error":
            return
        location = message.location or {}
        source = str(location.get("url") or "unknown")
        line = location.get("lineNumber")
        column = location.get("columnNumber")
        suffix = f":{line}:{column}" if isinstance(line, int) and isinstance(column, int) else ""
        console_errors.append(f"{message.text} @ {source}{suffix}")
    try:
        if base_url is None:
            process, base_url = _launch(site_path, contract)
        health = _http_json(_site_url(base_url, contract["runtime"]["health_path"]))
        if health.get("site_id") != contract["site_id"] or health.get("build_mode") != "benchmark_synthetic_replay":
            failures.append({"code": "runtime_identity_mismatch", "message": "health endpoint names another site or mode"})
        prefix_health = _http_json(
            _site_url(base_url, contract["runtime"]["health_path"]),
            headers={"X-Forwarded-Prefix": "/p/40000"},
        )
        proxy_ok = prefix_health.get("proxy_prefix_aware") is True and prefix_health.get("effective_prefix") == "/p/40000"
        if not proxy_ok:
            failures.append({"code": "proxy_prefix_failed", "message": "runtime did not honor X-Forwarded-Prefix"})

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SyntheticReplayVerificationError("Playwright is required; install webanything[browser]") from exc
        parsed_base = urlsplit(base_url)
        base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": True}
            if browser_path:
                launch_options["executable_path"] = browser_path
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            page.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith(base_origin) and not request.url.startswith("data:") and not request.url.startswith("blob:") else None)
            page.on("response", inspect_response)
            page.on("console", inspect_console)
            for route in contract["routes"]:
                page.goto(_site_url(base_url, route["path"]), wait_until="networkidle")
                fallback = page.locator('[data-webanything-route="local-target"], [data-webanything-fallback]')
                if fallback.count() > 0:
                    route_ok = interaction_ok = False
                    failures.append({
                        "code": "generic_fallback_detected",
                        "message": f"{route['path']} rendered a generic fallback instead of a declared product route",
                    })
                captured_requirement = captured_dom_requirements.get(route["path"])
                if captured_requirement is not None:
                    style_freeze_count = page.locator(
                        'style[data-webanything-computed-style-freeze="v1"]'
                    ).count()
                    topology_restored = (
                        page.locator('html[data-webanything-topology-restored="v1"]').count() == 1
                        if captured_requirement.get("dom_topology_required")
                        else True
                    )
                    visible_node_count = page.locator(
                        "[data-webanything-node-id]:visible"
                    ).count()
                    large_unbound_layers = page.locator(
                        "body *:visible:not([data-webanything-node-id])"
                    ).evaluate_all(
                        """
                        elements => elements
                          .map(element => {
                            const rect = element.getBoundingClientRect();
                            return {
                              tag: element.tagName.toLowerCase(),
                              id: element.id || null,
                              class_name: String(element.className || '').slice(0, 160),
                              width: Math.round(rect.width),
                              height: Math.round(rect.height),
                            };
                          })
                          .filter(item =>
                            item.width >= window.innerWidth * 0.4 &&
                            item.height >= window.innerHeight * 0.2
                          )
                        """
                    )
                    if (
                        style_freeze_count != 1
                        or not topology_restored
                        or visible_node_count < captured_requirement["minimum_visible_nodes"]
                    ):
                        route_ok = False
                        failures.append({
                            "code": "captured_dom_not_served",
                            "message": f"{route['path']} is not rendering the frozen captured DOM/CSS surface",
                            "style_freeze_count": style_freeze_count,
                            "topology_restored": topology_restored,
                            "visible_node_count": visible_node_count,
                            **captured_requirement,
                        })
                    if large_unbound_layers:
                        route_ok = False
                        failures.append({
                            "code": "captured_surface_overlay",
                            "message": f"{route['path']} adds a large visual layer outside the frozen captured DOM",
                            "layers": large_unbound_layers[:10],
                        })
                for selector in route["required_selectors"]:
                    if page.locator(selector).count() == 0:
                        route_ok = False
                        failures.append({"code": "route_selector_missing", "message": f"{route['path']}: {selector}"})
                declared = set(route.get("required_action_ids") or [])
                visible = set(page.locator("[data-action-id]:visible").evaluate_all("els => els.map(el => el.dataset.actionId)"))
                if declared != visible:
                    interaction_ok = False
                    failures.append({
                        "code": "interaction_inventory_mismatch",
                        "message": f"{route['path']} visible actions differ from contract",
                        "missing": sorted(declared - visible),
                        "unexpected": sorted(visible - declared),
                    })
                anchor_targets = page.locator("a[data-action-id]:visible").evaluate_all(
                    "els => els.map(el => ({id: el.dataset.actionId, href: el.getAttribute('href')}))"
                )
                checked_targets: set[str] = set()
                for action_target in anchor_targets:
                    action_id = str(action_target.get("id") or "")
                    href = action_target.get("href")
                    if not isinstance(href, str) or not href.strip() or href.lstrip().lower().startswith("javascript:"):
                        interaction_ok = False
                        failures.append({
                            "code": "dead_action_target",
                            "message": f"{route['path']} action {action_id} has no safe navigation target",
                        })
                        continue
                    target = urljoin(page.url, href)
                    target_parts = urlsplit(target)
                    target_origin = f"{target_parts.scheme}://{target_parts.netloc}"
                    if target_origin != base_origin:
                        interaction_ok = False
                        failures.append({
                            "code": "external_action_target",
                            "message": f"{route['path']} action {action_id} points outside the local replay",
                        })
                        continue
                    expected_kind = (captured_requirement or {}).get("action_target_kinds", {}).get(action_id)
                    actual_kind = declared_route_kinds.get(target_parts.path)
                    if actual_kind is None and target_parts.path.startswith("/api/navigate/"):
                        redirect_response = context.request.get(target, max_redirects=0)
                        redirected_target = _local_redirect_target(
                            redirect_response,
                            target,
                            base_origin,
                        )
                        if redirected_target is not None:
                            target = redirected_target
                            target_parts = urlsplit(target)
                            actual_kind = declared_route_kinds.get(target_parts.path)
                    if expected_kind is not None and actual_kind != expected_kind:
                        interaction_ok = False
                        failures.append({
                            "code": "action_target_kind_mismatch",
                            "message": f"{route['path']} action {action_id} does not reach a matching local product route",
                            "expected_kind": expected_kind,
                            "actual_kind": actual_kind,
                            "target_path": target_parts.path,
                        })
                        continue
                    if target in checked_targets:
                        continue
                    checked_targets.add(target)
                    target_response = context.request.get(target, max_redirects=0)
                    if target_response.status >= 400:
                        interaction_ok = False
                        failures.append({
                            "code": "dead_action_target",
                            "message": f"{route['path']} action {action_id} targets HTTP {target_response.status}",
                            "target_path": target_parts.path,
                        })
            prefix_external_start = len(external_requests)
            prefix_http_start = len(http_errors)
            prefix_console_start = len(console_errors)
            prefix_resource_start = len(resource_errors)
            prefix_context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                extra_http_headers={"X-Forwarded-Prefix": "/p/40000"},
            )
            prefix_page = prefix_context.new_page()
            prefix_page.set_default_timeout(timeout_ms)
            prefix_page.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith(base_origin) and not request.url.startswith("data:") and not request.url.startswith("blob:") else None)
            prefix_page.on("response", inspect_response)
            prefix_page.on("console", inspect_console)
            for route in contract["routes"]:
                prefixed_path = "/p/40000" + (route["path"] if route["path"].startswith("/") else f"/{route['path']}")
                response = prefix_page.goto(_site_url(base_url, prefixed_path), wait_until="networkidle")
                if response is None or response.status >= 400:
                    proxy_ok = False
                    failures.append({"code": "proxy_prefix_route_failed", "message": f"prefixed route failed: {route['path']}"})
                if prefix_page.locator('[data-webanything-route="local-target"], [data-webanything-fallback]').count() > 0:
                    proxy_ok = False
                    failures.append({
                        "code": "proxy_prefix_generic_fallback",
                        "message": f"prefixed route rendered a generic fallback: {route['path']}",
                    })
            login_task = next((task for task in tasks if task["workflow"] == "login"), None)
            if login_task is not None:
                prefix_auth_failures: list[str] = []
                _http_json(_site_url(base_url, contract["reset"]["endpoint"]), method="POST")
                home_path = contract["routes"][0]["path"]
                prefixed_home_path = "/p/40000" + (
                    home_path if home_path.startswith("/") else f"/{home_path}"
                )
                prefix_page.goto(_site_url(base_url, prefixed_home_path), wait_until="networkidle")
                auth_entry = prefix_page.locator(f"{AUTH_ENTRY_SELECTOR}:visible")
                if auth_entry.count() == 0:
                    prefix_auth_failures.append(
                        f"prefixed home route lacks a visible {AUTH_ENTRY_SELECTOR} login entry"
                    )
                else:
                    auth_entry.first.click()
                    prefix_page.wait_for_load_state("networkidle")
                    expected_login_path = "/p/40000" + login_task["start_url"]
                    if urlsplit(prefix_page.url).path != expected_login_path:
                        prefix_auth_failures.append(
                            f"prefixed login entry ended at {urlsplit(prefix_page.url).path}, expected {expected_login_path}"
                        )
                if not prefix_auth_failures:
                    for action in login_task["actions"]:
                        _perform_action(prefix_page, action)
                    prefix_page.wait_for_load_state("networkidle")
                    if urlsplit(prefix_page.url).path != prefixed_home_path:
                        prefix_auth_failures.append(
                            f"prefixed login ended at {urlsplit(prefix_page.url).path}, expected {prefixed_home_path}"
                        )
                    if prefix_page.locator(f"{AUTHENTICATED_SELECTOR}:visible").count() == 0:
                        prefix_auth_failures.append(
                            f"prefixed login did not expose visible {AUTHENTICATED_SELECTOR} user state"
                        )
                    prefix_page.reload(wait_until="networkidle")
                    if prefix_page.locator(f"{AUTHENTICATED_SELECTOR}:visible").count() == 0:
                        prefix_auth_failures.append(
                            "prefixed visible authenticated state did not persist across reload"
                        )
                if prefix_auth_failures:
                    proxy_ok = False
                    failures.append({
                        "code": "proxy_prefix_auth_failed",
                        "message": "prefixed authentication journey failed",
                        "details": prefix_auth_failures,
                    })
                _http_json(_site_url(base_url, contract["reset"]["endpoint"]), method="POST")
            prefix_context.close()
            prefix_external = external_requests[prefix_external_start:]
            prefix_http = http_errors[prefix_http_start:]
            prefix_console = console_errors[prefix_console_start:]
            prefix_resource = resource_errors[prefix_resource_start:]
            if prefix_external or prefix_http or prefix_console or prefix_resource:
                proxy_ok = False
                failures.append({
                    "code": "proxy_prefix_browser_errors",
                    "message": "prefixed route sweep observed external requests or browser runtime errors",
                    "unexpected_external_requests": sorted(set(prefix_external)),
                    "http_errors": prefix_http,
                    "console_errors": prefix_console,
                    "resource_errors": prefix_resource,
                })
            if not proxy_ok:
                raise SyntheticReplayVerificationError("prefixed route sweep failed")
            home_path = contract["routes"][0]["path"]
            runtime_db = site_path / contract["storage"]["runtime_path"]
            login_task = next(task for task in tasks if task["workflow"] == "login")
            for task in tasks:
                task_failures: list[str] = []
                _http_json(_site_url(base_url, contract["reset"]["endpoint"]), method="POST")
                if task.get("initial_state") == "authenticated":
                    page.goto(_site_url(base_url, login_task["start_url"]), wait_until="networkidle")
                    for action in login_task["actions"]:
                        _perform_action(page, action)
                    page.wait_for_load_state("networkidle")
                    if page.locator(f"{AUTHENTICATED_SELECTOR}:visible").count() == 0:
                        task_failures.append("authenticated task precondition could not be established")
                if task["workflow"] == "login":
                    page.goto(_site_url(base_url, home_path), wait_until="networkidle")
                    auth_entry = page.locator(f"{AUTH_ENTRY_SELECTOR}:visible")
                    if auth_entry.count() == 0:
                        task_failures.append(
                            f"home route lacks a visible {AUTH_ENTRY_SELECTOR} login entry"
                        )
                    else:
                        auth_entry.first.click()
                        page.wait_for_load_state("networkidle")
                        expected_login_path = urlsplit(
                            _site_url(base_url, task["start_url"])
                        ).path
                        if urlsplit(page.url).path != expected_login_path:
                            task_failures.append(
                                "home login entry did not navigate to the declared login route"
                            )
                if urlsplit(page.url).path != urlsplit(
                    _site_url(base_url, task["start_url"])
                ).path:
                    page.goto(_site_url(base_url, task["start_url"]), wait_until="networkidle")
                for action in task["actions"]:
                    _perform_action(page, action)
                if task["workflow"] == "login":
                    page.wait_for_load_state("networkidle")
                    expected_home_path = urlsplit(_site_url(base_url, home_path)).path
                    if urlsplit(page.url).path != expected_home_path:
                        task_failures.append(
                            f"login ended at {urlsplit(page.url).path}, expected {expected_home_path}"
                        )
                    if page.locator(f"{AUTHENTICATED_SELECTOR}:visible").count() == 0:
                        task_failures.append(
                            f"login did not expose visible {AUTHENTICATED_SELECTOR} user state"
                        )
                semantic_failures = _wait_for_postconditions(
                    page,
                    runtime_db,
                    task["success"],
                    timeout_seconds=max(0.5, min(5.0, timeout_ms / 1000)),
                )
                if semantic_failures:
                    semantic_ok = False
                    task_failures.extend(semantic_failures)
                endpoint = contract["evaluator"]["endpoint_template"].replace("{evaluator_id}", task["evaluator_id"])
                evaluation = _wait_for_evaluation(
                    _site_url(base_url, endpoint),
                    result_field=contract["evaluator"]["result_field"],
                    timeout_seconds=max(0.5, min(5.0, timeout_ms / 1000)),
                )
                if evaluation.get(contract["evaluator"]["result_field"]) is not True:
                    task_failures.append("evaluator returned a non-passing result")
                page.reload(wait_until="networkidle")
                persisted_semantic_failures = _wait_for_postconditions(
                    page,
                    runtime_db,
                    task["success"],
                    timeout_seconds=max(0.5, min(3.0, timeout_ms / 1000)),
                )
                if persisted_semantic_failures:
                    semantic_ok = False
                    persistence_ok = False
                    task_failures.extend(
                        f"after reload: {item}" for item in persisted_semantic_failures
                    )
                persisted = _http_json(_site_url(base_url, endpoint))
                if persisted.get(contract["evaluator"]["result_field"]) is not True:
                    persistence_ok = False
                    task_failures.append("evaluator result did not persist across reload")
                if (
                    task["workflow"] == "login"
                    and page.locator(f"{AUTHENTICATED_SELECTOR}:visible").count() == 0
                ):
                    persistence_ok = False
                    task_failures.append("visible authenticated state did not persist across reload")
                passed = not task_failures
                task_ok = task_ok and passed
                if task["workflow"] in {"login", "logout"}:
                    auth_ok = auth_ok and passed
                if not passed:
                    failures.append({"code": "task_failed", "message": f"task {task['task_id']} failed", "details": task_failures})
                task_results.append({
                    "task_id": task["task_id"],
                    "workflow": task["workflow"],
                    "status": "passed" if passed else "failed",
                    "success": task["success"],
                    "failures": task_failures,
                })
                # Authentication is a prerequisite for meaningful downstream
                # task coverage. Hand a focused repair package back immediately
                # instead of spending minutes replaying unrelated actions.
                if task["workflow"] == "login" and not passed:
                    break
            context.close()
            browser.close()
        network_ok = not external_requests and not http_errors and not console_errors and not resource_errors
        if not network_ok:
            failures.append({
                "code": "browser_runtime_errors",
                "message": "browser observed external requests or runtime errors",
                "unexpected_external_requests": sorted(set(external_requests)),
                "http_errors": list(http_errors),
                "console_errors": list(console_errors),
                "resource_errors": list(resource_errors),
            })
        runtime_db = site_path / contract["storage"]["runtime_path"]
        seed_db = site_path / contract["storage"]["seed_path"]
        _http_json(_site_url(base_url, contract["reset"]["endpoint"]), method="POST")
        first = sha256_file(runtime_db) if runtime_db.is_file() else None
        _http_json(_site_url(base_url, contract["reset"]["endpoint"]), method="POST")
        second = sha256_file(runtime_db) if runtime_db.is_file() else None
        seed_hash = sha256_file(seed_db) if seed_db.is_file() else None
        reset_result = {"seed_sha256": seed_hash, "first_sha256": first, "second_sha256": second, "byte_identical": seed_hash == first == second}
        if not reset_result["byte_identical"]:
            failures.append({"code": "reset_not_byte_identical", "message": "two resets did not restore exact seed bytes"})
    except Exception as exc:
        route_ok = interaction_ok = auth_ok = task_ok = semantic_ok = persistence_ok = network_ok = proxy_ok = False
        failures.append({"code": "browser_verification_error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    candidate_after = tree_identity(site_path)
    immutable = candidate_before == candidate_after == (identity.get("tree") or {})
    if not immutable:
        failures.append({"code": "candidate_mutated", "message": "verifier changed release candidate files"})
    static_codes = {item["code"] for item in failures}
    contract_ok = not static_codes.intersection({"candidate_not_frozen", "candidate_configuration_mismatch", "contract_identity_mismatch", "candidate_identity_mismatch", "contract_copy_mismatch", "runtime_entrypoint_missing", "seed_copy_mismatch"})
    capture_ok = "capture_bundle_invalid" not in static_codes and "replay_provenance_invalid" not in static_codes
    concurrency_ok = contract["concurrency"]["isolation"] == "session_partitioned" or contract["concurrency"]["max_parallel_sessions"] == 1
    coverage_ok = (
        set(contract["coverage"]["required_route_kinds"])
        <= {route["kind"] for route in contract["routes"]}
        and set(contract["coverage"]["required_workflows"])
        <= {task["workflow"] for task in tasks}
        and contract["coverage"]["generic_fallback_allowed"] is False
    )
    checks = {
        "contract": "passed" if contract_ok else "failed",
        "capture_bundle": "passed" if capture_ok else "failed",
        "product_coverage": "passed" if coverage_ok else "failed",
        "routes": "passed" if route_ok else "failed",
        "interactions": "passed" if interaction_ok else "failed",
        "auth": "passed" if auth_ok else "failed",
        "tasks": "passed" if task_ok and len(task_results) == contract["tasks"]["count"] else "failed",
        "semantic_tasks": "passed" if semantic_ok and len(task_results) == contract["tasks"]["count"] else "failed",
        "reset": "passed" if reset_result["byte_identical"] else "failed",
        "persistence": "passed" if persistence_ok else "failed",
        "determinism": "passed" if reset_result["byte_identical"] else "failed",
        "resource_privacy": "passed" if network_ok else "failed",
        "proxy_prefix": "passed" if proxy_ok else "failed",
        "concurrency_isolation": "passed" if concurrency_ok else "failed",
        "candidate_immutability": "passed" if immutable else "failed",
    }
    nonrepairable_codes = {
        "candidate_not_frozen", "candidate_configuration_mismatch", "contract_identity_mismatch",
        "candidate_identity_mismatch", "capture_bundle_invalid", "candidate_mutated",
    }
    repairable = bool(failures) and immutable and not nonrepairable_codes.intersection(
        item.get("code") for item in failures
    )
    result = {
        "schema": "webanything-synthetic-replay-verification-v1",
        "status": "passed" if not failures and all(value == "passed" for value in checks.values()) else "failed",
        "site": contract["site_id"],
        "build_mode": "benchmark_synthetic_replay",
        "repairable": repairable,
        "candidate_identity_sha256": identity.get("sha256"),
        "candidate_sha256": candidate_before["sha256"],
        "contract_path": str(contract_path),
        "contract_sha256": sha256_file(contract_path),
        "checks": checks,
        "reset": reset_result,
        "tasks": task_results,
        "network": {
            "unexpected_external_requests": len(external_requests),
            "urls": sorted(set(external_requests)),
            "http_errors": http_errors,
            "console_errors": console_errors,
            "resource_errors": resource_errors,
        },
        "candidate": {"pre_hash": candidate_before, "post_hash": candidate_after, "unchanged": immutable},
        "failures": failures,
    }
    atomic_json(output_path.resolve(), result)
    if repair_package_path is not None:
        atomic_json(
            repair_package_path.resolve(),
            _repair_package(site=contract["site_id"], candidate=candidate_before, failures=failures if repairable else []),
        )
    return result
