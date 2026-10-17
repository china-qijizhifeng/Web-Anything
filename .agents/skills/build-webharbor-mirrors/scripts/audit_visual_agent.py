#!/usr/bin/env python3
"""Run a read-only, matched official/local visual and workflow audit.

This command is deliberately an audit *agent input* rather than a site builder.
It captures the same desktop state from an official URL and a local mirror,
computes deterministic image/DOM/resource metrics, and emits a prompt that a
separate model can use to explain the differences.  The model output is
advisory; the JSON metrics remain the acceptance evidence.

Example::

    run.sh audit-visual-agent generated/visual-audit-recent-sites.json \
      --output generated/visual-audits/recent

The spec is JSON with ``routes`` entries containing ``id``, ``upstream_url``
and ``local_url``.  A route may optionally set ``wait_ms`` and ``max_links``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from PIL import Image, ImageChops, ImageEnhance, ImageStat
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from verify_fidelity import pixel_similarity


TOOL_VERSION = "webharbor-visual-audit-agent-v1"
SCHEMA_VERSION = 1
DEFAULT_VIEWPORT = {"width": 1440, "height": 1000}
DEFAULT_MAX_LINKS = 24
DEFAULT_TIMEOUT_MS = 25_000
DIFF_THRESHOLD = 24
DIFF_HIGHLIGHT_THRESHOLD = 12
LIMITATION_MARKERS = (
    "outside captured scope",
    "destination not included",
    "local workflow unavailable",
    "source blocker:",
    "not available in this offline mirror",
    "scope notice",
    "page not found",
    "404",
)


class AuditError(RuntimeError):
    """Raised for invalid audit input or an unusable browser environment."""


def repository_root() -> Path:
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / "sites").is_dir() and (parent / "mirror_factory").is_dir():
            return parent
    return path.parents[4]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_url(base: str, raw: str) -> str | None:
    """Resolve a visible href while retaining its query and target origin."""
    if not isinstance(raw, str) or not raw.strip() or raw.lstrip().startswith("#"):
        return None
    absolute = urldefrag(urljoin(base, raw.strip()))[0]
    parsed = urlsplit(absolute)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold()
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, ""))


def same_origin(first: str, second: str) -> bool:
    a = urlsplit(first)
    b = urlsplit(second)
    return (a.scheme.casefold(), a.netloc.casefold()) == (b.scheme.casefold(), b.netloc.casefold())


def append_unique(items: list[Any], value: Any, limit: int = 100) -> None:
    if value not in items and len(items) < limit:
        items.append(value)


def add_stability_style(page) -> None:
    page.add_style_tag(
        content=(
            "*,*::before,*::after{animation:none!important;transition:none!important;"
            "scroll-behavior:auto!important;caret-color:transparent!important}"
            "html{scroll-behavior:auto!important}"
        )
    )


def settle_page(page, wait_ms: int) -> dict[str, Any]:
    """Wait for bounded browser stability and restore the top of the page."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=4_000)
    except PlaywrightTimeoutError:
        pass
    try:
        page.wait_for_function(
            "() => !document.fonts || document.fonts.status === 'loaded'",
            timeout=4_000,
        )
    except PlaywrightTimeoutError:
        pass
    if wait_ms > 0:
        page.wait_for_timeout(min(wait_ms, 5_000))
    image_state = {"count": 0, "incomplete": 0, "broken": 0}
    try:
        image_state = page.evaluate(
            """() => {
              const images = [...document.images];
              images.forEach(image => { image.loading = 'eager'; image.decoding = 'sync'; });
              return {count: images.length,
                incomplete: images.filter(image => !image.complete).length,
                broken: images.filter(image => image.complete && image.naturalWidth === 0).length};
            }"""
        )
        page.wait_for_function(
            "() => [...document.images].every(image => image.complete)",
            timeout=4_000,
        )
    except (PlaywrightTimeoutError, PlaywrightError):
        pass
    try:
        page.evaluate(
            """() => {
              const height = Math.max(document.documentElement.scrollHeight, innerHeight || 1);
              const step = Math.max(500, Math.floor((innerHeight || 1) * .75));
              const limit = Math.min(height, Math.max(innerHeight || 1, (innerHeight || 1) * 8));
              for (let y = 0; y <= limit; y += step) window.scrollTo(0, y);
              window.scrollTo(0, 0);
            }"""
        )
        page.wait_for_timeout(250)
        page.evaluate("() => window.scrollTo(0, 0)")
    except PlaywrightError:
        pass
    return {
        "fonts_status": page.evaluate("() => document.fonts?.status || null"),
        "image_state": image_state if isinstance(image_state, dict) else {},
    }


