#!/usr/bin/env python3
"""Capture matched upstream/local screenshots and write fidelity evidence."""

from __future__ import annotations

import argparse
import html
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from prerender_snapshot import effective_page_records
from page_content_policy import ERROR_PAGE_MARKERS
from fidelity_contract import FIDELITY_CONTRACT_VERSION, TOOL_VERSION
from site_fingerprint import site_implementation_fingerprint


MISSING_ASSET_PATH = "/__webharbor_missing_asset"
CRITICAL_RESOURCE_TYPES = {
    "document", "stylesheet", "font", "image", "media", "script", "xhr", "fetch"
}
IFRAME_START_TAG_RE = re.compile(r"<iframe\b[^>]*>", re.IGNORECASE | re.DOTALL)
IFRAME_SRC_ATTR_RE = re.compile(
    r'''\s+src\s*=\s*(?:(?P<quote>["'])(?P<quoted>.*?)(?P=quote)|(?P<bare>[^\s>]+))''',
    re.IGNORECASE | re.DOTALL,
)
HIDDEN_FRAME_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)(?:\s*!important)?\s*(?:;|$)",
    re.IGNORECASE,
)


class FidelityError(RuntimeError):
    pass


class _IframeTagInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attrs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "iframe":
            self.attrs = attrs


def neutralize_hidden_uncaptured_iframes(
    body: bytes, source_url: str, captured_urls: set[str]
) -> tuple[bytes, int]:
    """Apply the compiler's strict hidden-frame rule before replay requests fire."""
    text = body.decode("utf-8", errors="replace")
    neutralized = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal neutralized
        tag = match.group(0)
        inspector = _IframeTagInspector()
        inspector.feed(tag)
        attributes = {name.casefold(): value for name, value in inspector.attrs}
        source = attributes.get("src")
        if not source:
            return tag
        normalized = urldefrag(urljoin(source_url, html.unescape(source))).url
        hidden = (
            "hidden" in attributes
            or (
                str(attributes.get("width", "")).strip() == "0"
                and str(attributes.get("height", "")).strip() == "0"
            )
            or bool(HIDDEN_FRAME_STYLE_RE.search(attributes.get("style") or ""))
        )
        if not hidden or normalized in captured_urls:
            return tag
        source_match = IFRAME_SRC_ATTR_RE.search(tag)
        if source_match is None:
            return tag
        rewritten = tag[: source_match.start()] + tag[source_match.end() :]
        if "data-webharbor-workflow" not in attributes:
            close_at = rewritten.rfind("/>")
            if close_at < 0:
                close_at = rewritten.rfind(">")
            rewritten = (
                rewritten[:close_at]
                + ' data-webharbor-workflow="hidden-frame-removed"'
                + rewritten[close_at:]
            )
        neutralized += 1
        return rewritten

    rewritten = IFRAME_START_TAG_RE.sub(replace, text)
    return rewritten.encode("utf-8"), neutralized


class CapturedReplay:
    """Fulfill browser requests only from hash-verified upstream capture bytes."""

    def __init__(self, root: Path, site: str, evidence: dict[str, Any]) -> None:
        self.capture_root = (
            root / "sites" / site / "static" / "external_cache" / "upstream"
        ).resolve()
        self.resources: dict[str, dict[str, Any]] = {}
        self.misses: set[str] = set()
        self.miss_details: set[tuple[str, str]] = set()
        self.rendered_pages = len(evidence.get("rendered_pages", []))
        records = [
            *(('pages', record) for record in effective_page_records(evidence)),
            *(('assets', record) for record in evidence.get("assets", [])),
        ]
        captured_urls = {
            urldefrag(value).url
            for _, record in records
            for key in ("url", "final_url")
            if isinstance((value := record.get(key)), str)
        }
        for kind, record in records:
            local_path = record.get("local_path")
            expected_hash = record.get("sha256")
            if not isinstance(local_path, str) or not isinstance(expected_hash, str):
                raise FidelityError(f"capture {kind} record is missing path or SHA256")
            path = (self.capture_root / local_path).resolve()
            if self.capture_root not in path.parents or not path.is_file():
                raise FidelityError(f"captured resource is missing or outside scope: {local_path}")
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != expected_hash:
                raise FidelityError(f"captured resource hash changed: {local_path}")
            if kind == "pages" and str(record.get("content_type", "")).startswith("text/html"):
                source_url = record.get("final_url") or record.get("url")
                if isinstance(source_url, str):
                    body, _ = neutralize_hidden_uncaptured_iframes(
                        body, source_url, captured_urls
                    )
            resource = {
                "body": body,
                "content_type": record.get("content_type") or "application/octet-stream",
                "sha256": expected_hash,
            }
            for key in ("url", "final_url"):
                url = record.get(key)
                if isinstance(url, str):
                    self.resources[urldefrag(url).url] = resource

    def handle(self, route) -> None:
        url = urldefrag(route.request.url).url
        resource = self.resources.get(url)
        if resource is None:
            self.misses.add(url)
            self.miss_details.add((
                getattr(route.request, "resource_type", "other"),
                url,
            ))
            route.abort("blockedbyclient")
            return
        body = resource["body"]
        headers = {
            "content-type": resource["content_type"],
            "cache-control": "no-store",
            "accept-ranges": "bytes",
        }
        range_header = getattr(route.request, "headers", {}).get("range")
        if range_header:
            resolved = resolve_single_byte_range(range_header, len(body))
            if resolved is None:
                headers["content-range"] = f"bytes */{len(body)}"
                route.fulfill(status=416, headers=headers, body=b"")
                return
            start, stop = resolved
            headers["content-range"] = f"bytes {start}-{stop - 1}/{len(body)}"
            headers["content-length"] = str(stop - start)
            route.fulfill(status=206, headers=headers, body=body[start:stop])
            return
        headers["content-length"] = str(len(body))
        route.fulfill(status=200, headers=headers, body=body)


