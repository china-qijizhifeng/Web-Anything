#!/usr/bin/env python3
"""Serialize meaningful SPA DOM using only hash-verified captured resources."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urldefrag

from playwright.sync_api import Browser, Error as PlaywrightError, Route

from capture_upstream import canonical_prerender_resource_url
from page_content_policy import ERROR_PAGE_MARKERS


TOOL_VERSION = "webharbor-captured-dom-prerender-v7"
MAX_BLOCKED_REQUEST_RECORDS = 2048
PRERENDER_VIEWPORT = {"width": 1440, "height": 900}


class PrerenderError(RuntimeError):
    pass


class StaticStructureParser(HTMLParser):
    """Measure useful body structure without executing page scripts."""

    IGNORED_TAGS = {"script", "style", "template", "noscript", "svg"}
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body_depth = 0
        self.ignored_depth = 0
        self.landmarks = 0
        self.main_landmarks = 0
        self.main_depth = 0
        self.main_content_elements = 0
        self.main_text_parts: list[str] = []
        self.headings = 0
        self.forms = 0
        self.content_elements = 0
        self.links = 0
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "body":
            self.body_depth += 1
        if tag in self.IGNORED_TAGS:
            self.ignored_depth += 1
            return
        if not self.body_depth or self.ignored_depth:
            return
        if tag in {"header", "nav", "main", "footer"}:
            self.landmarks += 1
        if tag == "main":
            self.main_landmarks += 1
            self.main_depth += 1
        if tag in {"h1", "h2", "h3"}:
            self.headings += 1
        if tag == "form":
            self.forms += 1
        if tag in {"a", "button", "form", "h1", "h2", "h3", "img", "input", "select"}:
            self.content_elements += 1
            if self.main_depth:
                self.main_content_elements += 1
        if tag == "a":
            self.links += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if tag == "body" and self.body_depth:
            self.body_depth -= 1
        if tag == "main" and self.main_depth:
            self.main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.body_depth and not self.ignored_depth:
            value = " ".join(data.split())
            if value:
                self.text_parts.append(value)
                if self.main_depth:
                    self.main_text_parts.append(value)

    def summary(self) -> dict[str, int | bool]:
        body_text_bytes = len(" ".join(self.text_parts).encode("utf-8"))
        main_text_bytes = len(" ".join(self.main_text_parts).encode("utf-8"))
        meaningful = bool(
            (
                self.main_landmarks
                and (self.main_content_elements or main_text_bytes >= 64)
            )
            or self.headings >= 2
            or self.forms
            or body_text_bytes >= 512
        )
        return {
            "meaningful": meaningful,
            "landmarks": self.landmarks,
            "main_landmarks": self.main_landmarks,
            "main_content_elements": self.main_content_elements,
            "main_text_bytes": main_text_bytes,
            "headings": self.headings,
            "forms": self.forms,
            "content_elements": self.content_elements,
            "links": self.links,
            "body_text_bytes": body_text_bytes,
        }


def static_structure(body: bytes | str) -> dict[str, int | bool]:
    parser = StaticStructureParser()
    parser.feed(body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body)
    parser.close()
    return parser.summary()


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def verified_body(capture_root: Path, record: dict[str, Any]) -> bytes:
    local_path = record.get("local_path")
    expected = record.get("sha256")
    if not isinstance(local_path, str) or not isinstance(expected, str):
        raise PrerenderError("captured resource lacks a local path or SHA256")
    path = (capture_root / local_path).resolve()
    if capture_root.resolve() not in path.parents or not path.is_file():
        raise PrerenderError(f"captured resource is missing or outside scope: {local_path}")
    body = path.read_bytes()
    if sha256_bytes(body) != expected:
        raise PrerenderError(f"captured resource hash changed: {local_path}")
    return body


def effective_page_records(capture: dict[str, Any]) -> list[dict[str, Any]]:
    """Substitute a rendered DOM only when it traces to the current raw page hash."""
    rendered = {
        (record.get("url"), record.get("source_page_sha256")): record
        for record in capture.get("rendered_pages", [])
        if record.get("status") == "passed"
    }
    return [
        rendered.get((record.get("url"), record.get("sha256")), record)
        for record in capture.get("pages", [])
    ]


def pages_requiring_prerender(
    capture_root: Path, capture: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        record
        for record in capture.get("pages", [])
        if not static_structure(verified_body(capture_root, record))["meaningful"]
    ]


def prerender_evidence_valid(capture_root: Path, capture: dict[str, Any]) -> bool:
    evidence = capture.get("captured_dom_prerender")
    if not isinstance(evidence, dict) or evidence.get("tool") != TOOL_VERSION:
        return False
    rendered = capture.get("rendered_pages")
    if not isinstance(rendered, list):
        return False
    shell_pages = pages_requiring_prerender(capture_root, capture)
    resource_attempts = evidence.get("resource_attempts")
    if (
        not isinstance(resource_attempts, list)
        or int(evidence.get("resource_attempted", -1)) != len(capture.get("pages", []))
        or len(resource_attempts) != len(capture.get("pages", []))
    ):
        return False
    if int(evidence.get("attempted", -1)) != len(shell_pages):
        return False
    if int(evidence.get("passed", -1)) != len(shell_pages):
        return False
    if len(rendered) != len(shell_pages):
        return False
    source_hashes = {
        (record.get("url"), record.get("sha256"))
        for record in capture.get("pages", [])
    }
    try:
        return all(
            record.get("status") == "passed"
            and (record.get("url"), record.get("source_page_sha256")) in source_hashes
            and verified_body(capture_root, record) is not None
            for record in rendered
        )
    except PrerenderError:
        return False


class CapturedResourceRouter:
    def __init__(self, capture_root: Path, capture: dict[str, Any]) -> None:
        self.resources: dict[str, dict[str, Any]] = {}
        self.used: dict[str, str] = {}
        self.blocked: dict[tuple[str, str, str], dict[str, str]] = {}
        for kind in ("pages", "assets"):
            for record in capture.get(kind, []):
                body = verified_body(capture_root, record)
                resource = {
                    "body": body,
                    "content_type": record.get("content_type") or "application/octet-stream",
                    "sha256": record["sha256"],
                    "status": int(record.get("status", 200)),
                }
                for key in ("url", "final_url"):
                    value = record.get(key)
                    if isinstance(value, str):
                        self.resources[urldefrag(value).url] = resource
                        self.resources[
                            canonical_prerender_resource_url(urldefrag(value).url)
                        ] = resource

    def record_block(self, method: str, resource_type: str, url: str, reason: str) -> None:
        key = (method, resource_type, url)
        if key not in self.blocked:
            self.blocked[key] = {
                "method": method,
                "resource_type": resource_type,
                "url": url,
                "reason": reason,
            }

    def handle(self, route: Route) -> None:
        request = route.request
        method = request.method.upper()
        url = urldefrag(request.url).url
        if method != "GET":
            self.record_block(method, request.resource_type, url, "non_get")
            route.abort("blockedbyclient")
            return
        resource = self.resources.get(url) or self.resources.get(
            canonical_prerender_resource_url(url)
        )
        if resource is None:
            self.record_block(method, request.resource_type, url, "uncaptured")
            route.abort("blockedbyclient")
            return
        self.used[url] = resource["sha256"]
        route.fulfill(
            status=resource["status"],
            headers={
                "content-type": resource["content_type"],
                "cache-control": "no-store",
                "access-control-allow-origin": "*",
            },
            body=resource["body"],
        )


def browser_structure(page) -> dict[str, Any]:
    return page.evaluate(
        """() => ({
          landmarks: document.querySelectorAll('header,nav,main,footer').length,
          main_landmarks: document.querySelectorAll('main').length,
          main_content_elements: document.querySelector('main')?.querySelectorAll('a,button,form,h1,h2,h3,img,input,select').length || 0,
          main_text_bytes: new TextEncoder().encode(document.querySelector('main')?.innerText || '').length,
          headings: document.querySelectorAll('h1,h2,h3').length,
          forms: document.querySelectorAll('form').length,
          content_elements: document.querySelectorAll('a,button,form,h1,h2,h3,img,input,select').length,
          links: document.querySelectorAll('a[href]').length,
          body_text_bytes: new TextEncoder().encode(document.body?.innerText || '').length,
        })"""
    )


def meaningful_browser_structure(structure: dict[str, Any]) -> bool:
    return bool(
        (
            int(structure.get("main_landmarks", 0)) > 0
            and (
                int(structure.get("main_content_elements", 0)) > 0
                or int(structure.get("main_text_bytes", 0)) >= 64
            )
        )
        or int(structure.get("headings", 0)) >= 2
        or int(structure.get("forms", 0)) > 0
        or int(structure.get("body_text_bytes", 0)) >= 512
        or (
            int(structure.get("landmarks", 0)) >= 2
            and int(structure.get("content_elements", 0)) >= 40
            and int(structure.get("links", 0)) >= 20
            and int(structure.get("body_text_bytes", 0)) >= 256
        )
    )


def browser_interstitial_markers(page) -> list[str]:
    return page.evaluate(
        """markers => {
          const text = (document.body?.innerText || '').toLowerCase();
          return markers.filter(marker => text.includes(marker));
        }""",
        list(ERROR_PAGE_MARKERS),
    )


def render_page(
    browser: Browser,
    *,
    capture_root: Path,
    capture: dict[str, Any],
    page_record: dict[str, Any],
) -> tuple[dict[str, Any], bytes | None]:
    router = CapturedResourceRouter(capture_root, capture)
    context = browser.new_context(
        viewport=PRERENDER_VIEWPORT,
        device_scale_factor=1,
        java_script_enabled=True,
        service_workers="block",
        accept_downloads=False,
        reduced_motion="reduce",
    )
    context.route("**/*", router.handle)
    if hasattr(context, "route_web_socket"):
        def block_web_socket(web_socket) -> None:
            router.record_block("GET", "websocket", web_socket.url, "uncaptured_websocket")
            web_socket.close()

        context.route_web_socket("**/*", block_web_socket)
    page = context.new_page()
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    try:
        response = page.goto(
            page_record["url"], wait_until="domcontentloaded", timeout=60_000
        )
        page.wait_for_timeout(2_000)
        page.evaluate("window.stop()")
        page.wait_for_timeout(250)
        structure = browser_structure(page)
        interstitial_markers = browser_interstitial_markers(page)
        dom = page.content().encode("utf-8")
        passed = bool(
            response
            and response.status == 200
            and meaningful_browser_structure(structure)
            and not interstitial_markers
        )
        blocked = sorted(
            router.blocked.values(),
            key=lambda item: (item["reason"], item["method"], item["url"]),
        )
        result = {
            "status": (
                "passed"
                if passed
                else "error_interstitial"
                if interstitial_markers
                else "not_meaningful"
            ),
            "url": page_record["url"],
            "final_url": page_record["final_url"],
            "source_page_sha256": page_record["sha256"],
            "http_status": response.status if response else None,
            "structure": structure,
            "interstitial_markers": interstitial_markers,
            "resource_sha256s": [
                {"url": url, "sha256": digest}
                for url, digest in sorted(router.used.items())
            ],
            "blocked_request_count": len(blocked),
            "blocked_requests": blocked[:MAX_BLOCKED_REQUEST_RECORDS],
            "blocked_requests_truncated": len(blocked) > MAX_BLOCKED_REQUEST_RECORDS,
            "console_errors": console_errors[:20],
        }
        if passed:
            result.update({
                "content_type": "text/html",
                "bytes": len(dom),
                "sha256": sha256_bytes(dom),
                "dom_sha256": sha256_bytes(dom),
            })
            return result, dom
        return result, None
    except PlaywrightError as exc:
        return ({
            "status": "failed",
            "url": page_record.get("url"),
            "final_url": page_record.get("final_url"),
            "source_page_sha256": page_record.get("sha256"),
            "error": str(exc),
            "blocked_request_count": len(router.blocked),
            "blocked_requests": list(router.blocked.values())[:MAX_BLOCKED_REQUEST_RECORDS],
            "resource_sha256s": [
                {"url": url, "sha256": digest}
                for url, digest in sorted(router.used.items())
            ],
        }, None)
    finally:
        context.close()


def audit_page_resources(
    browser: Browser,
    *,
    capture_root: Path,
    capture: dict[str, Any],
    page_record: dict[str, Any],
) -> dict[str, Any]:
    """Observe one desktop page's static resource graph without live network."""
    router = CapturedResourceRouter(capture_root, capture)
    context = browser.new_context(
        viewport=PRERENDER_VIEWPORT,
        device_scale_factor=1,
        java_script_enabled=False,
        service_workers="block",
        accept_downloads=False,
        reduced_motion="reduce",
    )
    context.route("**/*", router.handle)
    page = context.new_page()
    try:
        response = page.goto(
            page_record["url"], wait_until="domcontentloaded", timeout=60_000
        )
        page.wait_for_timeout(250)
        # Trigger native lazy-loaded images over the same bounded desktop range
        # used by fidelity verification. Page evaluation remains available to
        # Playwright even when page-authored JavaScript is disabled.
        page.evaluate(
            """async () => {
              const step = 720;
              const limit = Math.min(document.documentElement.scrollHeight, 7200);
              for (let y = step; y <= limit; y += step) {
                window.scrollTo(0, y);
                await new Promise(resolve => setTimeout(resolve, 25));
              }
              window.scrollTo(0, 0);
            }"""
        )
        page.wait_for_timeout(100)
        blocked = sorted(
            router.blocked.values(),
            key=lambda item: (item["reason"], item["method"], item["url"]),
        )
        return {
            "status": "passed" if response and response.status == 200 else "failed",
            "url": page_record["url"],
            "final_url": page_record["final_url"],
            "source_page_sha256": page_record["sha256"],
            "http_status": response.status if response else None,
            "javascript_enabled": False,
            "resource_sha256s": [
                {"url": url, "sha256": digest}
                for url, digest in sorted(router.used.items())
            ],
            "blocked_request_count": len(blocked),
            "blocked_requests": blocked[:MAX_BLOCKED_REQUEST_RECORDS],
            "blocked_requests_truncated": len(blocked) > MAX_BLOCKED_REQUEST_RECORDS,
        }
    except PlaywrightError as exc:
        return {
            "status": "failed",
            "url": page_record.get("url"),
            "final_url": page_record.get("final_url"),
            "source_page_sha256": page_record.get("sha256"),
            "javascript_enabled": False,
            "error": str(exc),
            "blocked_request_count": len(router.blocked),
            "blocked_requests": list(router.blocked.values())[
                :MAX_BLOCKED_REQUEST_RECORDS
            ],
            "blocked_requests_truncated": (
                len(router.blocked) > MAX_BLOCKED_REQUEST_RECORDS
            ),
        }
    finally:
        context.close()