def visible_dom(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const visible = element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && Number.parseFloat(style.opacity || '1') > 0 && rect.width > 1 && rect.height > 1;
          };
          const rect = element => {
            const box = element?.getBoundingClientRect();
            if (!box) return null;
            return {x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height)};
          };
          const links = [...document.querySelectorAll('a[href]')].filter(visible).map(link => ({
            text: (link.innerText || link.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 180),
            href: link.getAttribute('href') || '', target: link.getAttribute('target') || '', rect: rect(link)
          }));
          const controls = [...document.querySelectorAll('button,input,select,textarea,[role="button"]')]
            .filter(visible).map(control => ({
              tag: control.tagName.toLowerCase(), type: control.getAttribute('type') || '',
              text: (control.innerText || control.getAttribute('aria-label') || control.getAttribute('placeholder') || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
              disabled: Boolean(control.disabled || control.getAttribute('aria-disabled') === 'true'), rect: rect(control)
            }));
          const images = [...document.images].map(image => ({
            src: image.currentSrc || image.src || '', alt: image.alt || '', complete: image.complete,
            natural_width: image.naturalWidth, natural_height: image.naturalHeight, rect: rect(image)
          }));
          const headings = [...document.querySelectorAll('h1,h2,h3')].filter(visible).map(node =>
            (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 200));
          const landmarks = {};
          for (const name of ['header','nav','main','footer']) landmarks[name] = rect(document.querySelector(name));
          const bodyText = document.body?.innerText || '';
          const mainText = document.querySelector('main')?.innerText || '';
          const sheets = [...document.querySelectorAll('link[rel~="stylesheet"]')];
          return {
            url: location.href, title: document.title, headings, links, controls, images, landmarks,
            link_count: links.length, control_count: controls.length, form_count: document.forms.length,
            stylesheet_count: sheets.length, unloaded_stylesheets: sheets.filter(link => !link.sheet).map(link => link.href),
            body_text_bytes: new TextEncoder().encode(bodyText).length,
            main_text_bytes: new TextEncoder().encode(mainText).length,
            body_text_sample: bodyText.trim().replace(/\\s+/g, ' ').slice(0, 500),
            scroll_height: document.documentElement.scrollHeight,
          };
        }"""
    )


def render_resources(
    page,
    requests: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    http_errors: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    diagnostics = page.evaluate(
        """() => {
          const visible = element => {
            const style = getComputedStyle(element); const box = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              Number.parseFloat(style.opacity || '1') > 0 && box.width > 1 && box.height > 1;
          };
          const broken = [...document.images].filter(image => visible(image) && image.complete && image.naturalWidth === 0)
            .map(image => ({src: image.currentSrc || image.src || '', alt: image.alt || ''}));
          return {visible_broken_images: broken,
            missing_stylesheets: [...document.querySelectorAll('link[rel~="stylesheet"]')]
              .filter(link => !link.sheet).map(link => link.href)};
        }"""
    )
    base_origin = urlsplit(base_url).netloc.casefold()
    external = []
    request_count = 0
    for record in requests:
        request_count += 1
        request_url = record.get("url", "")
        parsed = urlsplit(request_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc.casefold() != base_origin:
            append_unique(external, request_url)
    return {
        "request_count": request_count,
        "failed_requests": failures[:100],
        "http_errors": http_errors[:100],
        "external_requests": external[:100],
        "visible_broken_images": diagnostics.get("visible_broken_images", []),
        "missing_stylesheets": diagnostics.get("missing_stylesheets", []),
        "critical_failures": len(failures) + len(http_errors) + len(diagnostics.get("visible_broken_images", [])),
    }


def screenshot_metrics(upstream_path: Path, local_path: Path, diff_path: Path) -> dict[str, Any]:
    first = Image.open(upstream_path).convert("RGB")
    second = Image.open(local_path).convert("RGB")
    if first.size != second.size:
        raise AuditError(f"screenshot sizes differ: {first.size} != {second.size}")
    difference = ImageChops.difference(first, second)
    diff_luma = difference.convert("L")
    max_channel = ImageChops.lighter(
        ImageChops.lighter(difference.getchannel("R"), difference.getchannel("G")),
        difference.getchannel("B"),
    )
    pixels = first.width * first.height
    histogram = diff_luma.histogram()
    thresholded = diff_luma.point(lambda value: 255 if value >= DIFF_HIGHLIGHT_THRESHOLD else 0)
    significant = diff_luma.point(lambda value: 255 if value >= DIFF_THRESHOLD else 0)
    mean_abs_luma = sum(index * count for index, count in enumerate(histogram)) / max(1, pixels)
    raw_agreement = sum(count for index, count in enumerate(max_channel.histogram()) if index <= 12) / max(1, pixels)
    significant_ratio = sum(count for index, count in enumerate(histogram) if index >= DIFF_THRESHOLD) / max(1, pixels)
    # Red heatmap on top of the local screenshot makes regions needing model
    # attention obvious without serving the heatmap as a page asset.
    heat = Image.new("RGB", first.size, (235, 235, 235))
    heat.paste((224, 44, 44), mask=thresholded)
    heat = ImageEnhance.Contrast(heat).enhance(1.15)
    Image.blend(second, heat, 0.58).save(diff_path, format="PNG", optimize=True)
    return {
        "width": first.width, "height": first.height,
        "pixel_similarity": pixel_similarity(upstream_path, local_path),
        "raw_pixel_agreement": round(raw_agreement, 6),
        "mean_abs_luma_diff": round(mean_abs_luma, 4),
        "significant_diff_ratio": round(significant_ratio, 6),
        "diff_bbox": list(significant.getbbox()) if significant.getbbox() else None,
        "nonblank_upstream": round(sum(ImageStat.Stat(first).stddev) / 3, 4),
        "nonblank_local": round(sum(ImageStat.Stat(second).stddev) / 3, 4),
        "thresholds": {"raw_agreement_channel_max": 12, "significant_luma": DIFF_THRESHOLD},
    }


def layout_differences(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for name in ("header", "nav", "main", "footer"):
        a, b = first.get("landmarks", {}).get(name), second.get("landmarks", {}).get(name)
        if not a or not b:
            result[name] = {"upstream": a, "local": b, "delta": None}
            continue
        result[name] = {
            "upstream": a, "local": b,
            "delta": {key: b[key] - a[key] for key in ("x", "y", "width", "height")},
        }
    return result


def heading_overlap(first: list[str], second: list[str]) -> float:
    a = {compact_text(value).casefold() for value in first if compact_text(value)}
    b = {compact_text(value).casefold() for value in second if compact_text(value)}
    return round(len(a & b) / max(1, len(a | b)), 6)


def visible_link_audit(browser, local_url: str, local_dom: dict[str, Any], timeout_ms: int, max_links: int) -> dict[str, Any]:
    links = local_dom.get("links", []) if isinstance(local_dom, dict) else []
    candidates = []
    for link in links:
        target = normalize_url(local_url, link.get("href", ""))
        if not target or not same_origin(local_url, target):
            continue
        if target in {normalize_url(local_url, local_url), normalize_url(local_url, "/")}:  # home duplicates are not useful probes
            continue
        if target not in [item["url"] for item in candidates]:
            candidates.append({"url": target, "text": link.get("text", ""), "target": link.get("target", "")})
        if len(candidates) >= max_links:
            break
    results = []
    context = browser.new_context(viewport=DEFAULT_VIEWPORT)
    page = context.new_page()
    try:
        for item in candidates:
            probe = {**item, "status": None, "final_url": None, "meaningful": False, "classification": "probe_error"}
            try:
                response = page.goto(item["url"], wait_until="domcontentloaded", timeout=timeout_ms)
                probe["status"] = response.status if response else None
                probe["final_url"] = page.url
                text = compact_text(page.locator("body").inner_text(timeout=2_000))
                probe["body_text_bytes"] = len(text.encode("utf-8"))
                folded = text.casefold()
                limitation = next((marker for marker in LIMITATION_MARKERS if marker in folded), None)
                probe["limitation_marker"] = limitation
                if probe["status"] is not None and 200 <= probe["status"] < 400 and not limitation and len(text.encode("utf-8")) >= 80:
                    probe["meaningful"] = True
                    probe["classification"] = "meaningful_local"
                elif limitation:
                    probe["classification"] = "limitation_or_404"
                elif probe["status"] is not None and probe["status"] >= 400:
                    probe["classification"] = "http_error"
                else:
                    probe["classification"] = "empty_or_redirect"
            except Exception as exc:  # a single bad link must not abort the audit
                probe["error"] = str(exc)[:300]
            results.append(probe)
    finally:
        context.close()
    counts = {key: sum(1 for item in results if item["classification"] == key) for key in (
        "meaningful_local", "limitation_or_404", "http_error", "empty_or_redirect", "probe_error"
    )}
    return {
        "visible_local_link_count": len(candidates),
        "probed_count": len(results),
        "meaningful_local_clickthrough": round(counts["meaningful_local"] / max(1, len(results)), 6),
        "counts": counts,
        "links": results,
    }


def capture_page(browser, url: str, label: str, viewport: dict[str, int], output_path: Path, timeout_ms: int, wait_ms: int) -> dict[str, Any]:
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    request_records: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    page.on("request", lambda request: request_records.append({"url": request.url, "resource_type": request.resource_type, "method": request.method}))
    page.on("requestfailed", lambda request: failed.append({"url": request.url, "resource_type": request.resource_type, "error": request.failure}))
    page.on("response", lambda response: errors.append({"url": response.url, "status": response.status, "resource_type": response.request.resource_type}) if response.status >= 400 else None)
    console_errors: list[str] = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    response = None
    navigation_error = None
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            add_stability_style(page)
        except PlaywrightError:
            pass
        stability = settle_page(page, wait_ms)
        dom = visible_dom(page)
        page.screenshot(path=str(output_path), full_page=False, animations="disabled")
    except Exception as exc:
        navigation_error = str(exc)[:500]
        try:
            page.screenshot(path=str(output_path), full_page=False)
            dom = visible_dom(page)
        except Exception:
            dom = {"headings": [], "links": [], "controls": [], "landmarks": {}}
        stability = {"error": navigation_error}
    resources = render_resources(page, request_records, failed, errors, url)
    result = {
        "label": label, "requested_url": url, "final_url": page.url,
        "http_status": response.status if response else None,
        "navigation_error": navigation_error, "stability": stability,
        "dom": dom, "resources": resources, "console_errors": console_errors[:100],
        "screenshot": str(output_path), "screenshot_sha256": sha256_file(output_path) if output_path.is_file() else None,
    }
    context.close()
    return result


def route_prompt(route: dict[str, Any], output_dir: Path) -> str:
    route_id = route["id"]
    return f"""## Route `{route_id}`\n\n- Official URL: `{route['upstream_url']}`\n- Local URL: `{route['local_url']}`\n- Official screenshot: `{output_dir / route_id / 'upstream.png'}`\n- Local screenshot: `{output_dir / route_id / 'local.png'}`\n- Difference heatmap: `{output_dir / route_id / 'diff.png'}`\n- Route evidence: `{output_dir / route_id / 'route.json'}`\n"""