def resolve_single_byte_range(value: str, length: int) -> tuple[int, int] | None:
    """Resolve one RFC-style byte range to a half-open interval."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip(), re.IGNORECASE)
    if match is None or length <= 0:
        return None
    start_text, end_text = match.groups()
    if not start_text:
        if not end_text:
            return None
        suffix_length = int(end_text)
        if suffix_length <= 0:
            return None
        return max(length - suffix_length, 0), length
    start = int(start_text)
    if start >= length:
        return None
    if not end_text:
        return start, length
    end = int(end_text)
    if end < start:
        return None
    return start, min(end + 1, length)


class HistoricalStructureParser(HTMLParser):
    """Collect the browser-structure fields used by the fidelity gate."""

    IGNORED = {"script", "style", "svg", "template", "noscript"}
    CONTENT = {"a", "button", "form", "h1", "h2", "h3", "img", "input", "select"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body_depth = 0
        self.main_depth = 0
        self.ignored_depth = 0
        self.heading_tag: str | None = None
        self.heading_parts: list[str] = []
        self.headings: list[str] = []
        self.h1: list[str] = []
        self.forms = 0
        self.links = 0
        self.images = 0
        self.has_header = False
        self.has_nav = False
        self.has_main = False
        self.has_footer = False
        self.main_content_elements = 0
        self.body_parts: list[str] = []
        self.main_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "body":
            self.body_depth += 1
        if tag in self.IGNORED:
            self.ignored_depth += 1
            return
        if not self.body_depth or self.ignored_depth:
            return
        if tag == "header":
            self.has_header = True
        elif tag == "nav":
            self.has_nav = True
        elif tag == "main":
            self.has_main = True
            self.main_depth += 1
        elif tag == "footer":
            self.has_footer = True
        if tag == "form":
            self.forms += 1
        elif tag == "a":
            self.links += 1
        elif tag == "img":
            self.images += 1
        if self.main_depth and tag in self.CONTENT:
            self.main_content_elements += 1
        if tag in {"h1", "h2", "h3"}:
            self.heading_tag = tag
            self.heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.IGNORED and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if tag == self.heading_tag:
            value = " ".join(" ".join(self.heading_parts).split())
            if value:
                self.headings.append(value)
                if tag == "h1":
                    self.h1.append(value)
            self.heading_tag = None
            self.heading_parts = []
        if tag == "main" and self.main_depth:
            self.main_depth -= 1
        if tag == "body" and self.body_depth:
            self.body_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.body_depth or self.ignored_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        self.body_parts.append(value)
        if self.main_depth:
            self.main_parts.append(value)
        if self.heading_tag:
            self.heading_parts.append(value)

    def result(self) -> dict[str, Any]:
        return {
            "title": "",
            "headings": self.headings,
            "h1": self.h1,
            "forms": self.forms,
            "links": self.links,
            "images": self.images,
            "hasHeader": self.has_header,
            "hasNav": self.has_nav,
            "hasMain": self.has_main,
            "mainContentElements": self.main_content_elements,
            "mainTextBytes": len(" ".join(self.main_parts).encode("utf-8")),
            "hasFooter": self.has_footer,
            "bodyTextBytes": len(" ".join(self.body_parts).encode("utf-8")),
            "interstitialMarkers": [],
        }


class HistoricalReference:
    """Load a hash-verified screenshot and structural HTML from historical evidence."""

    def __init__(self, root: Path, site: str, evidence_path: Path) -> None:
        self.root = root.resolve()
        self.site = site
        self.evidence_path = evidence_path.resolve()
        self.evidence = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        if self.evidence.get("site") != site or self.evidence.get("status") != "passed":
            raise FidelityError("historical reference requires passing evidence for the same site")
        if self.evidence.get("source_mode") != "historical_benchmark_snapshot":
            raise FidelityError("historical reference evidence has the wrong source mode")
        site_cache = (root / "sites" / site / "static" / "external_cache").resolve()
        screenshot = self.evidence.get("screenshot")
        if not isinstance(screenshot, dict):
            raise FidelityError("historical reference evidence is missing screenshot metadata")
        self.screenshot_path = (root / screenshot.get("local_path", "")).resolve()
        if site_cache not in self.screenshot_path.parents or not self.screenshot_path.is_file():
            raise FidelityError("historical reference screenshot is missing or outside site cache")
        if sha256_file(self.screenshot_path) != screenshot.get("sha256"):
            raise FidelityError("historical reference screenshot hash changed")
        with Image.open(self.screenshot_path) as image:
            self.width, self.height = image.size
        if (self.width, self.height) != (screenshot.get("width"), screenshot.get("height")):
            raise FidelityError("historical reference screenshot dimensions changed")
        pages = self.evidence.get("pages")
        if not isinstance(pages, list) or len(pages) != 1:
            raise FidelityError("historical reference must contain exactly one structural page")
        page = pages[0]
        capture_root = (root / "sites" / site / "static" / "external_cache" / "upstream").resolve()
        html_path = (capture_root / page.get("local_path", "")).resolve()
        if capture_root not in html_path.parents or not html_path.is_file():
            raise FidelityError("historical reference HTML is missing or outside capture cache")
        body = html_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != page.get("sha256"):
            raise FidelityError("historical reference HTML hash changed")
        parser = HistoricalStructureParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
        self.structure = parser.result()
        self.page_url = page.get("final_url") or page.get("url")

    def prepare(
        self,
        output: Path,
        viewport: dict[str, int],
        crop: dict[str, Any] | None,
    ) -> dict[str, Any]:
        configured = crop or {}
        x = int(configured.get("x", 0))
        y = int(configured.get("y", 0))
        width = int(configured.get("width", viewport["width"]))
        height = int(configured.get("height", viewport["height"]))
        if (width, height) != (viewport["width"], viewport["height"]):
            raise FidelityError("historical reference crop must match the declared viewport")
        if x < 0 or y < 0 or x + width > self.width or y + height > self.height:
            raise FidelityError("historical reference crop exceeds screenshot bounds")
        output.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(self.screenshot_path).convert("RGB") as image:
            image.crop((x, y, x + width, y + height)).save(output, format="PNG")
        return {
            "url": self.page_url,
            "http_status": None,
            "screenshot_sha256": sha256_file(output),
            "screenshot_bytes": output.stat().st_size,
            "nonblank_score": nonblank_score(output),
            "structure": self.structure,
            "external_requests": [],
            "blocked_uncaptured_requests": [],
            "blocked_uncaptured_request_details": [],
            "source_mode": "historical_benchmark_snapshot",
            "javascript_enabled": None,
            "baseline_normalizations": {},
            "console_errors": [],
            "render_integrity": {
                "source_kind": "pinned_historical_screenshot",
                "resource_check": "not_applicable_to_static_reference",
                "imageCount": 0,
                "visibleBrokenImageCount": 0,
                "visibleBrokenImages": [],
                "stylesheetLinkCount": 0,
                "missingStylesheetCount": 0,
                "missingStylesheets": [],
                "missing_placeholder_requests": [],
                "request_failures": [],
                "http_errors": [],
                "csp_errors": [],
                "settle": {
                    "stable": True,
                    "mode": "pinned_static_reference",
                },
            },
            "reference": {
                "evidence_sha256": sha256_file(self.evidence_path),
                "source_screenshot_sha256": sha256_file(self.screenshot_path),
                "source_dimensions": {"width": self.width, "height": self.height},
                "crop": {"x": x, "y": y, "width": width, "height": height},
            },
        }


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pixel_similarity(first: Path, second: Path) -> float:
    left = Image.open(first).convert("RGB")
    right = Image.open(second).convert("RGB")
    if left.size != right.size:
        raise FidelityError(f"screenshot sizes differ: {left.size} != {right.size}")
    difference = ImageChops.difference(left, right)
    histogram = difference.histogram()
    total = left.width * left.height * 3 * 255
    distance = sum((index % 256) * count for index, count in enumerate(histogram)) / total
    return round(1.0 - distance, 6)


def nonblank_score(path: Path) -> float:
    image = Image.open(path).convert("RGB")
    return round(sum(ImageStat.Stat(image).stddev) / 3, 4)


def heading_overlap(upstream: dict[str, Any], local: dict[str, Any]) -> float:
    first = {value.strip().casefold() for value in upstream.get("headings", []) if value.strip()}
    second = {value.strip().casefold() for value in local.get("headings", []) if value.strip()}
    return round(len(first & second) / max(1, len(first | second)), 6)


def nonblank_retention(upstream_score: float, local_score: float) -> float:
    if upstream_score <= 0:
        return 1.0 if local_score <= 0 else 0.0
    return round(local_score / upstream_score, 6)


def settle_page(page) -> dict[str, Any]:
    """Wait for fonts and bounded lazy loading, then restore the desktop viewport."""
    if hasattr(page, "wait_for_load_state"):
        try:
            page.wait_for_load_state("networkidle", timeout=3_000)
        except PlaywrightTimeoutError:
            pass
    last_error = ""
    navigation_context_resets = 0
    max_scroll_steps = 12
    for attempt in range(3):
        try:
            if hasattr(page, "wait_for_function"):
                try:
                    page.wait_for_function(
                        "() => !document.fonts || document.fonts.status === 'loaded'",
                        timeout=3_000,
                    )
                except PlaywrightTimeoutError:
                    pass
            before = page.evaluate(
                """() => ({
                  readyState: document.readyState,
                  beforeHeight: document.documentElement.scrollHeight,
                  viewportHeight: Math.max(window.innerHeight || 0, 1),
                  fontsStatus: document.fonts?.status || null,
                })"""
            )
            before = before if isinstance(before, dict) else {}
            viewport_height = max(int(before.get("viewportHeight", 1)), 1)
            before_height = max(int(before.get("beforeHeight", viewport_height)), viewport_height)
            scroll_step = max(500, int(viewport_height * 0.75))
            scroll_limit = min(
                max(before_height, viewport_height * 4),
                viewport_height * 8,
            )
            scroll_step_count = 0
            y = 0
            while y <= scroll_limit and scroll_step_count < max_scroll_steps:
                page.evaluate("y => window.scrollTo(0, y)", y)
                if hasattr(page, "wait_for_timeout"):
                    page.wait_for_timeout(100)
                y += scroll_step
                scroll_step_count += 1
            page.evaluate(
                "limit => window.scrollTo(0, Math.min(document.documentElement.scrollHeight, limit))",
                scroll_limit,
            )
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(400)
            page.evaluate("() => window.scrollTo(0, 0)")
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(300)
            after = page.evaluate(
                """() => ({
                  readyState: document.readyState,
                  afterHeight: document.documentElement.scrollHeight,
                  fontsStatus: document.fonts?.status || null,
                })"""
            )
            after = after if isinstance(after, dict) else {}
            return {
                "stable": True,
                "readyState": after.get("readyState", before.get("readyState")),
                "beforeHeight": before_height,
                "afterHeight": int(after.get("afterHeight", before_height)),
                "fontsStatus": after.get("fontsStatus", before.get("fontsStatus")),
                "scrollLimit": scroll_limit,
                "scrollStepCount": scroll_step_count,
                "navigationContextResets": navigation_context_resets,
            }
        except PlaywrightError as exc:
            last_error = str(exc)
            if "Execution context was destroyed" not in last_error:
                raise
            navigation_context_resets += 1
            if attempt < 2:
                if hasattr(page, "wait_for_load_state"):
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=2_000)
                    except PlaywrightTimeoutError:
                        pass
                if hasattr(page, "wait_for_timeout"):
                    page.wait_for_timeout(250)
                continue
    return {
        "stable": False,
        "error": last_error,
        "navigationContextResets": navigation_context_resets,
    }


def resource_integrity(page) -> dict[str, Any]:
    value = page.evaluate(
        """missingPath => {
          const visible = element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number.parseFloat(style.opacity || '1') > 0
              && rect.width > 1
              && rect.height > 1;
          };
          const images = [...document.images];
          const brokenImages = images
            .filter(image => visible(image) && image.complete && image.naturalWidth === 0)
            .map(image => ({
              src: image.currentSrc || image.src || '',
              alt: image.alt || '',
              width: Math.round(image.getBoundingClientRect().width),
              height: Math.round(image.getBoundingClientRect().height),
            }));
          const stylesheetLinks = [...document.querySelectorAll('link[rel~="stylesheet"]')];
          const missingStylesheets = stylesheetLinks
            .filter(link => link.href.includes(missingPath) || !link.sheet)
            .map(link => link.href);
          return {
            imageCount: images.length,
            visibleBrokenImageCount: brokenImages.length,
            visibleBrokenImages: brokenImages.slice(0, 50),
            stylesheetLinkCount: stylesheetLinks.length,
            missingStylesheetCount: missingStylesheets.length,
            missingStylesheets: missingStylesheets.slice(0, 50),
          };
        }""",
        MISSING_ASSET_PATH,
    )
    defaults = {
        "imageCount": 0,
        "visibleBrokenImageCount": 0,
        "visibleBrokenImages": [],
        "stylesheetLinkCount": 0,
        "missingStylesheetCount": 0,
        "missingStylesheets": [],
    }
    if isinstance(value, dict):
        defaults.update({key: value.get(key, default) for key, default in defaults.items()})
    return defaults


def render_integrity_failures(label: str, result: dict[str, Any]) -> list[str]:
    integrity = result.get("render_integrity", {})
    failures = []
    missing_requests = integrity.get("missing_placeholder_requests", [])
    if missing_requests:
        failures.append(
            f"{label}: requested {len(missing_requests)} local missing-asset placeholder URL(s)"
        )
    broken = int(integrity.get("visibleBrokenImageCount", 0))
    if broken:
        failures.append(f"{label}: contains {broken} visible broken image(s)")
    missing_stylesheets = int(integrity.get("missingStylesheetCount", 0))
    if missing_stylesheets:
        failures.append(f"{label}: contains {missing_stylesheets} missing stylesheet(s)")
    blocked = [
        item for item in result.get("blocked_uncaptured_request_details", [])
        if item.get("resource_type") in CRITICAL_RESOURCE_TYPES
    ]
    if blocked:
        failures.append(
            f"{label}: blocked {len(blocked)} uncaptured critical resource request(s)"
        )
    request_failures = [
        item for item in integrity.get("request_failures", [])
        if item.get("resource_type") in CRITICAL_RESOURCE_TYPES
    ]
    if request_failures:
        failures.append(f"{label}: has {len(request_failures)} failed critical request(s)")
    http_errors = [
        item for item in integrity.get("http_errors", [])
        if item.get("resource_type") in CRITICAL_RESOURCE_TYPES
    ]
    if http_errors:
        failures.append(f"{label}: has {len(http_errors)} critical HTTP error response(s)")
    csp_errors = integrity.get("csp_errors", [])
    if csp_errors:
        failures.append(f"{label}: has {len(csp_errors)} CSP execution/style error(s)")
    settle = integrity.get("settle", {})
    if settle.get("stable") is False:
        failures.append(f"{label}: browser execution context did not stabilize")
    return failures


def classify_request_failures(
    records: list[dict[str, Any]], *, javascript_enabled: bool
) -> dict[str, Any]:
    """Separate Chromium's expected disabled-script preload failures."""
    expected = []
    blocking = []
    for record in records:
        if (
            not javascript_enabled
            and record.get("resource_type") == "script"
            and record.get("error") == "csp"
        ):
            expected.append(record)
        else:
            blocking.append(record)
    return {
        "request_failures": blocking[:50],
        "expected_disabled_script_failures": expected[:50],
        "expected_disabled_script_failure_count": len(expected),
    }