def prerender_captured_pages(
    browser: Browser,
    *,
    capture_root: Path,
    capture: dict[str, Any],
    rendered_output_root: Path | None = None,
    rendered_relative_root: Path = Path("rendered"),
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Audit every page's resources and render only static SPA shells."""
    attempts: list[dict[str, Any]] = []
    page_records = list(capture.get("pages", []))
    resource_attempts: list[dict[str, Any]] = []
    for index, page_record in enumerate(page_records, start=1):
        if progress:
            progress({
                "phase": "resource_audit",
                "event": "started",
                "index": index,
                "total": len(page_records),
                "url": page_record.get("url"),
            })
        attempt = audit_page_resources(
            browser,
            capture_root=capture_root,
            capture=capture,
            page_record=page_record,
        )
        resource_attempts.append(attempt)
        if progress:
            progress({
                "phase": "resource_audit",
                "event": "completed",
                "index": index,
                "total": len(page_records),
                "url": page_record.get("url"),
                "status": attempt.get("status"),
            })
    rendered_pages: list[dict[str, Any]] = []
    shell_records = pages_requiring_prerender(capture_root, capture)
    rendered_root = rendered_output_root or (capture_root / rendered_relative_root)
    for index, page_record in enumerate(shell_records, start=1):
        if progress:
            progress({
                "phase": "shell_render",
                "event": "started",
                "index": index,
                "total": len(shell_records),
                "url": page_record.get("url"),
            })
        source_structure = static_structure(verified_body(capture_root, page_record))
        result, dom = render_page(
            browser,
            capture_root=capture_root,
            capture=capture,
            page_record=page_record,
        )
        result["source_structure"] = source_structure
        if dom is not None:
            filename = (
                hashlib.sha256(page_record["url"].encode("utf-8")).hexdigest()[:20]
                + ".html"
            )
            local_path = rendered_relative_root / filename
            destination = rendered_root / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(dom)
            result["local_path"] = local_path.as_posix()
            rendered_pages.append(dict(result))
        attempts.append(result)
        if progress:
            progress({
                "phase": "shell_render",
                "event": "completed",
                "index": index,
                "total": len(shell_records),
                "url": page_record.get("url"),
                "status": result.get("status"),
            })
    return {
        "tool": TOOL_VERSION,
        "mode": "hash_verified_captured_resources_only",
        "viewport": PRERENDER_VIEWPORT,
        "network_guardrails": {
            "http": "all requests routed through captured SHA256 resource map",
            "non_get": "blocked",
            "service_workers": "blocked",
            "websockets": "blocked",
            "uncaptured": "blocked",
        },
        "attempted": len(attempts),
        "passed": len(rendered_pages),
        "failed": len(attempts) - len(rendered_pages),
        "attempts": attempts,
        "resource_attempted": len(resource_attempts),
        "resource_failed": sum(
            attempt.get("status") != "passed" for attempt in resource_attempts
        ),
        "resource_attempts": resource_attempts,
        "rendered_pages": rendered_pages,
    }