def make_model_prompt(result: dict[str, Any], output_dir: Path) -> str:
    lines = [
        "# WebHarbor visual audit agent prompt",
        "",
        "You are a read-only reviewer of an offline WebHarbor mirror. Do not edit files, start services, or invent missing source data.",
        "Read `visual-audit.json`, each route's `route.json`, the official/local screenshots, and the diff heatmaps. Use the deterministic metrics as evidence.",
        "A score of 0.95 is a hard visual target only when both pages are the intended state, have HTTP 2xx, and have no critical resource errors. Do not turn a blocked official page or a scope notice into a fidelity pass.",
        "Return JSON with keys `overall_assessment`, `routes`, and `next_actions`. Each route finding must include `severity` (critical|major|minor|info), `category` (visual|content|resource|interaction|source_state), `evidence`, and `repair`. Separate upstream access/state blockers from local mirror defects.",
        "For interaction, distinguish visible local links that probe to meaningful pages from limitation pages, 404s, empty links, and external departures. Treat model judgement as advisory; never override the recorded deterministic gate.",
        "",
        "## Deterministic audit output",
        f"`{output_dir / 'visual-audit.json'}`",
        "",
    ]
    for route in result.get("routes", []):
        lines.append(route_prompt(route, output_dir))
    return "\n".join(lines).rstrip() + "\n"