def page_structure(page) -> dict[str, Any]:
    return page.evaluate(
        """markers => {
          const text = document.body.innerText;
          const folded = text.toLowerCase();
          return ({
          title: document.title,
          headings: [...document.querySelectorAll('h1,h2,h3')].map(node => node.textContent.trim()),
          h1: [...document.querySelectorAll('h1')].map(node => node.textContent.trim()),
          forms: document.querySelectorAll('form').length,
          links: document.querySelectorAll('a[href]').length,
          images: document.querySelectorAll('img').length,
          hasHeader: Boolean(document.querySelector('header')),
          hasNav: Boolean(document.querySelector('nav')),
          hasMain: Boolean(document.querySelector('main')),
          mainContentElements: document.querySelector('main')?.querySelectorAll('a,button,form,h1,h2,h3,img,input,select').length || 0,
          mainTextBytes: new TextEncoder().encode(document.querySelector('main')?.innerText || '').length,
          hasFooter: Boolean(document.querySelector('footer')),
          bodyTextBytes: new TextEncoder().encode(document.body.innerText).length,
          interstitialMarkers: markers.filter(marker => folded.includes(marker)),
        });
        }""",
        list(ERROR_PAGE_MARKERS),
    )


def normalize_captured_workflows(page) -> dict[str, int]:
    """Apply the compiler's deterministic static state to a raw captured page."""
    return page.evaluate(
        r"""() => {
          let collapsed = 0;
          for (const control of document.querySelectorAll('[aria-expanded="false"][aria-controls]')) {
            for (const id of control.getAttribute('aria-controls').split(/\s+/).filter(Boolean)) {
              const target = document.getElementById(id);
              if (target && !target.hasAttribute('hidden')) {
                target.setAttribute('hidden', '');
                collapsed += 1;
              }
            }
          }
          let widgets = 0;
          for (const widget of document.querySelectorAll('.splide')) {
            if (widget.classList.contains('is-initialized') || widget.classList.contains('is-rendered')) {
              continue;
            }
            widget.classList.add('is-rendered');
            const names = [...widget.classList].join(' ').toLowerCase();
            widget.dataset.webharborWorkflow = names.includes('menu') || names.includes('navigation')
              ? 'static-list' : 'static-first-frame';
            widgets += 1;
          }
          if (widgets) {
            const style = document.createElement('style');
            style.dataset.webharborStaticWidgetFallback = '';
            style.textContent = `.splide[data-webharbor-workflow="static-first-frame"]{visibility:visible}
.splide[data-webharbor-workflow="static-first-frame"]>.splide__track>.splide__list{display:block}
.splide[data-webharbor-workflow="static-first-frame"]>.splide__track>.splide__list>.splide__slide{display:none}
.splide[data-webharbor-workflow="static-first-frame"]>.splide__track>.splide__list>.splide__slide:first-child{display:block;width:100%}
.splide[data-webharbor-workflow="static-list"]{visibility:visible}`;
            document.head.appendChild(style);
          }
          let streamChunks = 0;
          for (const chunk of document.querySelectorAll('div[hidden][id^="S:"]')) {
            if (/^S:\d+$/.test(chunk.id)) {
              chunk.removeAttribute('hidden');
              chunk.dataset.webharborWorkflow = 'static-stream-chunk';
              streamChunks += 1;
            }
          }
          let imageLoadStates = 0;
          for (const image of document.querySelectorAll('img[onload]')) {
            const source = (image.getAttribute('onload') || '').replace(/\s+/g, '').replace(/;+$/, '');
            if (source === "this.style.opacity='1'" || source === 'this.style.opacity="1"') {
              image.style.opacity = '1';
              image.dataset.webharborStaticImageLoaded = '';
              imageLoadStates += 1;
            } else if (
              source === "this.parentNode.dataset.loaded='true'"
              || source === 'this.parentNode.dataset.loaded="true"'
            ) {
              image.parentElement.dataset.loaded = 'true';
              image.parentElement.dataset.webharborStaticImageLoaded = '';
              imageLoadStates += 1;
            }
          }
          let hiddenBodies = 0;
          const bodyStyle = document.body ? getComputedStyle(document.body) : null;
          if (bodyStyle && (
            bodyStyle.display === 'none'
            || bodyStyle.visibility === 'hidden'
            || Number.parseFloat(bodyStyle.opacity || '1') === 0
          )) {
            document.body.dataset.webharborStaticHiddenBody = '';
            const style = document.createElement('style');
            style.dataset.webharborStaticHiddenBodyFallback = '';
            style.textContent = 'body[data-webharbor-static-hidden-body]{display:block!important;visibility:visible!important;opacity:1!important}';
            document.head.appendChild(style);
            hiddenBodies = 1;
          }
          let hiddenRoots = 0;
          const bootRootTokens = new Set([
            '__next', 'app', 'app-root', 'application', 'application-root',
            'page-root', 'root', 'site-root', 'template-root'
          ]);
          const hiddenDeclaration = /(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?)(?:\s*!important)?(?:;|$)/i;
          for (const element of document.body?.children || []) {
            const tokens = new Set([
              element.id.toLowerCase(),
              ...[...element.classList].map(value => value.toLowerCase())
            ]);
            if (![...tokens].some(value => bootRootTokens.has(value))) continue;
            if (!hiddenDeclaration.test(element.getAttribute('style') || '')) continue;
            const style = getComputedStyle(element);
            if (style.display !== 'none' && style.visibility !== 'hidden' && Number.parseFloat(style.opacity || '1') !== 0) continue;
            element.dataset.webharborStaticHiddenRoot = '';
            hiddenRoots += 1;
          }
          if (hiddenRoots) {
            const style = document.createElement('style');
            style.dataset.webharborStaticHiddenRootFallback = '';
            style.textContent = '[data-webharbor-static-hidden-root]{display:block!important;visibility:visible!important;opacity:1!important}';
            document.head.appendChild(style);
          }
          const hiddenFrames = document.querySelectorAll(
            'iframe[data-webharbor-workflow="hidden-frame-removed"]'
          ).length;
          return {
            collapsed_controls: collapsed,
            static_widgets: widgets,
            static_stream_chunks: streamChunks,
            static_image_load_states: imageLoadStates,
            static_hidden_bodies: hiddenBodies,
            static_hidden_roots: hiddenRoots,
            hidden_iframes_neutralized: hiddenFrames,
          };
        }"""
    )


