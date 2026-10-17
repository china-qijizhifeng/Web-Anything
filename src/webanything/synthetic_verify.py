"""Independent browser verifier for deterministic synthetic benchmarks."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from .release import atomic_json, load_candidate_freeze, sha256_file, tree_identity
from .synthetic import (
    SyntheticContractError,
    canonical_sha256,
    contract_content_mode,
    load_synthetic_contract,
    load_tasks,
)


TEXT_EXTENSIONS = {".html", ".css", ".js", ".json", ".jsonl", ".svg", ".txt", ".md"}
FORBIDDEN_RUNTIME_PATTERNS = {
    "direct_date_now": re.compile(r"\bDate\.now\s*\("),
    "direct_new_date": re.compile(r"\bnew\s+Date\s*\("),
    "direct_math_random": re.compile(r"\bMath\.random\s*\("),
    "direct_random_uuid": re.compile(r"\bcrypto\.randomUUID\s*\("),
}
EXTERNAL_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SyntheticVerificationError(RuntimeError):
    """Synthetic verification cannot be performed safely."""


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _wait_ready(url: str, process: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SyntheticVerificationError(f"static server exited with {process.returncode}")
        try:
            with LOOPBACK_OPENER.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # readiness polling records only the final error
            last = exc
        time.sleep(0.1)
    raise SyntheticVerificationError(f"static server did not become ready: {last}")


def _candidate_text_files(site_path: Path) -> Iterable[Path]:
    for path in sorted(site_path.rglob("*")):
        if path.is_file() and path.suffix.casefold() in TEXT_EXTENSIONS:
            yield path


def _mulberry32_values(seed: int, count: int) -> list[float]:
    value = seed & 0xFFFFFFFF
    output: list[float] = []
    for _ in range(count):
        value = (value + 0x6D2B79F5) & 0xFFFFFFFF
        mixed = value
        mixed = ((mixed ^ (mixed >> 15)) * (mixed | 1)) & 0xFFFFFFFF
        mixed ^= (mixed + (((mixed ^ (mixed >> 7)) * (mixed | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        output.append(((mixed ^ (mixed >> 14)) & 0xFFFFFFFF) / 4294967296)
    return output


def static_failures(site_path: Path, contract: dict[str, Any], contract_path: Path) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    content_mode = contract_content_mode(contract)
    candidate_contract = site_path / "synthetic_contract.json"
    excluded = {
        contract_path.resolve(),
        candidate_contract.resolve(),
        (site_path / "content-provenance.json").resolve(),
    }
    if not candidate_contract.is_file() or sha256_file(candidate_contract) != sha256_file(contract_path):
        failures.append({
            "code": "contract_copy_mismatch",
            "message": "candidate synthetic_contract.json is not a byte-for-byte copy of the frozen contract",
        })
    for artifact in [*contract["fixtures"], contract["tasks"]]:
        relative = str(artifact["path"])
        candidate_artifact = site_path / relative
        if not candidate_artifact.is_file() or sha256_file(candidate_artifact) != artifact["sha256"]:
            failures.append({
                "code": "contract_artifact_mismatch",
                "message": "candidate contract artifact is missing or stale",
                "path": relative,
            })
    canonical_runtime = Path(__file__).resolve().parents[2] / "runtime/webanything-benchmark-runtime.js"
    candidate_runtime = site_path / "webanything-runtime.js"
    if (
        not canonical_runtime.is_file()
        or not candidate_runtime.is_file()
        or sha256_file(candidate_runtime) != sha256_file(canonical_runtime)
    ):
        failures.append({
            "code": "runtime_copy_mismatch",
            "message": "candidate webanything-runtime.js is not the canonical deterministic runtime",
        })
    forbidden_terms = [str(item).casefold() for item in contract["content_policy"]["forbidden_terms"]]
    for path in sorted(site_path.rglob("*")):
        if path.is_symlink():
            failures.append({
                "code": "candidate_symlink",
                "message": "synthetic candidates must not contain symlinks",
                "path": path.relative_to(site_path).as_posix(),
            })
    for path in _candidate_text_files(site_path):
        if path.resolve() in excluded:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append({"code": "invalid_text_encoding", "message": "candidate text must be UTF-8", "path": path.relative_to(site_path).as_posix()})
            continue
        relative = path.relative_to(site_path).as_posix()
        urls = sorted(set(EXTERNAL_URL_RE.findall(text)))
        if urls:
            failures.append({"code": "external_url_literal", "message": "candidate contains an external URL", "path": relative, "urls": urls[:5]})
        lowered = text.casefold()
        matched = sorted(term for term in forbidden_terms if term in lowered) if content_mode == "fictional" else []
        if matched:
            failures.append({"code": "forbidden_source_brand", "message": "candidate contains a forbidden source term", "path": relative, "terms": matched})
        if path.suffix.casefold() == ".js" and path.name != "webanything-runtime.js":
            for code, pattern in FORBIDDEN_RUNTIME_PATTERNS.items():
                if pattern.search(text):
                    failures.append({"code": code, "message": "candidate bypasses the deterministic runtime", "path": relative})
    provenance_path = site_path / "content-provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        failures.append({"code": "content_provenance_missing", "message": f"content provenance is unavailable: {exc}"})
    else:
        raw_assets = provenance.get("assets") if isinstance(provenance, dict) else None
        raw_upstream = provenance.get("upstream_assets") if isinstance(provenance, dict) else None
        base_valid = (
            isinstance(provenance, dict)
            and provenance.get("schema") == "webanything-synthetic-content-provenance-v1"
            and isinstance(raw_upstream, list)
            and all(
                isinstance(item, str) and item and not Path(item).is_absolute() and ".." not in Path(item).parts
                for item in raw_upstream
            )
            and isinstance(raw_assets, list)
            and raw_assets
            and all(
                isinstance(item, str) and item and not Path(item).is_absolute() and ".." not in Path(item).parts
                for item in raw_assets
            )
        )
        if content_mode == "source_fidelity":
            mode_valid = (
                provenance.get("synthetic") is False
                and bool(raw_upstream)
                and provenance.get("source_reference_sha256")
                == (contract.get("visual_reference") or {}).get("reference_sha256")
            ) if isinstance(provenance, dict) else False
        else:
            mode_valid = (
                provenance.get("synthetic") is True
                and raw_upstream == []
                and provenance.get("source_reference_sha256") in (None, "")
            ) if isinstance(provenance, dict) else False
        if (
            not base_valid
            or not mode_valid
        ):
            failures.append({
                "code": "content_provenance_invalid",
                "message": f"content provenance does not prove {content_mode} local assets",
            })
        else:
            declared = set(raw_assets)
            upstream = set(raw_upstream)
            actual = {
                path.relative_to(site_path).as_posix()
                for path in site_path.rglob("*")
                if path.is_file() and path.name != "content-provenance.json"
            }
            if not upstream.issubset(declared):
                failures.append({
                    "code": "content_provenance_invalid",
                    "message": "source-derived assets must be included in the complete asset inventory",
                    "unexpected": sorted(upstream - declared),
                })
            if declared != actual:
                failures.append({
                    "code": "content_provenance_incomplete",
                    "message": "content provenance must enumerate every candidate artifact exactly",
                    "missing": sorted(actual - declared),
                    "unexpected": sorted(declared - actual),
                })
    return failures


def _value_at_path(value: Any, dotted: str) -> Any:
    current = value
    for token in dotted.split("."):
        if not token:
            raise KeyError(dotted)
        if isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise KeyError(dotted)
    return current


def _changed_paths(before: Any, after: Any, prefix: str = "") -> set[str]:
    if type(before) is not type(after):
        return {prefix or "$"}
    if isinstance(before, dict):
        changed: set[str] = set()
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                changed.add(path)
            else:
                changed.update(_changed_paths(before[key], after[key], path))
        return changed
    if isinstance(before, list):
        if len(before) != len(after):
            return {prefix or "$"}
        changed: set[str] = set()
        for index, (left, right) in enumerate(zip(before, after)):
            changed.update(_changed_paths(left, right, f"{prefix}.{index}" if prefix else str(index)))
        return changed
    return set() if before == after else {prefix or "$"}


def _allowed_change(path: str, allowed: list[str]) -> bool:
    return any(path == item or path.startswith(item + ".") or item.startswith(path + ".") for item in allowed)


def _logical_path(url: str, base_url: str) -> str:
    current = urlsplit(url).path
    base = urlsplit(base_url).path.rstrip("/")
    if base and current.startswith(base + "/"):
        current = current[len(base):]
    return current or "/"


def _route_url(base_url: str, route: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", route.lstrip("/"))


def _raw_state(page: Any, namespace: str) -> dict[str, Any]:
    value = page.evaluate(
        """key => {
          const raw = window.localStorage.getItem(key);
          if (raw === null) throw new Error(`missing localStorage key ${key}`);
          const parsed = JSON.parse(raw);
          if (!parsed || typeof parsed !== 'object' || !parsed.state) throw new Error('invalid state envelope');
          return parsed.state;
        }""",
        namespace,
    )
    if not isinstance(value, dict):
        raise SyntheticVerificationError("localStorage state must be an object")
    return value


def _perform_action(page: Any, action: dict[str, Any]) -> None:
    locator = page.locator(action["selector"])
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
    else:  # contract compilation prevents this branch
        raise SyntheticVerificationError(f"unsupported action: {kind}")


def _assert_success(page: Any, assertion: dict[str, Any], state: dict[str, Any], base_url: str) -> str | None:
    kind = assertion["type"]
    if kind == "local_state":
        try:
            actual = _value_at_path(state, assertion["path"])
        except KeyError:
            return f"state path is missing: {assertion['path']}"
        if actual != assertion["equals"]:
            return f"state {assertion['path']} expected {assertion['equals']!r}, got {actual!r}"
    elif kind == "dom":
        locator = page.locator(assertion["selector"])
        expected_exists = assertion.get("exists", True)
        if (locator.count() > 0) != expected_exists:
            return f"DOM selector existence mismatch: {assertion['selector']}"
        if "text_contains" in assertion and assertion["text_contains"] not in (locator.first.text_content() or ""):
            return f"DOM selector lacks expected text: {assertion['selector']}"
    elif kind == "url":
        if _logical_path(page.url, base_url) != assertion["path"]:
            return f"URL expected {assertion['path']}, got {_logical_path(page.url, base_url)}"
    return None


def verify_synthetic_candidate(
    *,
    site_path: Path,
    contract_path: Path,
    freeze_path: Path,
    output_path: Path,
    base_url: str | None = None,
    browser_path: Path | None = None,
    dependency_path: Path | None = None,
    timeout_ms: int = 10_000,
) -> dict[str, Any]:
    site_path = site_path.resolve()
    contract_path = contract_path.resolve()
    freeze_path = freeze_path.resolve()
    output_path = output_path.resolve()
    failures: list[dict[str, Any]] = []
    try:
        contract = load_synthetic_contract(contract_path)
    except SyntheticContractError as exc:
        raise SyntheticVerificationError(str(exc)) from exc
    freeze = load_candidate_freeze(freeze_path)
    identity = freeze.get("identity") or {}
    contract_hash = sha256_file(contract_path)
    candidate_before = tree_identity(site_path)
    if freeze.get("state") != "frozen":
        failures.append({"code": "candidate_not_frozen", "message": "candidate freeze state is not frozen"})
    if (identity.get("tree") or {}) != candidate_before:
        failures.append({"code": "candidate_identity_mismatch", "message": "candidate differs from its frozen identity"})
    if contract_hash not in {item.get("sha256") for item in identity.get("contracts") or []}:
        failures.append({"code": "contract_identity_mismatch", "message": "contract is not bound by the candidate freeze"})
    configuration = identity.get("configuration") or {}
    if configuration.get("site") not in (None, contract["site_id"]) or configuration.get("build_mode") != "benchmark_synthetic":
        failures.append({"code": "candidate_configuration_mismatch", "message": "freeze configuration is not synthetic or names another site"})
    failures.extend(static_failures(site_path, contract, contract_path))

    task_path = contract_path.parent / contract["tasks"]["path"]
    tasks = load_tasks(task_path, route_paths={item["path"] for item in contract["routes"]})
    initial_fixture_path = contract_path.parent / "fixtures/initial-state.json"
    try:
        fixed_initial_state = json.loads(initial_fixture_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SyntheticVerificationError(f"invalid fixed initial state fixture: {exc}") from exc
    if not isinstance(fixed_initial_state, dict):
        raise SyntheticVerificationError("fixed initial state fixture must be an object")
    server: subprocess.Popen[bytes] | None = None
    browser = None
    task_results: list[dict[str, Any]] = []
    interaction_results: list[dict[str, Any]] = []
    reset_result = {"first_sha256": None, "second_sha256": None, "identical": False}
    route_ok = True
    interaction_ok = True
    task_ok = True
    persistence_ok = True
    reset_ok = True
    network_ok = True
    external_requests: list[str] = []
    browser_stage = "initialization"
    try:
        if dependency_path:
            sys.path.insert(0, str(dependency_path.resolve()))
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SyntheticVerificationError("Playwright is required for synthetic verification") from exc
        if base_url is None:
            port = _free_port()
            server_script = Path(__file__).resolve().parents[2] / "scripts/static_site_server.py"
            server = subprocess.Popen(
                [sys.executable, str(server_script), "--root", str(site_path), "--site-id", contract["site_id"], "--port", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            base_url = f"http://127.0.0.1:{port}/"
            _wait_ready(base_url + "_health", server)
        base_url = base_url.rstrip("/") + "/"
        base_origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": True}
            if browser_path:
                launch_options["executable_path"] = str(browser_path.resolve())
            browser = playwright.chromium.launch(**launch_options)

            def observe_request(request: Any) -> None:
                parsed = urlsplit(request.url)
                if parsed.scheme in {"http", "https"} and f"{parsed.scheme}://{parsed.netloc}" != base_origin:
                    external_requests.append(request.url)

            def new_page() -> tuple[Any, Any]:
                fresh_context = browser.new_context(viewport={"width": 1440, "height": 1000})
                fresh_page = fresh_context.new_page()
                fresh_page.set_default_timeout(timeout_ms)
                fresh_page.on("request", observe_request)
                return fresh_context, fresh_page

            context, page = new_page()
            for route in contract["routes"]:
                browser_stage = f"route:{route['path']}"
                response = page.goto(_route_url(base_url, route["path"]), wait_until="networkidle")
                if response is None or response.status >= 400:
                    route_ok = False
                    failures.append({"code": "route_unavailable", "message": f"route failed: {route['path']}"})
                    continue
                page.wait_for_function("() => Boolean(window.__WEBANYTHING__)")
                for selector in route["required_selectors"]:
                    if page.locator(selector).count() == 0:
                        route_ok = False
                        failures.append({"code": "route_dom_missing", "message": f"{route['path']} lacks {selector}"})
                policy = contract.get("interaction_policy")
                if policy is not None:
                    inventory = page.locator(policy["inventory_selector"])
                    visible_action_ids: list[str] = []
                    missing_attribute = 0
                    for index in range(inventory.count()):
                        control = inventory.nth(index)
                        if not control.is_visible():
                            continue
                        action_id = control.get_attribute(policy["action_attribute"])
                        if action_id:
                            visible_action_ids.append(action_id)
                        else:
                            missing_attribute += 1
                    duplicate_ids = sorted({
                        action_id for action_id in visible_action_ids
                        if visible_action_ids.count(action_id) > 1
                    })
                    expected_ids = set(route["required_action_ids"])
                    actual_ids = set(visible_action_ids)
                    missing_ids = sorted(expected_ids - actual_ids)
                    unexpected_ids = sorted(actual_ids - expected_ids)
                    passed = not missing_attribute and not duplicate_ids and not unexpected_ids
                    interaction_ok = interaction_ok and passed
                    interaction_results.append({
                        "route": route["path"],
                        "status": "passed" if passed else "failed",
                        "visible_controls": len(visible_action_ids) + missing_attribute,
                        "declared_actions": len(expected_ids),
                        "missing_action_attribute": missing_attribute,
                        "duplicate_action_ids": duplicate_ids,
                        "not_initially_visible_action_ids": missing_ids,
                        "unexpected_action_ids": unexpected_ids,
                    })
                    if not passed:
                        failures.append({
                            "code": "interaction_inventory_mismatch",
                            "message": f"{route['path']} visible controls differ from the frozen action inventory",
                            "missing_action_attribute": missing_attribute,
                            "duplicate_action_ids": duplicate_ids,
                            "not_initially_visible_action_ids": missing_ids,
                            "unexpected_action_ids": unexpected_ids,
                        })
            browser_stage = "runtime-api"
            api_ok = page.evaluate(
                """() => {
                  const api = window.__WEBANYTHING__;
                  return Boolean(api)
                    && ['getState', 'setState', 'update', 'reset', 'getClock', 'random']
                      .every(name => typeof api[name] === 'function');
                }"""
            )
            if not api_ok:
                failures.append({"code": "runtime_api_missing", "message": "standard window.__WEBANYTHING__ API is incomplete"})
            clock = page.evaluate("() => window.__WEBANYTHING__.getClock()")
            if not isinstance(clock, dict) or clock.get("iso") != contract["clock"]["fixed_time"] or clock.get("timezone") != "UTC":
                failures.append({"code": "fixed_clock_mismatch", "message": "runtime clock differs from the frozen contract"})

            browser_stage = "reset-api"
            page.goto(_route_url(base_url, contract["reset"]["initial_url"]), wait_until="networkidle")
            page.wait_for_function("() => Boolean(window.__WEBANYTHING__)")
            namespace = contract["storage"]["namespace"]
            initial_state = _raw_state(page, namespace)
            if initial_state != fixed_initial_state:
                reset_ok = False
                failures.append({"code": "initial_state_mismatch", "message": "browser state differs from fixtures/initial-state.json"})
            page.evaluate("() => window.__WEBANYTHING__.update(state => { state.__verifier_probe__ = true; })")
            page.evaluate("() => window.__WEBANYTHING__.reset({navigate: false})")
            first = _raw_state(page, namespace)
            page.evaluate("() => window.__WEBANYTHING__.update(state => { state.__verifier_probe__ = 'second'; })")
            page.evaluate("() => window.__WEBANYTHING__.reset({navigate: false})")
            second = _raw_state(page, namespace)
            reset_result = {
                "first_sha256": canonical_sha256(first),
                "second_sha256": canonical_sha256(second),
                "identical": first == second == fixed_initial_state,
            }
            if not reset_result["identical"]:
                reset_ok = False
                failures.append({"code": "reset_not_deterministic", "message": "two resets did not restore the same initial state"})
            observed_random = page.evaluate("() => [window.__WEBANYTHING__.random(), window.__WEBANYTHING__.random(), window.__WEBANYTHING__.random()]")
            expected_random = _mulberry32_values(contract["random"]["seed"], 3)
            if observed_random != expected_random:
                failures.append({"code": "random_seed_mismatch", "message": "runtime random sequence differs from the frozen seed"})

            browser_stage = "reset-entrypoint"
            page.goto(_route_url(base_url, contract["reset"]["entrypoint"]), wait_until="networkidle")
            page.wait_for_function(
                "suffix => window.location.pathname.endsWith(suffix)",
                arg=contract["reset"]["initial_url"],
            )
            page.wait_for_function("() => Boolean(window.__WEBANYTHING__)")
            if _raw_state(page, namespace) != fixed_initial_state:
                reset_ok = False
                failures.append({"code": "reset_entrypoint_failed", "message": "reset.html did not restore the fixed initial state"})
            context.close()

            for task in tasks:
                browser_stage = f"task:{task['task_id']}:start"
                context, page = new_page()
                task_failures: list[str] = []
                try:
                    page.goto(_route_url(base_url, task["start_url"]), wait_until="networkidle")
                    page.wait_for_function("() => Boolean(window.__WEBANYTHING__)")
                    before = _raw_state(page, namespace)
                    if before != fixed_initial_state:
                        task_failures.append("fresh browser context did not start from the fixed initial state")
                    for action in task["actions"]:
                        browser_stage = f"task:{task['task_id']}:action:{action['type']}:{action['selector']}"
                        _perform_action(page, action)
                    after = _raw_state(page, namespace)
                    changed = sorted(_changed_paths(before, after))
                    unexpected = [path for path in changed if not _allowed_change(path, task["allowed_state_changes"])]
                    if unexpected:
                        task_failures.append("unexpected state changes: " + ", ".join(unexpected))
                    for assertion in task["success"]:
                        error = _assert_success(page, assertion, after, base_url)
                        if error:
                            task_failures.append(error)
                    browser_stage = f"task:{task['task_id']}:reload"
                    page.reload(wait_until="networkidle")
                    page.wait_for_function("() => Boolean(window.__WEBANYTHING__)")
                    persisted = _raw_state(page, namespace)
                    if persisted != after:
                        persistence_ok = False
                        task_failures.append("state did not persist across reload")
                except Exception as exc:
                    task_failures.append(f"task replay raised {type(exc).__name__}: {exc}")
                finally:
                    context.close()
                passed = not task_failures
                task_ok = task_ok and passed
                if not passed:
                    failures.append({"code": "task_failed", "message": f"task {task['task_id']} failed", "details": task_failures})
                task_results.append({"task_id": task["task_id"], "status": "passed" if passed else "failed", "failures": task_failures})
            network_ok = not external_requests
            if not network_ok:
                failures.append({"code": "external_request", "message": "browser made external network requests", "urls": sorted(set(external_requests))[:20]})
            browser.close()
            browser = None
    except Exception as exc:
        failures.append({"code": "browser_verification_error", "message": f"{browser_stage}: {type(exc).__name__}: {exc}"})
        route_ok = interaction_ok = task_ok = persistence_ok = reset_ok = network_ok = False
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)

    candidate_after = tree_identity(site_path)
    immutable = candidate_before == candidate_after == (identity.get("tree") or {})
    if not immutable:
        failures.append({"code": "candidate_mutated", "message": "verification changed the frozen candidate"})
    content_ok = not any(item["code"] in {
        "external_url_literal", "forbidden_source_brand", "content_provenance_missing",
        "content_provenance_invalid", "content_provenance_incomplete", "candidate_symlink",
        "invalid_text_encoding",
    } for item in failures)
    determinism_ok = not any(item["code"] in set(FORBIDDEN_RUNTIME_PATTERNS) | {
        "reset_not_deterministic", "initial_state_mismatch", "fixed_clock_mismatch", "random_seed_mismatch",
    } for item in failures)
    contract_ok = not any(item["code"] in {
        "candidate_not_frozen", "candidate_identity_mismatch", "contract_identity_mismatch",
        "candidate_configuration_mismatch", "contract_copy_mismatch", "contract_artifact_mismatch",
        "runtime_copy_mismatch", "runtime_api_missing",
    } for item in failures)
    checks = {
        "contract": "passed" if contract_ok else "failed",
        "routes": "passed" if route_ok else "failed",
        "interactions": "passed" if interaction_ok else "failed",
        "tasks": "passed" if task_ok and len(task_results) == len(tasks) else "failed",
        "reset": "passed" if reset_ok and reset_result["identical"] else "failed",
        "persistence": "passed" if persistence_ok else "failed",
        "determinism": "passed" if determinism_ok else "failed",
        "resource_privacy": "passed" if network_ok else "failed",
        "content_policy": "passed" if content_ok else "failed",
        "candidate_immutability": "passed" if immutable else "failed",
    }
    result = {
        "schema": "webanything-synthetic-verification-v1",
        "status": "passed" if not failures and all(value == "passed" for value in checks.values()) else "failed",
        "site": contract["site_id"],
        "build_mode": "benchmark_synthetic",
        "candidate_identity_sha256": identity.get("sha256"),
        "candidate_sha256": candidate_before["sha256"],
        "contract_path": str(contract_path),
        "contract_sha256": contract_hash,
        "checks": checks,
        "reset": reset_result,
        "tasks": task_results,
        "interactions": interaction_results,
        "network": {"unexpected_external_requests": len(external_requests), "urls": sorted(set(external_requests))},
        "candidate": {"pre_hash": candidate_before, "post_hash": candidate_after, "unchanged": immutable},
        "failures": failures,
    }
    atomic_json(output_path, result)
    return result