def validate_spec(spec: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    if spec.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise AuditError("visual audit spec schema_version must be 1")
    viewport = spec.get("viewport", DEFAULT_VIEWPORT)
    if not isinstance(viewport, dict) or int(viewport.get("width", 0)) <= 0 or int(viewport.get("height", 0)) <= 0:
        raise AuditError("viewport must contain positive width and height")
    routes = spec.get("routes")
    if not isinstance(routes, list) or not routes:
        raise AuditError("spec must declare at least one route")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for route in routes:
        if not isinstance(route, dict):
            raise AuditError("each route must be an object")
        route_id = route.get("id")
        upstream_url, local_url = route.get("upstream_url"), route.get("local_url")
        if not isinstance(route_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", route_id) or route_id in seen:
            raise AuditError("route ids must be unique ASCII identifiers")
        if not isinstance(upstream_url, str) or not urlsplit(upstream_url).scheme:
            raise AuditError(f"route {route_id} needs an absolute upstream_url")
        if not isinstance(local_url, str) or not urlsplit(local_url).scheme:
            raise AuditError(f"route {route_id} needs an absolute local_url")
        seen.add(route_id)
        normalized.append({
            **route, "id": route_id, "wait_ms": max(0, min(int(route.get("wait_ms", 500)), 5_000)),
            "max_links": max(0, min(int(route.get("max_links", DEFAULT_MAX_LINKS)), 100)),
        })
    return {"width": int(viewport["width"]), "height": int(viewport["height"])}, normalized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="directory for screenshots and evidence")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--max-links", type=int, default=DEFAULT_MAX_LINKS)
    args = parser.parse_args(argv)
    spec_path = args.spec.resolve()
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        viewport, routes = validate_spec(spec)
        output_dir = args.output.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "tool_version": TOOL_VERSION,
            "started_at": utc_now(), "spec": str(spec_path),
            "spec_sha256": sha256_file(spec_path), "viewport": viewport,
            "routes": [], "hard_gate": {"minimum_pixel_similarity": 0.95},
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            for route in routes:
                route_dir = output_dir / route["id"]
                route_dir.mkdir(parents=True, exist_ok=True)
                upstream = capture_page(browser, route["upstream_url"], "upstream", viewport, route_dir / "upstream.png", args.timeout_ms, route["wait_ms"])
                local = capture_page(browser, route["local_url"], "local", viewport, route_dir / "local.png", args.timeout_ms, route["wait_ms"])
                route_result: dict[str, Any] = {
                    "id": route["id"], "upstream_url": route["upstream_url"], "local_url": route["local_url"],
                    "upstream": upstream, "local": local,
                }
                if Path(upstream["screenshot"]).is_file() and Path(local["screenshot"]).is_file():
                    route_result["visual"] = screenshot_metrics(Path(upstream["screenshot"]), Path(local["screenshot"]), route_dir / "diff.png")
                    route_result["structure"] = {
                        "heading_overlap": heading_overlap(upstream.get("dom", {}).get("headings", []), local.get("dom", {}).get("headings", [])),
                        "layout_differences": layout_differences(upstream.get("dom", {}), local.get("dom", {})),
                    }
                route_result["clickthrough"] = visible_link_audit(browser, route["local_url"], local.get("dom", {}), args.timeout_ms, min(args.max_links, route["max_links"]))
                route_result["hard_gate"] = {
                    "official_http_2xx": bool(upstream.get("http_status") and 200 <= upstream["http_status"] < 300),
                    "local_http_2xx": bool(local.get("http_status") and 200 <= local["http_status"] < 300),
                    "pixel_95": bool(route_result.get("visual", {}).get("pixel_similarity", 0) >= 0.95),
                    "local_critical_resource_errors": local.get("resources", {}).get("critical_failures", 0),
                    "local_external_request_count": len(local.get("resources", {}).get("external_requests", [])),
                    "local_resources_clean": (
                        local.get("resources", {}).get("critical_failures", 0) == 0
                        and not local.get("resources", {}).get("external_requests", [])
                    ),
                    "local_has_limitation_marker": any(marker in compact_text(local.get("dom", {}).get("body_text_sample", "")).casefold() for marker in LIMITATION_MARKERS),
                }
                (route_dir / "route.json").write_text(json.dumps(route_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result["routes"].append(route_result)
            browser.close()
        result["finished_at"] = utc_now()
        result["summary"] = {
            "route_count": len(result["routes"]),
            "visual_95_pass_count": sum(1 for route in result["routes"] if route.get("hard_gate", {}).get("pixel_95")),
            "official_available_count": sum(1 for route in result["routes"] if route.get("hard_gate", {}).get("official_http_2xx")),
            "local_available_count": sum(1 for route in result["routes"] if route.get("hard_gate", {}).get("local_http_2xx")),
            "local_resource_clean_count": sum(1 for route in result["routes"] if route.get("hard_gate", {}).get("local_resources_clean")),
            "mean_pixel_similarity": round(sum(route.get("visual", {}).get("pixel_similarity", 0) for route in result["routes"]) / max(1, len(result["routes"])), 6),
            "mean_meaningful_local_clickthrough": round(sum(route.get("clickthrough", {}).get("meaningful_local_clickthrough", 0) for route in result["routes"]) / max(1, len(result["routes"])), 6),
        }
        output_path = output_dir / "visual-audit.json"
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output_dir / "model-audit-prompt.md").write_text(make_model_prompt(result, output_dir), encoding="utf-8")
        print(json.dumps({"output": str(output_path), "summary": result["summary"]}, ensure_ascii=False, indent=2))
        # A blocked official page is useful evidence but not a successful match.
        return 0 if result["summary"]["route_count"] and result["summary"]["local_available_count"] == result["summary"]["route_count"] else 1
    except (AuditError, OSError, ValueError, PlaywrightError) as exc:
        print(f"audit-visual-agent: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