LANDMARK_KEYS = ("hasHeader", "hasNav", "hasMain", "hasFooter")


def required_landmarks(
    route: dict[str, Any], upstream_structure: dict[str, Any]
) -> tuple[str, ...]:
    configured = route.get("required_landmarks")
    if configured == "match_upstream":
        return tuple(name for name in LANDMARK_KEYS if upstream_structure.get(name))
    if configured is None:
        return LANDMARK_KEYS
    if not isinstance(configured, list) or any(name not in LANDMARK_KEYS for name in configured):
        raise FidelityError(
            "required_landmarks must be 'match_upstream' or a list of known landmark keys"
        )
    return tuple(configured)


def has_meaningful_structure(structure: dict[str, Any]) -> bool:
    """Reject browser-disabled shells even when their pixels replay exactly."""
    return not structure.get("interstitialMarkers") and bool(
        (
            structure.get("hasMain")
            and (
                int(structure.get("mainContentElements", 0)) > 0
                or int(structure.get("mainTextBytes", 0)) >= 64
            )
        )
        or len(structure.get("headings", [])) >= 2
        or int(structure.get("forms", 0)) > 0
        or int(structure.get("bodyTextBytes", 0)) >= 512
        or (
            sum(bool(structure.get(key)) for key in LANDMARK_KEYS) >= 2
            and int(structure.get("links", 0)) >= 20
            and int(structure.get("bodyTextBytes", 0)) >= 256
        )
    )


