#!/usr/bin/env python3
"""Audit visible local navigation and produce a fail-closed coverage report.

The audit is intentionally independent of a site's Flask implementation.  It
uses a real desktop browser, walks visible links from the local entry page, and
classifies every reachable destination as meaningful local navigation,
limitation, departure, external, broken, or protocol.  A HTTP 200 limitation
page is never counted as implemented click-through.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit, urlunsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


TOOL_VERSION = "webharbor-clickthrough-audit-v2"
VIEWPORT = {"width": 1440, "height": 1000}
LIMITATION_MARKERS = (
    "outside captured scope",
    "destination not included",
    "outside the captured",
    "local workflow unavailable",
    "source blocker:",
    "not available in this offline mirror",
    "scope notice",
)
EMPTY_MARKERS = ("page not found", "404", "找不到页面", "not found")
SKIP_SCHEMES = {"javascript", "data", "blob"}


class AuditError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuditConfig:
    base_url: str
    max_pages: int
    timeout_ms: int
    min_body_chars: int
    min_clickthrough: float
    official_hosts: frozenset[str]
    include_external: bool
    scope_policy: dict | None = None
    # Pointer-click audits can be expensive on navigation-heavy pages.  A
    # positive value bounds the number of visible anchors exercised per page;
    # the omitted remainder is disclosed in the evidence instead of hidden.
    max_links_per_page: int | None = None


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_url(base: str, raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw or raw.startswith("#"):
        return None
    absolute = urldefrag(urljoin(base, raw))[0]
    parsed = urlsplit(absolute)
    if parsed.scheme.casefold() in SKIP_SCHEMES or not parsed.hostname:
        return None
    if parsed.scheme.casefold() not in {"http", "https", "mailto", "tel"}:
        return None
    if parsed.scheme.casefold() in {"mailto", "tel"}:
        return absolute
    host = parsed.hostname.casefold()
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, ""))


def origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def local_path(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path or "/"


def route_identity(url: str) -> str:
    """Return the local route identity without conflating query variants."""
    parsed = urlsplit(url)
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def external_target(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.path != "/__webharbor_external":
        return None
    return parse_qs(parsed.query).get("url", [None])[0]


def compact_text(value: str) -> str:
    return " ".join((value or "").split())


def load_scope_policy(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuditError(f"invalid scope policy {path}: {exc}") from exc
    if policy.get("schema_version") != 1:
        raise AuditError("scope policy schema_version must be 1")
    if policy.get("profile") not in {
        "bounded_editorial_archive",
        "major_surface_first",
    }:
        raise AuditError(
            "scope policy profile must be bounded_editorial_archive or "
            "major_surface_first"
        )
    required_routes = policy.get("required_routes") or []
    archive_rules = policy.get("archive_rules") or policy.get("deferred_rules") or []
    if not isinstance(required_routes, list) or not isinstance(archive_rules, list):
        raise AuditError("scope policy routes and rules must be lists")
    for route in required_routes:
        route_path = route.get("path") if isinstance(route, dict) else None
        if not isinstance(route_path, str) or not route_path.startswith("/"):
            raise AuditError("every required scope route must have a root-relative path")
    seen_rule_ids: set[str] = set()
    for rule in archive_rules:
        if not isinstance(rule, dict):
            raise AuditError("every archive rule must be an object")
        rule_id = rule.get("id")
        pattern = rule.get("destination_regex")
        limit = rule.get("representative_limit")
        classification = rule.get(
            "classification",
            "bounded_archive_exclusion"
            if policy.get("profile") == "bounded_editorial_archive"
            else "deferred_surface_exclusion",
        )
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen_rule_ids:
            raise AuditError("archive rule ids must be nonempty and unique")
        if not isinstance(pattern, str) or not pattern.startswith("^"):
            raise AuditError(f"archive rule {rule_id} needs an anchored destination_regex")
        if not isinstance(limit, int) or limit < 0:
            raise AuditError(f"archive rule {rule_id} representative_limit must be nonnegative")
        if classification not in {
            "bounded_archive_exclusion",
            "deferred_surface_exclusion",
            "auth_or_account_exclusion",
        }:
            raise AuditError(f"archive rule {rule_id} has unsupported classification")
        try:
            re.compile(pattern)
            if rule.get("source_path_regex"):
                re.compile(rule["source_path_regex"])
        except re.error as exc:
            raise AuditError(f"archive rule {rule_id} has invalid regex: {exc}") from exc
        seen_rule_ids.add(rule_id)
    return policy


def scope_required_urls(config: AuditConfig) -> list[str]:
    if not config.scope_policy:
        return []
    urls = []
    for route in config.scope_policy.get("required_routes") or []:
        url = normalize_url(config.base_url + "/", route["path"])
        if url:
            urls.append(url)
    return urls


def classify_archive_scope(
    item: dict,
    source_path: str,
    config: AuditConfig,
    selected: dict[str, set[str]],
) -> tuple[str, str, bool, str] | None:
    """Select bounded representatives and classify the remaining archive links."""
    if not config.scope_policy or origin(item["url"]) != config.base_url:
        return None
    destination = route_identity(item["url"])
    required = {
        route_identity(url) for url in scope_required_urls(config)
    }
    if destination in required:
        return None
    rules = (
        config.scope_policy.get("archive_rules")
        or config.scope_policy.get("deferred_rules")
        or []
    )
    for rule in rules:
        source_pattern = rule.get("source_path_regex")
        if source_pattern and re.search(source_pattern, source_path) is None:
            continue
        if re.search(rule["destination_regex"], destination) is None:
            continue
        rule_id = rule["id"]
        chosen = selected.setdefault(rule_id, set())
        if item["url"] in chosen:
            return None
        limit = rule["representative_limit"]
        if len(chosen) < limit:
            chosen.add(item["url"])
            return None
        classification = rule.get(
            "classification",
            "bounded_archive_exclusion"
            if config.scope_policy.get("profile") == "bounded_editorial_archive"
            else "deferred_surface_exclusion",
        )
        reason = rule.get("reason") or (
            f"phase-one scope: kept {limit} deterministic representative route(s) "
            f"for family {rule_id}; remaining routes are disclosed phase-two work"
        )
        return classification, reason, False, rule_id
    return None


def navigation_equivalent(expected: str, actual: str) -> bool:
    expected_parts = urlsplit(expected)
    actual_parts = urlsplit(actual)
    if origin(expected) != origin(actual):
        return False
    expected_path = (expected_parts.path or "/").rstrip("/") or "/"
    actual_path = (actual_parts.path or "/").rstrip("/") or "/"
    return expected_path == actual_path and expected_parts.query == actual_parts.query


def classify_body(
    status: int | None,
    body: str,
    headings: list[str],
    path: str,
    min_body_chars: int = 100,
) -> tuple[str, str]:
    folded = compact_text(body).casefold()
    if status is None or status >= 500:
        return "broken_local", f"HTTP status {status}"
    if path == "/__webharbor_external":
        return "departure_notice", "local external-destination notice"
    if status == 404:
        return "broken_local", "local route returned 404"
    if any(marker in folded for marker in LIMITATION_MARKERS):
        return "limitation", "visible limitation/scope notice marker"
    if status >= 400:
        return "broken_local", f"HTTP status {status}"
    if len(compact_text(body)) < min_body_chars or not headings:
        return "broken_local", "empty or heading-less local destination"
    if any(marker == compact_text(headings[0]).casefold() for marker in EMPTY_MARKERS):
        return "broken_local", "not-found heading"
    return "meaningful_local", "non-empty local page with heading"


def reveal_disclosures(page, cfg: AuditConfig) -> None:
    controls = page.locator("button[aria-expanded='false'], [role='button'][aria-expanded='false'], summary")
    for index in range(min(controls.count(), 32)):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
            label = compact_text(control.inner_text() or control.get_attribute("aria-label") or "").casefold()
            if any(word in label for word in ("submit", "apply", "download", "delete", "sign out", "log out")):
                continue
            control.click(timeout=cfg.timeout_ms)
            page.wait_for_timeout(100)
        except Exception:
            continue


def discover_links(page, route: str, cfg: AuditConfig) -> tuple[list[dict], list[dict]]:
    """Return visible anchors and a small set of links revealed by menus."""
    discovered: list[dict] = []

    def collect() -> None:
        locator = page.locator("a[href]")
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                href = item.get_attribute("href") or ""
                target = normalize_url(page.url, href)
                if not target:
                    continue
                box = item.bounding_box()
                discovered.append(
                    {
                        "href": href,
                        "url": target,
                        "text": compact_text(item.inner_text() or item.get_attribute("aria-label") or item.get_attribute("title") or ""),
                        "target": item.get_attribute("target") or "",
                        "selector": f":nth-match(a[href], {index + 1})",
                        "visible_y": round(float((box or {}).get("y", 0.0)), 2),
                    }
                )
            except Exception:  # a detached link must not stop the whole inventory
                continue

    collect()
    # Header menus and disclosures often contain the missing official links.
    # Click only closed, visible, non-submit controls and return to the route.
    starting_url = urldefrag(page.url)[0]
    reveal_disclosures(page, cfg)
    if urldefrag(page.url)[0] == starting_url:
        collect()
    deduped: dict[tuple[str, str], dict] = {}
    for item in discovered:
        deduped[(item["url"], item["text"])] = item
    values = list(deduped.values())
    limit = cfg.max_links_per_page
    if limit is None or limit <= 0 or len(values) <= limit:
        return values, []
    # Keep links in DOM order so the audit exercises the primary navigation
    # and first visible content actions before lower-priority footer links.
    return values[:limit], values[limit:]


def verify_visible_click(page, source_url: str, item: dict, cfg: AuditConfig) -> dict:
    """Exercise a visible anchor with a real pointer click, including popups."""
    try:
        page.goto(source_url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
        reveal_disclosures(page, cfg)
        anchors = page.locator("a[href]")
        candidate = None
        for index in range(anchors.count()):
            anchor = anchors.nth(index)
            if not anchor.is_visible() or (anchor.get_attribute("href") or "") != item["href"]:
                continue
            text = compact_text(anchor.inner_text() or anchor.get_attribute("aria-label") or anchor.get_attribute("title") or "")
            if item.get("text") and text != item["text"]:
                continue
            candidate = anchor
            break
        if candidate is None:
            return {"status": "failed", "reason": "visible anchor could not be re-located"}
        if (item.get("target") or "").casefold() == "_blank":
            with page.context.expect_page(timeout=cfg.timeout_ms) as popup_info:
                candidate.click(timeout=cfg.timeout_ms)
            destination = popup_info.value
            try:
                destination.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
                actual = normalize_url(destination.url, destination.url)
            finally:
                destination.close()
        else:
            candidate.click(timeout=cfg.timeout_ms)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=cfg.timeout_ms)
            except PlaywrightTimeoutError:
                pass
            actual = normalize_url(page.url, page.url)
        if not actual or not navigation_equivalent(item["url"], actual):
            return {
                "status": "failed",
                "reason": "pointer click did not reach the anchor destination",
                "actual_url": actual,
            }
        return {"status": "passed", "actual_url": actual}
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def browser_open(
    page,
    url: str,
    timeout_ms: int,
    expected_origin: str,
) -> tuple[int | None, str, list[str], list[dict], list[str]]:
    errors: list[str] = []
    resource_issues: list[dict] = []

    def on_request(request) -> None:
        parsed = urlsplit(request.url)
        if parsed.scheme in {"http", "https"} and origin(request.url) != expected_origin:
            resource_issues.append({
                "kind": "external_runtime_request",
                "url": request.url,
                "resource_type": request.resource_type,
            })

    def on_request_failed(request) -> None:
        resource_issues.append({
            "kind": "failed_runtime_request",
            "url": request.url,
            "resource_type": request.resource_type,
            "failure": request.failure,
        })

    def on_response(response) -> None:
        if response.status >= 400 and response.request.resource_type != "document":
            resource_issues.append({
                "kind": "http_error_resource",
                "url": response.url,
                "resource_type": response.request.resource_type,
                "status": response.status,
            })

    page.on("request", on_request)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
        except PlaywrightTimeoutError:
            pass
        status = response.status if response is not None else None
        body = compact_text(page.locator("body").inner_text(timeout=timeout_ms))
        headings = [compact_text(x) for x in page.locator("h1, h2").all_inner_texts() if compact_text(x)]
        resource_issues.extend(page_resource_issues(page))
        deduped = list({json.dumps(issue, sort_keys=True): issue for issue in resource_issues}.values())
        return status, body, headings, deduped, errors
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        return None, "", [], resource_issues, errors
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("requestfailed", on_request_failed)
        page.remove_listener("response", on_response)


def page_resource_issues(page) -> list[dict]:
    """Inventory deterministic rendered resource failures on the current page."""
    try:
        return page.evaluate(
            """
            () => {
              const issues = [];
              for (const image of document.querySelectorAll('img[src]')) {
                if (image.complete && image.naturalWidth === 0) {
                  issues.push({kind: 'broken_image', url: image.currentSrc || image.src});
                }
              }
              for (const link of document.querySelectorAll('link[rel~="stylesheet"][href]')) {
                if (link.sheet === null) {
                  issues.push({kind: 'unloaded_stylesheet', url: link.href});
                }
              }
              return issues;
            }
            """
        )
    except Exception as exc:
        return [{"kind": "resource_inventory_error", "error": f"{type(exc).__name__}: {exc}"}]


def classify_link(item: dict, cfg: AuditConfig) -> tuple[str, str, bool]:
    url = item["url"]
    target = external_target(url)
    if target:
        host = (urlsplit(target).hostname or "").casefold()
        if host in cfg.official_hosts:
            return "official_supplemental_unimplemented", f"official supplemental target {host}", True
        return "departure_notice", "local external-destination wrapper", True
    parsed = urlsplit(url)
    if parsed.scheme in {"mailto", "tel"}:
        return "protocol", "requires an external protocol handler", True
    if origin(url) != cfg.base_url:
        host = (parsed.hostname or "").casefold()
        if host in cfg.official_hosts:
            return "official_supplemental_unimplemented", f"official supplemental host {host}", True
        return "external", "cross-origin destination", False
    return "candidate_local", "same-origin local destination", True


def audit(config: AuditConfig, output: Path, site_root: Path | None = None) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    routes: dict[str, dict] = {}
    links: list[dict] = []
    entry_url = config.base_url + "/"
    initial_urls = [entry_url, *scope_required_urls(config)]
    queue: deque[str] = deque(dict.fromkeys(initial_urls))
    queued = set(queue)
    truncated_urls: set[str] = set()
    browser_errors: list[str] = []
    selected_scope_urls: dict[str, set[str]] = {}
    truncated_link_count = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport=VIEWPORT)
        page = context.new_page()
        while queue and len(routes) < config.max_pages:
            url = queue.popleft()
            path = local_path(url)
            route_key = route_identity(url)
            status, body, headings, resources, errors = browser_open(
                page, url, config.timeout_ms, config.base_url
            )
            browser_errors.extend(f"{url}: {error}" for error in errors)
            body_class, body_reason = classify_body(
                status, body, headings, path, config.min_body_chars
            )
            routes[route_key] = {
                "url": url,
                "route_key": route_key,
                "status": status,
                "body_chars": len(body),
                "headings": headings[:8],
                "classification": body_class,
                "reason": body_reason,
                "resource_issues": resources,
            }
            if status != 200 or body_class != "meaningful_local":
                continue
            discovered, skipped_links = discover_links(page, path, config)
            truncated_link_count += len(skipped_links)
            for item in discovered:
                scope_decision = classify_archive_scope(
                    item, path, config, selected_scope_urls
                )
                scope_rule = None
                if scope_decision:
                    classification, reason, actionable, scope_rule = scope_decision
                else:
                    classification, reason, actionable = classify_link(item, config)
                click_result = (
                    verify_visible_click(page, url, item, config)
                    if classification == "candidate_local"
                    else None
                )
                link = {
                    **item,
                    "source_path": path,
                    "classification": classification,
                    "reason": reason,
                    "actionable": actionable,
                    "click": click_result,
                    "scope_rule": scope_rule,
                }
                links.append(link)
                if classification == "candidate_local":
                    if item["url"] not in queued:
                        if len(queued) < config.max_pages:
                            queued.add(item["url"])
                            queue.append(item["url"])
                        else:
                            truncated_urls.add(item["url"])
        browser.close()

    # Validate every same-origin candidate after discovery.  This is separate
    # from traversal so a bad page cannot hide links from later pages.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport=VIEWPORT)
        page = context.new_page()
        for item in links:
            if item["classification"] != "candidate_local":
                continue
            target_path = local_path(item["url"])
            target_key = route_identity(item["url"])
            if target_key not in routes:
                if item["url"] not in queued:
                    item["destination_classification"] = "unaudited_limit"
                    item["destination_status"] = None
                    item["destination_reason"] = (
                        f"route inventory exceeded max_pages={config.max_pages}"
                    )
                    continue
                status, body, headings, resources, errors = browser_open(
                    page, item["url"], config.timeout_ms, config.base_url
                )
                browser_errors.extend(f"{item['url']}: {error}" for error in errors)
                destination, reason = classify_body(
                    status, body, headings, target_path, config.min_body_chars
                )
                routes[target_key] = {
                    "url": item["url"],
                    "route_key": target_key,
                    "status": status,
                    "body_chars": len(body),
                    "headings": headings[:8],
                    "classification": destination,
                    "reason": reason,
                    "resource_issues": resources,
                }
            route = routes.get(target_key, {})
            item["destination_classification"] = route.get("classification", "broken_local")
            item["destination_status"] = route.get("status")
            item["destination_reason"] = route.get("reason")
        browser.close()

    for item in links:
        if item["classification"] != "candidate_local":
            continue
        if (item.get("click") or {}).get("status") != "passed":
            item["classification"] = "interaction_failure"
            continue
        if item.get("destination_classification") == "meaningful_local":
            item["classification"] = "meaningful_local"
        elif item.get("destination_classification") == "limitation":
            item["classification"] = "limitation"
        elif item.get("destination_classification") == "departure_notice":
            item["classification"] = "departure_notice"
        elif item.get("destination_classification") == "unaudited_limit":
            item["classification"] = "unaudited_limit"
        else:
            item["classification"] = "broken_local"

    counts: dict[str, int] = {}
    for item in links:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
    meaningful = counts.get("meaningful_local", 0)
    actionable_total = sum(
        counts.get(kind, 0)
        for kind in (
            "meaningful_local", "limitation", "departure_notice", "official_supplemental_unimplemented",
            "broken_local", "protocol", "unaudited_limit", "interaction_failure",
        )
    )
    clickthrough = round(100 * meaningful / actionable_total, 2) if actionable_total else 0.0
    unimplemented = [
        item for item in links
        if item["classification"] in {
            "limitation", "departure_notice", "official_supplemental_unimplemented", "broken_local", "protocol",
            "unaudited_limit",
            "interaction_failure",
        }
    ]
    deferred_exclusion_kinds = {
        "bounded_archive_exclusion",
        "deferred_surface_exclusion",
        "auth_or_account_exclusion",
    }
    bounded_archive_exclusions = [
        item for item in links
        if item["classification"] in deferred_exclusion_kinds
    ]
    scope_categories = []
    scope_rules = (
        (config.scope_policy or {}).get("archive_rules")
        or (config.scope_policy or {}).get("deferred_rules")
        or []
    )
    for rule in scope_rules:
        rule_id = rule["id"]
        scope_categories.append({
            "id": rule_id,
            "classification": rule.get(
                "classification",
                "bounded_archive_exclusion"
                if (config.scope_policy or {}).get("profile")
                == "bounded_editorial_archive"
                else "deferred_surface_exclusion",
            ),
            "representative_limit": rule["representative_limit"],
            "selected_representative_count": len(
                selected_scope_urls.get(rule_id, set())
            ),
            "bounded_archive_exclusion_count": sum(
                1 for item in bounded_archive_exclusions
                if item.get("scope_rule") == rule_id
            ),
        })
    resource_issues = [
        {"route_key": route["route_key"], **issue}
        for route in routes.values()
        for issue in route.get("resource_issues", [])
    ]
    all_visible_actionable = actionable_total + len(bounded_archive_exclusions)
    all_visible_percent = (
        round(100 * meaningful / all_visible_actionable, 2)
        if all_visible_actionable else 0.0
    )
    required_route_failures = []
    for route in (config.scope_policy or {}).get("required_routes") or []:
        required_url = normalize_url(config.base_url + "/", route["path"])
        required_key = route_identity(required_url) if required_url else route["path"]
        inventory = routes.get(required_key)
        if not inventory or inventory.get("classification") != "meaningful_local":
            required_route_failures.append({
                "category": route.get("category"),
                "path": route["path"],
                "classification": (
                    inventory.get("classification") if inventory else "missing"
                ),
                "status": inventory.get("status") if inventory else None,
            })
    complete_gate = (
        clickthrough >= config.min_clickthrough
        and not browser_errors
        and not resource_issues
        and not truncated_urls
        and not required_route_failures
        and routes.get("/", {}).get("classification") == "meaningful_local"
    )
    status = "passed" if complete_gate and truncated_link_count == 0 else (
        "sampled" if complete_gate else "failed"
    )
    result = {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "generated_at": utc_now(),
        "status": status,
        "site": site_root.name if site_root else None,
        "canonical_entry": {
            "url": config.base_url + "/",
            "path": "/",
            "status": routes.get("/", {}).get("status"),
            "rendered": routes.get("/", {}).get("classification") == "meaningful_local",
        },
        "viewport": VIEWPORT,
        "route_inventory": sorted(routes.values(), key=lambda item: item["url"]),
        "inventory_complete": not truncated_urls,
        "click_inventory_complete": truncated_link_count == 0,
        "pointer_click_scope": {
            "max_links_per_page": config.max_links_per_page,
            "selected_link_count": len(links),
            "discovered_link_count": len(links) + truncated_link_count,
            "truncated_link_count": truncated_link_count,
        },
        "max_pages": config.max_pages,
        "truncated_route_count": len(truncated_urls),
        "truncated_routes": sorted(truncated_urls),
        "route_counts": {key: sum(1 for route in routes.values() if route["classification"] == key) for key in sorted({route["classification"] for route in routes.values()})},
        "visible_link_count": len(links),
        "link_classification_counts": dict(sorted(counts.items())),
        "scope_policy": {
            "profile": config.scope_policy.get("profile"),
            "name": config.scope_policy.get("name"),
            "completion_level": config.scope_policy.get(
                "completion_level", "major_surface_mirror"
            ),
            "required_routes": config.scope_policy.get("required_routes", []),
            "categories": scope_categories,
            "bounded_archive_exclusion_count": len(bounded_archive_exclusions),
            "deferred_scope_exclusion_count": len(bounded_archive_exclusions),
            "policy_sha256": hashlib.sha256(
                json.dumps(
                    config.scope_policy,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "required_route_failures": required_route_failures,
        } if config.scope_policy else None,
        "clickthrough": {
            "requested_min_percent": config.min_clickthrough,
            "meaningful_local_count": meaningful,
            "actionable_count": actionable_total,
            "meaningful_local_percent": clickthrough,
            "all_visible_actionable_count": all_visible_actionable,
            "all_visible_meaningful_local_percent": all_visible_percent,
            "deferred_scope_count": len(bounded_archive_exclusions),
            "departure_notice_count": counts.get("departure_notice", 0),
            "limitation_count": counts.get("limitation", 0),
            "official_supplemental_unimplemented_count": counts.get("official_supplemental_unimplemented", 0),
            "broken_local_count": counts.get("broken_local", 0),
            "unaudited_limit_count": counts.get("unaudited_limit", 0),
            "interaction_failure_count": counts.get("interaction_failure", 0),
        },
        "unimplemented_links": unimplemented,
        "bounded_archive_exclusions": bounded_archive_exclusions,
        "deferred_scope_exclusions": bounded_archive_exclusions,
        "browser_errors": browser_errors,
        "resource_issues": resource_issues,
        "next_candidates": sorted({item["url"] for item in unimplemented if item["classification"] in {"limitation", "official_supplemental_unimplemented", "broken_local", "unaudited_limit", "interaction_failure"}}),
        "source_fingerprint": {
            "app_sha256": sha256_file(site_root / "app.py") if site_root else None,
            "seed_sha256": sha256_file(next(iter((site_root / "instance_seed").glob("*.db")), Path("/__missing__"))) if site_root and (site_root / "instance_seed").is_dir() else None,
        },
    }
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Loopback URL of the running local mirror.")
    parser.add_argument("--site", help="Site id; used to locate evidence and source fingerprints.")
    parser.add_argument("--workspace", type=Path, help="Workspace containing sites/<site> and mirror_factory/evidence.")
    parser.add_argument("--output", type=Path, help="clickthrough-coverage.json output path")
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--min-clickthrough", type=float, default=80.0)
    parser.add_argument(
        "--max-links-per-page",
        type=int,
        default=0,
        help="Bound real pointer clicks per page; 0 means all visible links.",
    )
    parser.add_argument("--official-host", action="append", default=[], help="Same-owner supplemental host; repeatable.")
    parser.add_argument(
        "--scope-policy",
        type=Path,
        help="Optional bounded editorial archive scope policy JSON.",
    )
    parser.add_argument("--include-external", action="store_true", help="Reserved for compatibility; external links are always classified.")
    args = parser.parse_args()
    parsed = urlsplit(args.base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("--base-url must be a loopback HTTP(S) origin")
    base = f"{parsed.scheme}://{parsed.netloc}"
    workspace = args.workspace.resolve() if args.workspace else None
    site_root = workspace / "sites" / args.site if workspace and args.site else None
    evidence = workspace / "mirror_factory" / "evidence" / args.site if workspace and args.site else None
    output = args.output.resolve() if args.output else (
        evidence / "clickthrough-coverage.json"
        if evidence
        else Path("clickthrough-coverage.json").resolve()
    )
    config = AuditConfig(
        base_url=base,
        max_pages=max(1, args.max_pages),
        timeout_ms=max(1000, args.timeout_ms),
        min_body_chars=100,
        min_clickthrough=max(0.0, min(100.0, args.min_clickthrough)),
        official_hosts=frozenset(host.casefold().strip().strip(".") for host in args.official_host),
        include_external=args.include_external,
        max_links_per_page=max(0, args.max_links_per_page) or None,
        scope_policy=load_scope_policy(
            args.scope_policy.resolve() if args.scope_policy else None
        ),
    )
    result = audit(config, output, site_root)
    print(json.dumps({
        "status": result["status"],
        "coverage": output.as_posix(),
        "pages": len(result["route_inventory"]),
        "visible_links": result["visible_link_count"],
        "meaningful_local_percent": result["clickthrough"]["meaningful_local_percent"],
        "next_candidates": len(result["next_candidates"]),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
