#!/usr/bin/env python3
"""Execute bounded declarative browser steps against one local mirror."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from quality_contract import resolve_upload_fixture


BROWSER_ACTIONS = {
    "browser_navigate",
    "browser_fill",
    "browser_select",
    "browser_set_input_files",
    "browser_click",
    "browser_expect",
}


def _local_url(base_url: str, local_path: Any) -> str:
    if not isinstance(local_path, str) or not local_path.startswith("/"):
        raise ValueError("browser navigation requires a local absolute path")
    parsed = urlsplit(local_path)
    if parsed.scheme or parsed.netloc:
        raise ValueError("browser navigation cannot leave the local mirror")
    return urljoin(base_url.rstrip("/") + "/", local_path.lstrip("/"))


def verify_browser_steps(
    steps: list[dict[str, Any]],
    *,
    base_url: str,
    timeout: float,
    repository_root: Path | None = None,
    site: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Run browser actions with all non-local network traffic blocked."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    failures: list[str] = []
    results: list[dict[str, Any]] = []
    base = urlsplit(base_url)
    base_port = base.port or (443 if base.scheme == "https" else 80)
    timeout_ms = max(1, int(timeout * 1000))
    external_requests: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})

        def route_request(route) -> None:
            target = urlsplit(route.request.url)
            if target.scheme in {"data", "blob"} or (
                target.scheme in {"http", "https"}
                and target.scheme == base.scheme
                and target.hostname == base.hostname
                and (target.port or (443 if target.scheme == "https" else 80))
                == base_port
            ):
                route.continue_()
                return
            external_requests.append(route.request.url)
            route.abort()

        context.route("**/*", route_request)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        for index, step in enumerate(steps):
            action = step.get("action")
            if action not in BROWSER_ACTIONS:
                continue
            step_failures: list[str] = []
            fixture_result: dict[str, Any] | None = None
            try:
                if action == "browser_navigate":
                    response = page.goto(
                        _local_url(base_url, step.get("local_path")),
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    expected_status = step.get("expected_status", 200)
                    status = response.status if response is not None else None
                    if status != expected_status:
                        step_failures.append(
                            f"HTTP {status}, expected {expected_status}"
                        )
                    page.wait_for_timeout(100)
                elif action == "browser_fill":
                    page.locator(step["selector"]).fill(str(step.get("value", "")))
                elif action == "browser_select":
                    page.locator(step["selector"]).select_option(str(step["value"]))
                elif action == "browser_set_input_files":
                    if repository_root is None or site is None:
                        raise ValueError(
                            "browser file upload requires repository root and site"
                        )
                    fixture = step.get("fixture")
                    fixture_path = resolve_upload_fixture(
                        repository_root, site, fixture
                    )
                    page.locator(step["selector"]).set_input_files(str(fixture_path))
                    fixture_result = {
                        "path": fixture["path"],
                        "sha256": fixture["sha256"],
                        "bytes": fixture["bytes"],
                        "media_type": fixture["media_type"],
                        "synthetic": True,
                    }
                elif action == "browser_click":
                    locator = page.locator(step["selector"])
                    wait_for_url = step.get("wait_for_url")
                    if wait_for_url:
                        expected_url = _local_url(base_url, wait_for_url)
                        target = locator.evaluate(
                            "el => (el.closest('a') || el).getAttribute('target')"
                        )
                        if target == "_blank":
                            with page.expect_popup(timeout=timeout_ms) as popup_info:
                                locator.click()
                            page = popup_info.value
                            page.set_default_timeout(timeout_ms)
                            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                            page.wait_for_url(expected_url, timeout=timeout_ms)
                        else:
                            locator.click()
                            page.wait_for_url(expected_url, timeout=timeout_ms)
                    else:
                        locator.click()
                    page.wait_for_timeout(100)
                elif action == "browser_expect":
                    selector = step.get("selector")
                    locator = page.locator(selector) if selector else page.locator("body")
                    expected_visible = step.get("visible", True)
                    locator.wait_for(
                        state="visible" if expected_visible else "hidden",
                        timeout=timeout_ms,
                    )
                    expected_text = step.get("text")
                    if isinstance(expected_text, str) and expected_text not in locator.inner_text():
                        step_failures.append(f"visible text is missing: {expected_text!r}")
                    for name, expected_value in step.get("attributes", {}).items():
                        actual_value = locator.get_attribute(name)
                        if actual_value != expected_value:
                            step_failures.append(
                                f"attribute {name}={actual_value!r}, expected {expected_value!r}"
                            )
                    expected_url = step.get("url")
                    if isinstance(expected_url, str) and page.url != _local_url(base_url, expected_url):
                        step_failures.append(
                            f"URL {page.url!r}, expected {_local_url(base_url, expected_url)!r}"
                        )
            except (KeyError, ValueError, PlaywrightError, PlaywrightTimeoutError) as exc:
                step_failures.append(str(exc))
            failures.extend(f"browser step {index}: {failure}" for failure in step_failures)
            result = {
                "index": index,
                "action": action,
                "url": page.url,
                "status": "failed" if step_failures else "passed",
                "failures": step_failures,
            }
            if fixture_result is not None:
                result["fixture"] = fixture_result
            results.append(result)
        context.close()
        browser.close()
    if external_requests:
        failures.append(
            f"browser workflow attempted {len(external_requests)} external request(s)"
        )
    return failures, results