def javascript_runtime_failures(
    label: str, result: dict[str, Any], *, separate_sample: bool
) -> list[str]:
    """Reject a destroyed JS runtime only when it was captured separately."""
    if not separate_sample:
        return []
    failures = []
    if float(result.get("nonblank_score", 0)) < 2:
        failures.append(f"{label}: JavaScript runtime blank or near-blank screenshot")
    if not has_meaningful_structure(result.get("structure", {})):
        failures.append(f"{label}: JavaScript runtime lacks meaningful structure")
    return failures


def capture_one(
    browser: Browser,
    *,
    url: str,
    viewport: dict[str, int],
    screenshot: Path,
    wait_for: str,
    local: bool,
    user_agent: str | None = None,
    replay: CapturedReplay | None = None,
    javascript_enabled: bool = True,
) -> dict[str, Any]:
    context_options: dict[str, Any] = {
        "viewport": viewport,
        "device_scale_factor": 1,
        "java_script_enabled": javascript_enabled,
    }
    if user_agent:
        context_options["user_agent"] = user_agent
    context = browser.new_context(**context_options)
    replay_misses_before = set(replay.miss_details) if replay is not None else set()
    if replay is not None:
        context.route("**/*", replay.handle)
    page = context.new_page()
    console_errors: list[str] = []
    external_requests: set[str] = set()
    missing_placeholder_requests: set[str] = set()
    request_failures: list[dict[str, Any]] = []
    http_errors: list[dict[str, Any]] = []
    expected_host = urlsplit(url).hostname

    def inspect_console(message) -> None:
        if message.type == "error":
            console_errors.append(message.text)

    def inspect_request(request) -> None:
        parsed = urlsplit(request.url)
        if MISSING_ASSET_PATH in parsed.path:
            missing_placeholder_requests.add(request.url)
        if local and parsed.scheme in {"http", "https"} and parsed.hostname != expected_host:
            external_requests.add(request.url)

    def inspect_failed_request(request) -> None:
        request_failures.append({
            "url": request.url,
            "resource_type": getattr(request, "resource_type", "other"),
            "error": getattr(request, "failure", None),
        })

    def inspect_response(response) -> None:
        if response.status >= 400:
            http_errors.append({
                "url": response.url,
                "status": response.status,
                "resource_type": getattr(response.request, "resource_type", "other"),
            })

    page.on("console", inspect_console)
    page.on("request", inspect_request)
    page.on("requestfailed", inspect_failed_request)
    page.on("response", inspect_response)
    response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if response is None or response.status != 200:
        raise FidelityError(f"navigation failed: {url} -> {response.status if response else 'no response'}")
    page.wait_for_selector(wait_for, state="attached", timeout=30_000)
    normalizations = (
        normalize_captured_workflows(page)
        if replay is not None
        else {
            "collapsed_controls": 0,
            "static_widgets": 0,
            "static_stream_chunks": 0,
            "static_image_load_states": page.locator(
                "[data-webharbor-static-image-loaded]"
            ).count(),
            "static_hidden_bodies": page.locator(
                "body[data-webharbor-static-hidden-body]"
            ).count(),
            "static_hidden_roots": page.locator(
                "[data-webharbor-static-hidden-root]"
            ).count(),
            "hidden_iframes_neutralized": page.locator(
                'iframe[data-webharbor-workflow="hidden-frame-removed"]'
            ).count(),
        }
    )
    settle = settle_page(page)
    page.wait_for_timeout(1200)
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot), full_page=False, animations="disabled")
    try:
        structure = page_structure(page)
    except PlaywrightError as exc:
        structure = {
            "title": page.title() if hasattr(page, "title") else "",
            "headings": [],
            "h1": [],
            "forms": 0,
            "links": 0,
            "images": 0,
            "hasHeader": False,
            "hasNav": False,
            "hasMain": False,
            "hasFooter": False,
            "mainContentElements": 0,
            "mainTextBytes": 0,
            "bodyTextBytes": 0,
            "interstitialMarkers": ["browser execution context unavailable"],
            "browser_error": str(exc),
        }
    try:
        integrity = resource_integrity(page)
    except PlaywrightError as exc:
        integrity = {
            "imageCount": 0,
            "visibleBrokenImageCount": 0,
            "visibleBrokenImages": [],
            "stylesheetLinkCount": 0,
            "missingStylesheetCount": 0,
            "missingStylesheets": [],
            "browser_error": str(exc),
        }
    integrity.update({
        "settle": settle,
        "missing_placeholder_requests": sorted(missing_placeholder_requests),
        **classify_request_failures(
            request_failures, javascript_enabled=javascript_enabled
        ),
        "http_errors": http_errors[:50],
        "csp_errors": [
            message for message in console_errors
            if "Content Security Policy" in message
            or "violates the following Content Security Policy" in message
        ][:50],
    })
    replay_misses = (
        sorted(replay.miss_details - replay_misses_before)
        if replay is not None
        else []
    )
    context.close()
    return {
        "url": url,
        "http_status": response.status,
        "screenshot_sha256": sha256_file(screenshot),
        "screenshot_bytes": screenshot.stat().st_size,
        "nonblank_score": nonblank_score(screenshot),
        "structure": structure,
        "external_requests": sorted(external_requests),
        "blocked_uncaptured_requests": sorted(url for _, url in replay_misses),
        "blocked_uncaptured_request_details": [
            {"resource_type": resource_type, "url": missed_url}
            for resource_type, missed_url in replay_misses
        ],
        "render_integrity": integrity,
        "source_mode": "captured_replay" if replay else "live_http",
        "javascript_enabled": javascript_enabled,
        "baseline_normalizations": normalizations,
        "console_errors": console_errors[:20],
    }


