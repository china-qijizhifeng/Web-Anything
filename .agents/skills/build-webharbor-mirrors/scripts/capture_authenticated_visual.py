#!/usr/bin/env python3
"""Capture bounded authenticated upstream screenshots without exposing storage state.

The storage-state file is credential material. This command reads it only when
creating a Playwright context and never copies its path or contents into output.
Personalized screenshots require an explicit user-authorized flag and remain
non-publication evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


SCHEMA_VERSION = 1
TOOL_VERSION = "webharbor-authenticated-visual-v1"
ROUTE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ALLOWED_STATES = {"anonymous", "authenticated"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--storage-state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--updated-storage-state", type=Path)
    parser.add_argument("--timeout-ms", type=int, default=45_000)
    parser.add_argument(
        "--allow-personalized-screenshots",
        action="store_true",
        help="Confirm explicit user authorization to retain personalized upstream pixels.",
    )
    return parser.parse_args()


def load_spec(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"invalid capture spec: {exc}") from exc
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(f"capture spec schema_version must be {SCHEMA_VERSION}")
    allowed_origin = str(payload.get("allowed_origin") or "").rstrip("/")
    parsed_origin = urlsplit(allowed_origin)
    if parsed_origin.scheme != "https" or not parsed_origin.hostname or parsed_origin.path:
        raise SystemExit("allowed_origin must be an HTTPS origin without a path")
    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        raise SystemExit("capture spec must contain routes")
    seen: set[str] = set()
    normalized = []
    for index, raw in enumerate(routes):
        if not isinstance(raw, dict):
            raise SystemExit(f"routes[{index}] must be an object")
        route_id = str(raw.get("id") or "")
        if not ROUTE_ID_RE.fullmatch(route_id) or route_id in seen:
            raise SystemExit(f"routes[{index}].id is invalid or duplicated")
        seen.add(route_id)
        url = str(raw.get("url") or "")
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
        if origin != allowed_origin:
            raise SystemExit(f"routes[{index}].url is outside allowed_origin")
        state = str(raw.get("state") or "authenticated")
        if state not in ALLOWED_STATES:
            raise SystemExit(f"routes[{index}].state must be anonymous or authenticated")
        viewport = raw.get("viewport") or {}
        try:
            width, height = int(viewport["width"]), int(viewport["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"routes[{index}].viewport must contain integer width/height") from exc
        if not (320 <= width <= 2560 and 480 <= height <= 2000):
            raise SystemExit(f"routes[{index}].viewport is outside the accepted bounds")
        interactions = raw.get("interactions") or []
        if not isinstance(interactions, list) or len(interactions) > 4:
            raise SystemExit(f"routes[{index}].interactions must contain at most four steps")
        for step in interactions:
            if not isinstance(step, dict) or step.get("action") != "click_first":
                raise SystemExit("only click_first interaction steps are accepted")
            selectors = step.get("selectors")
            if not isinstance(selectors, list) or not selectors or len(selectors) > 8:
                raise SystemExit("click_first selectors must contain one to eight selectors")
            if not all(isinstance(item, str) and 0 < len(item) <= 240 for item in selectors):
                raise SystemExit("click_first selectors must be bounded strings")
        normalized.append(
            {
                "id": route_id,
                "url": url,
                "state": state,
                "viewport": {"width": width, "height": height},
                "full_page": bool(raw.get("full_page", False)),
                "wait_ms": max(0, min(int(raw.get("wait_ms", 800)), 5_000)),
                "interactions": interactions,
                "authenticated_markers": [
                    item for item in raw.get("authenticated_markers", [])
                    if isinstance(item, str) and 0 < len(item) <= 240
                ][:8],
            }
        )
    return {"allowed_origin": allowed_origin, "routes": normalized}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_url(raw: str) -> str:
    parsed = urlsplit(raw)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def capture_geometry(page) -> dict:
    return page.evaluate(
        """() => {
          const selectors = [
            ['header', '[role="banner"], body > header'],
            ['primary_nav', '[aria-label="Global"], header nav, [role="banner"] nav'],
            ['main', 'main, [role="main"]'],
            ['aside', 'aside'],
            ['footer', 'footer, [role="contentinfo"]'],
            ['menu', '[role="menu"]']
          ];
          const result = {};
          for (const [name, selector] of selectors) {
            const el = document.querySelector(selector);
            if (!el) continue;
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            result[name] = {
              x: Math.round(rect.x), y: Math.round(rect.y),
              width: Math.round(rect.width), height: Math.round(rect.height),
              display: style.display, position: style.position,
              background_color: style.backgroundColor,
              color: style.color, border_color: style.borderColor,
              font_family: style.fontFamily, font_size: style.fontSize,
              line_height: style.lineHeight
            };
          }
          return {
            landmarks: result,
            document: {
              width: document.documentElement.scrollWidth,
              height: document.documentElement.scrollHeight,
              viewport_width: innerWidth,
              viewport_height: innerHeight
            },
            counts: {
              links: document.querySelectorAll('a[href]').length,
              buttons: document.querySelectorAll('button, [role="button"]').length,
              forms: document.forms.length,
              inputs: document.querySelectorAll('input, select, textarea').length,
              images: document.images.length
            }
          };
        }"""
    )


def click_first(page, selectors: list[str], timeout_ms: int) -> str:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                locator.click(timeout=min(timeout_ms, 8_000))
                return selector
        except Exception:
            continue
    raise RuntimeError("none of the authorized click_first selectors was visible")


def capture_route(browser, route: dict, state_path: Path, output: Path, timeout_ms: int) -> dict:
    context_options = {"viewport": route["viewport"]}
    if route["state"] == "authenticated":
        context_options["storage_state"] = str(state_path)
    context = browser.new_context(**context_options)
    page = context.new_page()
    requests: list[str] = []
    failures: list[dict] = []
    http_errors: list[dict] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("request", lambda request: requests.append(safe_url(request.url)))
    page.on(
        "requestfailed",
        lambda request: failures.append({"url": safe_url(request.url), "error": str(request.failure or "failed")[:160]}),
    )
    page.on(
        "response",
        lambda response: http_errors.append({"url": safe_url(response.url), "status": response.status})
        if response.status >= 400 else None,
    )
    page.on("console", lambda message: console_errors.append(message.text[:240]) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)[:240]))
    record = {
        "id": route["id"],
        "state": route["state"],
        "requested_path": urlsplit(route["url"]).path or "/",
        "final_path": None,
        "http_status": None,
        "viewport": route["viewport"],
        "interactions": [],
        "authenticated_marker_found": None,
        "screenshot": None,
        "geometry": None,
        "resources": {},
        "error": None,
    }
    try:
        response = page.goto(route["url"], wait_until="domcontentloaded", timeout=timeout_ms)
        record["http_status"] = response.status if response else None
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8_000))
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(route["wait_ms"])
        for step in route["interactions"]:
            used = click_first(page, step["selectors"], timeout_ms)
            record["interactions"].append({"action": "click_first", "matched_selector": used})
            page.wait_for_timeout(max(200, min(int(step.get("wait_ms", 500)), 3_000)))
        final = urlsplit(page.url)
        record["final_path"] = final.path or "/"
        markers = route["authenticated_markers"]
        if route["state"] == "authenticated":
            record["authenticated_marker_found"] = (
                any(page.locator(selector).count() > 0 for selector in markers)
                if markers else "/login" not in record["final_path"]
            )
            if not record["authenticated_marker_found"]:
                raise RuntimeError("authenticated route did not expose an accepted marker")
        screenshot = output / f"{route['id']}.png"
        page.screenshot(path=str(screenshot), full_page=route["full_page"], animations="disabled")
        os.chmod(screenshot, 0o600)
        record["screenshot"] = {
            "file": screenshot.name,
            "sha256": sha256_file(screenshot),
            "bytes": screenshot.stat().st_size,
            "full_page": route["full_page"],
        }
        record["geometry"] = capture_geometry(page)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        requested_origin = urlsplit(route["url"]).netloc.casefold()
        record["resources"] = {
            "request_count": len(requests),
            "external_origins": sorted(
                {urlsplit(url).netloc for url in requests if urlsplit(url).netloc.casefold() != requested_origin}
            ),
            "failed": failures[:100],
            "http_errors": http_errors[:100],
            "console_errors": console_errors[:100],
            "page_errors": page_errors[:100],
        }
        context.close()
    return record


def main() -> int:
    args = parse_args()
    if not args.allow_personalized_screenshots:
        raise SystemExit(
            "personalized upstream screenshots require explicit user authorization; "
            "rerun with --allow-personalized-screenshots only after receiving it"
        )
    spec_path = args.spec.expanduser().resolve()
    state_path = args.storage_state.expanduser().resolve()
    output = args.output.expanduser().resolve()
    updated_state = args.updated_storage_state.expanduser().resolve() if args.updated_storage_state else None
    if not state_path.is_file():
        raise SystemExit(f"storage state does not exist: {state_path}")
    if state_path.stat().st_mode & 0o077:
        raise SystemExit("storage state permissions must not grant group/other access")
    if state_path == spec_path or state_path == output or state_path == updated_state:
        raise SystemExit("storage state and output paths must differ")
    spec = load_spec(spec_path)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    browser_path = os.getenv("WEBHARBOR_CHROMIUM_EXECUTABLE")
    launch_options = {"headless": True, "args": ["--disable-dev-shm-usage"]}
    if browser_path:
        launch_options["executable_path"] = browser_path
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options)
        try:
            records = [capture_route(browser, route, state_path, output, args.timeout_ms) for route in spec["routes"]]
            authenticated_ok = any(
                item["state"] == "authenticated" and item.get("authenticated_marker_found")
                for item in records
            )
            if authenticated_ok and updated_state:
                updated_state.parent.mkdir(parents=True, exist_ok=True)
                context = browser.new_context(storage_state=str(state_path))
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=str(updated_state.parent), prefix=f".{updated_state.name}.", suffix=".tmp", delete=False
                    ) as handle:
                        temporary = Path(handle.name)
                    context.storage_state(path=str(temporary), indexed_db=False)
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, updated_state)
                finally:
                    context.close()
                    if "temporary" in locals():
                        temporary.unlink(missing_ok=True)
        finally:
            browser.close()
    passed = all(
        item.get("http_status") and 200 <= item["http_status"] < 400
        and not item.get("error") and item.get("screenshot")
        for item in records
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "status": "passed" if passed else "failed",
        "allowed_origin": spec["allowed_origin"],
        "personalized_visuals": True,
        "publication_allowed": False,
        "builder_use_only": True,
        "storage_state_path_recorded": False,
        "routes": records,
    }
    contract_path = output / "visual-contract.json"
    contract_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(contract_path, 0o600)
    print(json.dumps({
        "status": payload["status"], "routes": len(records),
        "contract": str(contract_path), "screenshots": sum(bool(item.get("screenshot")) for item in records),
    }, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