def repository_root() -> Path:
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / "sites").is_dir() and (parent / "mirror_factory").is_dir():
            return parent
    return script_path.parents[4]


def is_local_entry_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (parsed.path or "/") == "/" and not parsed.query


def validate_route_scope(spec: dict[str, Any]) -> None:
    """Keep the user-facing local entry page inside every fidelity run."""
    routes = spec.get("routes")
    if not isinstance(routes, list) or not routes:
        raise FidelityError("fidelity spec must declare at least one route")
    if not any(
        isinstance(route, dict) and is_local_entry_url(route.get("local_url"))
        for route in routes
    ):
        raise FidelityError(
            "fidelity spec must include the mandatory local entry route '/'"
        )
    excluded = spec.get("excluded_routes", [])
    if not isinstance(excluded, list):
        raise FidelityError("excluded_routes must be a list when present")
    if any(
        isinstance(route, dict) and is_local_entry_url(route.get("local_url"))
        for route in excluded
    ):
        raise FidelityError("the local entry route '/' cannot be excluded from fidelity")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    root = repository_root()
    spec_path = args.spec.resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    validate_route_scope(spec)
    site = spec["site"]
    upstream_user_agent = spec.get("upstream_user_agent")
    if upstream_user_agent is not None and (
        not isinstance(upstream_user_agent, str) or not upstream_user_agent.strip()
    ):
        raise FidelityError("upstream_user_agent must be a non-empty string")
    replay = None
    historical_reference = None
    capture_evidence = root / "mirror_factory" / "evidence" / site / "upstream.json"
    historical_evidence = None
    historical_value = spec.get("historical_source_evidence")
    if historical_value is not None:
        if not isinstance(historical_value, str) or not historical_value:
            raise FidelityError("historical_source_evidence must be a non-empty path")
        historical_evidence = (root / historical_value).resolve()
        expected_evidence_root = (root / "mirror_factory" / "evidence" / site).resolve()
        if expected_evidence_root not in historical_evidence.parents:
            raise FidelityError("historical_source_evidence must stay under the site's evidence directory")
        if not historical_evidence.is_file():
            raise FidelityError(f"historical source evidence is missing: {historical_evidence}")
        historical_reference = HistoricalReference(root, site, historical_evidence)
    if spec.get("upstream_capture_replay"):
        if historical_reference is not None:
            raise FidelityError("capture replay and historical reference modes are mutually exclusive")
        if not capture_evidence.is_file():
            raise FidelityError(f"capture evidence is missing: {capture_evidence}")
        capture = json.loads(capture_evidence.read_text(encoding="utf-8"))
        if capture.get("site") != site or capture.get("status") != "passed":
            raise FidelityError("capture replay requires passing evidence for the same site")
        replay = CapturedReplay(root, site, capture)
    matched_javascript_enabled = replay is None and historical_reference is None
    minimum_pixel_similarity = float(spec.get("minimum_pixel_similarity", 0.75))
    if not 0 <= minimum_pixel_similarity <= 1:
        raise FidelityError("minimum_pixel_similarity must be between 0 and 1")
    minimum_nonblank_retention = float(spec.get("minimum_nonblank_retention", 0.5))
    if not 0 <= minimum_nonblank_retention <= 1:
        raise FidelityError("minimum_nonblank_retention must be between 0 and 1")
    require_complete_resources = spec.get("require_complete_resources", True)
    if not isinstance(require_complete_resources, bool):
        raise FidelityError("require_complete_resources must be a boolean")
    runtime_javascript_check = spec.get("runtime_javascript_check", True)
    if not isinstance(runtime_javascript_check, bool):
        raise FidelityError("runtime_javascript_check must be a boolean")
    output = (root / spec.get("output_dir", f"sites/{site}/static/external_cache/fidelity")).resolve()
    required_root = (root / "sites" / site / "static" / "external_cache").resolve()
    if required_root not in output.parents:
        raise FidelityError(f"output_dir must stay under {required_root}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f"webharbor-fidelity-{site}-", dir=output.parent))
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            for viewport in spec["viewports"]:
                dimensions = {"width": int(viewport["width"]), "height": int(viewport["height"])}
                for route in spec["routes"]:
                    stem = re.sub(r"[^a-z0-9_-]+", "-", f"{route['name']}-{viewport['name']}".lower()).strip("-")
                    upstream_path = temp / f"{stem}-upstream.png"
                    local_path = temp / f"{stem}-local.png"
                    upstream = (
                        historical_reference.prepare(
                            upstream_path, dimensions, route.get("reference_crop")
                        )
                        if historical_reference is not None
                        else capture_one(
                            browser, url=route["upstream_url"], viewport=dimensions,
                            screenshot=upstream_path, wait_for=route.get("wait_for", "main"), local=False,
                            user_agent=upstream_user_agent,
                            replay=replay,
                            javascript_enabled=matched_javascript_enabled,
                        )
                    )
                    local = capture_one(
                        browser, url=route["local_url"], viewport=dimensions,
                        screenshot=local_path, wait_for=route.get("wait_for", "main"), local=True,
                        javascript_enabled=matched_javascript_enabled,
                    )
                    runtime_path = temp / f"{stem}-runtime.png"
                    separate_runtime_sample = (
                        runtime_javascript_check and not matched_javascript_enabled
                    )
                    runtime_local = (
                        capture_one(
                            browser, url=route["local_url"], viewport=dimensions,
                            screenshot=runtime_path, wait_for=route.get("wait_for", "main"),
                            local=True, javascript_enabled=True,
                        )
                        if separate_runtime_sample
                        else local
                    )
                    if upstream["nonblank_score"] < 2 or local["nonblank_score"] < 2:
                        failures.append(f"{stem}: blank or near-blank screenshot")
                    if not has_meaningful_structure(upstream["structure"]):
                        failures.append(
                            f"{stem}: upstream capture lacks meaningful static structure"
                        )
                    if not has_meaningful_structure(local["structure"]):
                        failures.append(
                            f"{stem}: local page lacks meaningful static structure"
                        )
                    failures.extend(javascript_runtime_failures(
                        stem,
                        runtime_local,
                        separate_sample=separate_runtime_sample,
                    ))
                    if local["external_requests"]:
                        failures.append(f"{stem}: local page requested external resources")
                    if runtime_local["external_requests"]:
                        failures.append(
                            f"{stem}: JavaScript runtime requested external resources"
                        )
                    if require_complete_resources:
                        failures.extend(render_integrity_failures(f"{stem} upstream", upstream))
                        failures.extend(
                            render_integrity_failures(f"{stem} local runtime", runtime_local)
                        )
                    landmark_names = required_landmarks(route, upstream["structure"])
                    missing = [name for name in landmark_names if not local["structure"].get(name)]
                    if missing:
                        failures.append(f"{stem}: local page missing {', '.join(missing)}")
                    similarity = pixel_similarity(upstream_path, local_path)
                    if similarity < minimum_pixel_similarity:
                        failures.append(
                            f"{stem}: pixel similarity {similarity:.6f} is below "
                            f"{minimum_pixel_similarity:.6f}"
                        )
                    retention = nonblank_retention(
                        upstream["nonblank_score"], local["nonblank_score"]
                    )
                    if retention < minimum_nonblank_retention:
                        failures.append(
                            f"{stem}: nonblank retention {retention:.6f} is below "
                            f"{minimum_nonblank_retention:.6f}"
                        )
                    results.append({
                        "route": route["name"],
                        "upstream_url": route["upstream_url"],
                        "local_url": route["local_url"],
                        "viewport": {"name": viewport["name"], **dimensions},
                        "upstream": {**upstream, "screenshot": upstream_path.name},
                        "local": {**local, "screenshot": local_path.name},
                        "runtime_local": {
                            **runtime_local,
                            "screenshot": (
                                runtime_path.name
                                if runtime_local is not local
                                else local_path.name
                            ),
                        },
                        "pixel_similarity": similarity,
                        "nonblank_retention": retention,
                        "heading_overlap": heading_overlap(upstream["structure"], local["structure"]),
                    })
            browser.close()
        if output.exists():
            shutil.rmtree(output)
        os.replace(temp, output)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    evidence = {
        "schema_version": 2,
        "fidelity_contract_version": FIDELITY_CONTRACT_VERSION,
        "tool": TOOL_VERSION,
        "phase": "fidelity",
        "site": site,
        "site_implementation": site_implementation_fingerprint(root / "sites" / site),
        "generated_at": utc_now(),
        "status": "passed" if not failures else "failed",
        "spec": {"path": spec_path.relative_to(root).as_posix(), "sha256": sha256_file(spec_path)},
        "upstream_capture_evidence_sha256": (
            sha256_file(capture_evidence) if replay is not None else None
        ),
        "historical_source_evidence_sha256": (
            sha256_file(historical_evidence) if historical_evidence is not None else None
        ),
        "results": results,
        "failures": failures,
        "minimum_pixel_similarity": minimum_pixel_similarity,
        "minimum_nonblank_retention": minimum_nonblank_retention,
        "require_complete_resources": require_complete_resources,
        "runtime_javascript_check": runtime_javascript_check,
        "upstream_user_agent": upstream_user_agent,
        "upstream_source_mode": (
            "historical_benchmark_snapshot"
            if historical_reference is not None
            else "captured_replay" if replay else "live_http"
        ),
        "captured_dom_prerender": (
            {
                "tool": capture.get("captured_dom_prerender", {}).get("tool"),
                "rendered_pages": replay.rendered_pages,
                "evidence_sha256": sha256_file(capture_evidence),
            }
            if replay and replay.rendered_pages
            else None
        ),
        "matched_javascript_enabled": matched_javascript_enabled,
        "notes": (
            "Historical screenshot similarity measures a pinned benchmark observation, not current production-site drift."
            if historical_reference is not None
            else "Resource integrity and visual similarity are automated candidate gates, not reviewer approval."
        ),
    }
    evidence_path = (args.evidence or root / "mirror_factory" / "evidence" / site / "fidelity.json").resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "site": site, "status": evidence["status"], "comparisons": len(results),
        "failures": len(failures), "evidence": str(evidence_path),
    }, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FidelityError as exc:
        print(f"fidelity error: {exc}", file=sys.stderr)
        raise SystemExit(1)
