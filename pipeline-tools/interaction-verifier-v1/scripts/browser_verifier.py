#!/usr/bin/env python3
"""Verify fail-closed immersive interaction closure in a real desktop browser.

The command inventories rendered routes and active-looking controls, opens
menus/disclosures to discover nested targets, executes declared outcomes, blocks
traffic outside the local surface, and emits normalized contract, closure, and
Builder repair-package artifacts.  It never changes a site implementation and it
never publishes a preview.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contract import (
    CLOSURE_SCHEMA,
    VERIFIER_TOOL,
    ContractError,
    LocalSurface,
    atomic_json,
    build_repair_package,
    canonical_control_identity,
    canonical_control_identity_key,
    control_identity_compatible,
    candidate_fingerprint,
    canonical_json,
    canonical_query,
    contract_artifact,
    deduplicate_failures,
    derive_browser_control_contracts,
    derive_site_contract_surface,
    load_contract,
    provenance_file_failures,
    semantic_control_key,
    sha256_bytes,
    sha256_file,
)
from inventory import ANALYZER_TOOL, INVENTORY_SCHEMA, inventory_site
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from triangulation import (
    build_interaction_diff,
    canonicalize_controls,
    route_equivalent,
)

DEFENSIVE_PHRASES = (
    "unsupported",
    "not available in this mirror",
    "not available in this offline mirror",
    "external link",
    "external destination",
    "leaving the site",
    "leaving this site",
    "scope limitation",
    "outside captured scope",
    "outside the captured scope",
    "destination not included",
    "local workflow unavailable",
    "page not found",
    "not found",
)
DISCOVERY_SELECTOR = "a[href], button, [role='button'], [role='link'], [role='menuitem'], [role='tab'], summary, form, input:not([type='hidden']), select, textarea, [onclick], [data-action], [aria-controls], [aria-expanded], [tabindex]:not([tabindex='-1'])"


def compact_text(value: str | None) -> str:
    return " ".join((value or "").split())


def route_without_query(route: str) -> str:
    return urlsplit(route).path or "/"


def route_context(contract_route: dict | None) -> dict:
    return {
        "route_family": (contract_route or {}).get("family", "undeclared"),
        "page_archetype": (contract_route or {}).get("archetype", "undeclared"),
    }


def failure(
    code: str,
    message: str,
    *,
    route: str | None = None,
    contract_route: dict | None = None,
    control: dict | None = None,
    selector: str | None = None,
    evidence: Any | None = None,
) -> dict:
    value = {
        "code": code,
        "message": message,
        "route": route,
        **route_context(contract_route),
        "control_id": (control or {}).get("id"),
        "control_type": (control or {}).get("control_type"),
        "selector": selector or (control or {}).get("selector"),
    }
    if evidence not in (None, {}, []):
        value["evidence"] = evidence
    return value


class NetworkGuard:
    def __init__(self, surface: LocalSurface):
        self.surface = surface
        self.events: list[dict] = []
        self.loaded: list[dict] = []

    def install(self, context) -> None:
        def handle(route, request) -> None:
            if self.surface.request_allowed(request.url):
                self.loaded.append({
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "is_navigation_request": request.is_navigation_request(),
                })
                route.continue_()
                return
            parsed = urlsplit(request.url)
            if parsed.scheme in {"data", "blob", "about"}:
                route.continue_()
                return
            kind = (
                "unexpected_upstream_navigation"
                if request.resource_type == "document"
                else "unexpected_external_request"
            )
            self.events.append({
                "kind": kind,
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "is_navigation_request": request.is_navigation_request(),
            })
            route.abort("blockedbyclient")

        context.route("**/*", handle)


def goto(page, surface: LocalSurface, logical_path: str, timeout_ms: int) -> tuple[int | None, str | None]:
    try:
        response = page.goto(
            surface.to_url(logical_path),
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 3000))
        except PlaywrightTimeoutError:
            pass
        return response.status if response is not None else None, None
    except Exception as exc:  # noqa: BLE001 - browser navigation failures are evidence
        return None, f"{type(exc).__name__}: {exc}"


def discover_controls(page, revealed_by: list[str] | None = None) -> list[dict]:
    """Collect visible actionable semantics and retain deterministic DOM evidence."""
    return page.evaluate(
        """
        ({selector, revealedBy}) => {
          let nextId = Number(document.documentElement.dataset.webharborAuditNext || '1');
          const cssEscape = value => {
            if (globalThis.CSS && CSS.escape) return CSS.escape(value);
            return String(value).replace(/[^A-Za-z0-9_-]/g, ch => `\\${ch}`);
          };
          const cssAttrEscape = value => String(value)
            .split(String.fromCharCode(92)).join(String.fromCharCode(92) + String.fromCharCode(92))
            .split('"').join(String.fromCharCode(92) + '"')
            .split('\\n').join(String.fromCharCode(92) + '0A ')
            .split('\\r').join(String.fromCharCode(92) + '0D ')
            .split('\\f').join(String.fromCharCode(92) + '0C ');
          const visible = element => {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              Number(style.opacity || '1') > 0 && box.width > 0 && box.height > 0;
          };
          const accessibleName = element => {
            const labelled = element.getAttribute('aria-labelledby');
            if (labelled) {
              const text = labelled.split(/\\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
              if (text) return text;
            }
            const explicit = element.getAttribute('aria-label') || element.getAttribute('title');
            if (explicit) return explicit.trim();
            if (element.id) {
              const label = document.querySelector(`label[for="${cssEscape(element.id)}"]`);
              if (label?.innerText?.trim()) return label.innerText.trim();
            }
            return (element.innerText || element.getAttribute('alt') ||
              element.getAttribute('placeholder') || element.getAttribute('value') ||
              element.getAttribute('name') || '').trim();
          };
          const classify = element => {
            const tag = element.tagName.toLowerCase();
            const role = (element.getAttribute('role') || '').toLowerCase();
            const type = (element.getAttribute('type') || '').toLowerCase();
            const closestPagination = element.closest('[aria-label*="agination" i], .pagination, [class*="pagination"]');
            if (closestPagination) return 'pagination';
            if (role === 'tab') return 'tab';
            if (tag === 'summary' || element.hasAttribute('aria-expanded')) {
              if ((element.getAttribute('aria-haspopup') || '').toLowerCase() === 'menu') return 'menu';
              return 'disclosure';
            }
            if (tag === 'form') return 'form';
            if (tag === 'select') return 'filter';
            if (tag === 'textarea') return 'input';
            if (tag === 'input') {
              if (type === 'search' || element.closest('[role="search"], form.search, form[class*="search"]')) return 'search';
              if (['checkbox', 'radio', 'range'].includes(type)) return 'filter';
              return 'input';
            }
            if (element.matches('[data-card], [class*="card" i]')) return 'card';
            if (tag === 'a' || role === 'link') {
              if ((element.getAttribute('target') || '').toLowerCase() === '_blank') return 'popup';
              if (element.hasAttribute('download')) return 'download';
              if (element.closest('[class*="card" i], article, [data-card]')) return 'card';
              return 'link';
            }
            if ((element.getAttribute('aria-haspopup') || '').toLowerCase() === 'menu') return 'menu';
            if (element.closest('form') && (type === 'submit' || tag === 'button')) return 'button';
            return 'state_change';
          };
          const candidates = new Set(document.querySelectorAll(selector));
          for (const element of document.querySelectorAll('body *')) {
            if (getComputedStyle(element).cursor !== 'pointer') continue;
            if (element.closest(selector)) continue;
            candidates.add(element);
          }
          const values = [];
          for (const element of candidates) {
            if (!visible(element)) continue;
            if (!element.dataset.webharborAuditId) {
              element.dataset.webharborAuditId = String(nextId++);
            }
            const auditId = element.dataset.webharborAuditId;
            const tag = element.tagName.toLowerCase();
            let identitySelector;
            if (element.id) identitySelector = `#${cssEscape(element.id)}`;
            else if (element.getAttribute('data-testid')) identitySelector = `${tag}[data-testid="${cssAttrEscape(element.getAttribute('data-testid'))}"]`;
            else if (element.getAttribute('name')) identitySelector = `${tag}[name="${cssAttrEscape(element.getAttribute('name'))}"]`;
            else if (tag === 'a' && element.getAttribute('href') && !/[{}]/.test(element.getAttribute('href'))) identitySelector = `a[href="${cssAttrEscape(element.getAttribute('href'))}"]`;
            else if (tag === 'form' && element.getAttribute('action') && !/[{}]/.test(element.getAttribute('action'))) identitySelector = `form[action="${cssAttrEscape(element.getAttribute('action'))}"]`;
            else if (element.getAttribute('aria-label')) identitySelector = `${tag}[aria-label="${cssAttrEscape(element.getAttribute('aria-label'))}"]`;
            const runtimeSelector = `[data-webharbor-audit-id="${auditId}"]`;
            const box = element.getBoundingClientRect();
            values.push({
              audit_id: auditId,
              selector: identitySelector || runtimeSelector,
              identity_selector: identitySelector || null,
              runtime_selector: runtimeSelector,
              tag,
              role: element.getAttribute('role') || '',
              control_type: classify(element),
              accessible_name: accessibleName(element).replace(/\\s+/g, ' ').trim(),
              href: element.getAttribute('href'),
              action: element.getAttribute('action'),
              method: (element.getAttribute('method') || 'get').toLowerCase(),
              target: element.getAttribute('target') || '',
              aria_expanded: element.getAttribute('aria-expanded'),
              aria_controls: element.getAttribute('aria-controls'),
              disabled: Boolean(element.disabled) || element.getAttribute('aria-disabled') === 'true',
              revealed_by: revealedBy,
              bbox: {x: Math.round(box.x * 100) / 100, y: Math.round(box.y * 100) / 100,
                     width: Math.round(box.width * 100) / 100, height: Math.round(box.height * 100) / 100},
            });
          }
          document.documentElement.dataset.webharborAuditNext = String(nextId);
          return values;
        }
        """,
        {"selector": DISCOVERY_SELECTOR, "revealedBy": revealed_by or []},
    )


def canonicalize_edges(edges: list[dict]) -> list[dict]:
    """Collapse repeat crawl edges while retaining bounded raw provenance."""
    grouped: dict[tuple[str, str, str, str], dict] = {}
    for edge in edges:
        key = (
            canonical_query(edge.get("from")) or "/",
            canonical_query(edge.get("to")) or "",
            str(edge.get("kind") or ""),
            str(edge.get("control_type") or ""),
        )
        item = grouped.get(key)
        if item is None:
            item = dict(edge)
            item["raw_observation_count"] = 0
            item["raw_provenance"] = []
            grouped[key] = item
        item["raw_observation_count"] += int(edge.get("raw_observation_count") or 1)
        item["raw_provenance"].append({
            "from": edge.get("from"),
            "to": edge.get("to"),
            "selector": edge.get("selector"),
            "audit_id": edge.get("audit_id"),
        })
    result = []
    for item in grouped.values():
        item["raw_provenance"] = sorted(
            {json.dumps(value, sort_keys=True): value for value in item["raw_provenance"]}.values(),
            key=lambda value: json.dumps(value, sort_keys=True),
        )[:64]
        result.append(item)
    return sorted(
        result,
        key=lambda item: (
            str(item.get("from") or ""),
            str(item.get("to") or ""),
            str(item.get("kind") or ""),
            str(item.get("control_type") or ""),
        ),
    )


def probe_source_controls(page, route: str, source_controls: list[dict]) -> list[dict]:
    """Measure declared source controls even when the visible inventory omits them."""
    relevant = [
        item for item in source_controls
        if item.get("selector")
        and any(route_equivalent(rule, route) for rule in item.get("source_routes") or [])
    ]
    if not relevant:
        return []
    return page.evaluate(
        """
        ({route, controls}) => controls.map(control => {
          let elements = [];
          let selectorError = null;
          try { elements = [...document.querySelectorAll(control.selector)]; }
          catch (error) { selectorError = String(error); }
          const element = elements[0] || null;
          let visible = false;
          let disabled = false;
          let hitTestable = false;
          let bbox = null;
          if (element) {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            bbox = {x: box.x, y: box.y, width: box.width, height: box.height};
            visible = style.display !== 'none' && style.visibility !== 'hidden' &&
              Number(style.opacity || '1') > 0 && box.width > 0 && box.height > 0;
            disabled = Boolean(element.disabled) || element.getAttribute('aria-disabled') === 'true';
            if (visible) {
              const x = Math.max(0, Math.min(innerWidth - 1, box.left + box.width / 2));
              const y = Math.max(0, Math.min(innerHeight - 1, box.top + box.height / 2));
              const hit = document.elementFromPoint(x, y);
              hitTestable = Boolean(hit && (hit === element || element.contains(hit) || hit.contains(element)));
            }
          }
          return {
            source_id: control.id,
            route,
            selector: control.selector,
            count: elements.length,
            visible,
            disabled,
            hit_testable: hitTestable,
            bbox,
            selector_error: selectorError,
          };
        })
        """,
        {"route": route, "controls": relevant},
    )


def reveal_nested_controls(page, timeout_ms: int) -> tuple[list[dict], list[dict]]:
    discovered: dict[str, dict] = {
        item["audit_id"]: item for item in discover_controls(page)
    }
    proofs: list[dict] = []
    reveal_candidates = [
        item for item in discovered.values()
        if item["control_type"] in {"menu", "disclosure"} and not item["disabled"]
    ][:32]
    for item in reveal_candidates:
        locator = page.locator(f"[data-webharbor-audit-id='{item['audit_id']}']")
        if locator.count() != 1 or not locator.is_visible():
            continue
        before_url = page.url
        before_expanded = locator.get_attribute("aria-expanded")
        try:
            locator.click(timeout=timeout_ms)
            page.wait_for_timeout(75)
            after_expanded = locator.get_attribute("aria-expanded")
            changed = before_expanded != after_expanded or page.evaluate(
                "id => document.querySelector(`[data-webharbor-audit-id='${id}']`)?.closest('details')?.open === true",
                item["audit_id"],
            )
            proofs.append({
                "selector": item["selector"],
                "accessible_name": item["accessible_name"],
                "before_aria_expanded": before_expanded,
                "after_aria_expanded": after_expanded,
                "opened": bool(changed),
                "navigated": page.url != before_url,
            })
            if page.url == before_url:
                for nested in discover_controls(page, [item["selector"]]):
                    discovered.setdefault(nested["audit_id"], nested)
        except Exception as exc:  # noqa: BLE001 - one control must not abort the crawl
            proofs.append({
                "selector": item["selector"],
                "accessible_name": item["accessible_name"],
                "opened": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return sorted(discovered.values(), key=lambda item: int(item["audit_id"])), proofs


def visible_locator_text(locator) -> str:
    values = []
    for index in range(locator.count()):
        item = locator.nth(index)
        try:
            if item.is_visible():
                values.append(compact_text(item.inner_text()))
        except Exception:  # noqa: BLE001, S112 - stale DOM nodes are expected during reveals
            continue
    return compact_text(" ".join(values))


def inspect_route(
    page,
    *,
    requested_route: str,
    status: int | None,
    navigation_error: str | None,
    surface: LocalSurface,
    contract_route: dict | None,
    min_body_chars: int,
) -> tuple[dict, list[dict]]:
    failures: list[dict] = []
    actual_route = surface.logical_path(page.url)
    try:
        body = compact_text(page.locator("body").inner_text())
        title = compact_text(page.title())
    except Exception:  # noqa: BLE001 - DOM inspection is best effort evidence
        body = ""
        title = ""
    folded = body.casefold()
    defensive = sorted({phrase for phrase in DEFENSIVE_PHRASES if phrase in folded})
    if navigation_error:
        failures.append(failure(
            "navigation_error", navigation_error,
            route=requested_route, contract_route=contract_route,
        ))
    if status is None or status >= 400:
        failures.append(failure(
            "dead_destination", f"destination returned HTTP {status}",
            route=requested_route, contract_route=contract_route,
        ))
    if len(body) < min_body_chars:
        failures.append(failure(
            "empty_destination",
            f"meaningful body has {len(body)} characters; minimum is {min_body_chars}",
            route=requested_route, contract_route=contract_route,
        ))
    if defensive:
        failures.append(failure(
            "defensive_page",
            "rendered page contains a prohibited limitation/departure phrase",
            route=requested_route,
            contract_route=contract_route,
            evidence={"phrases": defensive},
        ))
    if contract_route is None:
        failures.append(failure(
            "undeclared_route",
            "discovered local destination has no route contract",
            route=requested_route,
        ))
        return ({
            "requested_path": requested_route,
            "actual_path": actual_route,
            "status": status,
            "title": title,
            "body_chars": len(body),
            "defensive_phrases": defensive,
            "archetype": "undeclared",
            "field_results": {},
            "content_signature": None,
            "passed": False,
        }, failures)

    alias_of = contract_route.get("alias_of")
    if actual_route != requested_route and not (
        canonical_query(actual_route) == canonical_query(requested_route)
        or
        alias_of and actual_route == alias_of
    ):
        failures.append(failure(
            "unexpected_route_navigation",
            f"requested {requested_route} but rendered {actual_route}",
            route=requested_route,
            contract_route=contract_route,
        ))

    field_results: dict[str, dict] = {}
    signature_parts = []
    for field_name in ("title", "context", "main", "return"):
        spec = contract_route["minimum_fields"][field_name]
        locator = page.locator(spec["selector"])
        text = visible_locator_text(locator)
        minimum = int(spec.get("min_chars", 1))
        passed = locator.count() > 0 and len(text) >= minimum
        result = {
            "selector": spec["selector"],
            "visible_text": text[:2000],
            "chars": len(text),
            "minimum_chars": minimum,
            "passed": passed,
        }
        if field_name == "return" and passed:
            expected_path = spec.get("expected_path")
            href = locator.first.get_attribute("href")
            absolute, logical = surface.resolve(page.url, href or "")
            result.update({
                "href": href,
                "resolved_url": absolute,
                "logical_path": logical,
                "expected_path": expected_path,
            })
            if logical != expected_path:
                passed = False
                result["passed"] = False
        field_results[field_name] = result
        if not passed:
            failures.append(failure(
                "archetype_field_missing",
                f"{contract_route['archetype']} route lacks valid {field_name} semantics",
                route=requested_route,
                contract_route=contract_route,
                selector=spec["selector"],
                evidence=result,
            ))
        if field_name in {"title", "context", "main"}:
            signature_parts.append(text.casefold())

    if alias_of:
        canonical = page.locator(contract_route["canonical_selector"])
        canonical_href = canonical.first.get_attribute("href") if canonical.count() else None
        _, logical = surface.resolve(page.url, canonical_href or "")
        if actual_route != alias_of and logical != alias_of:
            failures.append(failure(
                "alias_mapping_unproved",
                f"duplicate alias does not prove canonical mapping to {alias_of}",
                route=requested_route,
                contract_route=contract_route,
                selector=contract_route["canonical_selector"],
            ))

    signature = hashlib.sha256(
        "\n".join(signature_parts).encode("utf-8")
    ).hexdigest()
    return ({
        "requested_path": requested_route,
        "actual_path": actual_route,
        "status": status,
        "title": title,
        "body_chars": len(body),
        "defensive_phrases": defensive,
        "route_id": contract_route["id"],
        "family": contract_route["family"],
        "archetype": contract_route["archetype"],
        "provenance": contract_route["provenance"],
        "field_results": field_results,
        "content_signature": signature,
        "passed": not failures,
    }, failures)


def contract_matches(page, controls: list[dict], declared: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    discovered_by_id = {item["audit_id"]: item for item in controls}
    matches: dict[str, dict] = {}
    failures: list[dict] = []
    for control in declared:
        selector = control.get("runtime_selector") or control.get("selector")
        locator = page.locator(selector) if selector else page.locator("#__webharbor_missing_selector__")
        discovered = None
        if locator.count() == 1:
            audit_id = locator.get_attribute("data-webharbor-audit-id")
            discovered = discovered_by_id.get(audit_id or "")
        if discovered is None:
            semantic_candidates = [
                item for item in controls
                if control_identity_compatible(
                    control,
                    item,
                    left_route=control.get("source_path"),
                    right_route=item.get("source_path"),
                )
            ]
            if len(semantic_candidates) == 1:
                discovered = semantic_candidates[0]
            elif len(semantic_candidates) > 1:
                failures.append(failure(
                    "declared_control_ambiguous",
                    "semantic identity resolved multiple rendered controls",
                    route=control["source_path"], control=control,
                    evidence={"candidate_audit_ids": [item.get("audit_id") for item in semantic_candidates]},
                ))
                continue
        if discovered is None and locator.count() != 1:
            failures.append(failure(
                "declared_control_missing" if locator.count() == 0 else "declared_control_ambiguous",
                f"declared selector resolved {locator.count()} elements",
                route=control["source_path"], control=control,
            ))
            continue
        if not discovered:
            failures.append(failure(
                "declared_control_not_visible",
                "declared control is not visible at the desktop viewport after its discovery state",
                route=control["source_path"], control=control,
            ))
            continue
        if compact_text(discovered["accessible_name"]) != compact_text(control["accessible_name"]):
            failures.append(failure(
                "accessible_name_mismatch",
                "rendered accessible name differs from the contract",
                route=control["source_path"], control=control,
                evidence={
                    "expected": control["accessible_name"],
                    "actual": discovered["accessible_name"],
                },
            ))
            continue
        if discovered["control_type"] != control["control_type"]:
            failures.append(failure(
                "control_type_mismatch",
                "rendered control type differs from the contract",
                route=control["source_path"], control=control,
                evidence={
                    "expected": control["control_type"],
                    "actual": discovered["control_type"],
                },
            ))
            continue
        matches[control["id"]] = discovered
    matched_audit_ids = {item["audit_id"] for item in matches.values()}
    for item in controls:
        if item["audit_id"] in matched_audit_ids:
            continue
        failures.append(failure(
            "undeclared_active_control",
            "visible active-looking control is absent from the interaction contract",
            route=declared[0]["source_path"] if declared else None,
            selector=item["selector"],
            evidence={
                "accessible_name": item["accessible_name"],
                "control_type": item["control_type"],
                "revealed_by": item["revealed_by"],
            },
        ))
    return matches, failures


def apply_step(page, step: dict, timeout_ms: int) -> dict:
    locator = page.locator(step["selector"])
    if locator.count() != 1:
        raise RuntimeError(f"step selector {step['selector']!r} resolved {locator.count()} elements")
    action = step["action"]
    if action == "fill":
        locator.fill(str(step.get("value", "")), timeout=timeout_ms)
    elif action == "select":
        locator.select_option(str(step["value"]), timeout=timeout_ms)
    elif action == "check":
        locator.check(timeout=timeout_ms)
    elif action == "click":
        locator.click(timeout=timeout_ms)
    else:
        raise RuntimeError(f"unsupported preparation action {action!r}")
    return {"action": action, "selector": step["selector"], "value": step.get("value")}


def evaluate_assertion(page, assertion: dict) -> dict:
    locator = page.locator(assertion["selector"])
    kind = assertion["kind"]
    result = {"kind": kind, "selector": assertion["selector"], "passed": False}
    if kind == "visible":
        result["passed"] = locator.count() == 1 and locator.is_visible()
    elif kind == "hidden":
        result["passed"] = locator.count() == 0 or not locator.first.is_visible()
    elif kind == "text_contains":
        actual = visible_locator_text(locator)
        result.update({"actual": actual[:2000], "expected": assertion["value"]})
        result["passed"] = str(assertion["value"]) in actual
    elif kind == "attribute":
        actual = locator.first.get_attribute(assertion["name"]) if locator.count() == 1 else None
        result.update({"actual": actual, "expected": assertion["value"], "name": assertion["name"]})
        result["passed"] = actual == str(assertion["value"])
    elif kind == "value":
        actual = locator.first.input_value() if locator.count() == 1 else None
        result.update({"actual": actual, "expected": assertion["value"]})
        result["passed"] = actual == str(assertion["value"])
    elif kind == "checked":
        actual = locator.first.is_checked() if locator.count() == 1 else None
        result.update({"actual": actual, "expected": bool(assertion["value"])})
        result["passed"] = actual is bool(assertion["value"])
    elif kind == "count":
        actual = locator.count()
        result.update({"actual": actual, "expected": int(assertion["value"])})
        result["passed"] = actual == int(assertion["value"])
    else:
        result["error"] = f"unsupported assertion kind {kind!r}"
    return result


def sqlite_proof(workspace: Path, spec: dict) -> dict:
    database = (workspace / spec["database"]).resolve()
    try:
        database.relative_to(workspace.resolve())
    except ValueError:
        return {"passed": False, "reason": "SQLite database escapes the workspace"}
    query = str(spec.get("query") or "").strip()
    if not re.match(r"(?is)^select\b", query) or ";" in query.rstrip(";"):
        return {"passed": False, "reason": "SQLite proof query must be one SELECT statement"}
    try:
        with sqlite3.connect(database) as connection:
            rows = [list(row) for row in connection.execute(query, spec.get("params", [])).fetchall()]
    except Exception as exc:  # noqa: BLE001 - SQLite proof failures are reported
        return {"passed": False, "reason": f"{type(exc).__name__}: {exc}"}
    expected = spec.get("expected_rows")
    expected_count = spec.get("expected_count")
    passed = rows == expected if expected is not None else len(rows) == expected_count
    return {"passed": passed, "rows": rows, "expected_rows": expected, "expected_count": expected_count}


def reset_evidence_proof(workspace: Path, spec: dict) -> dict:
    path = (workspace / str(spec.get("path") or "")).resolve()
    try:
        path.relative_to(workspace.resolve())
    except ValueError:
        return {"passed": False, "reason": "reset evidence escapes the workspace"}
    actual_sha256 = sha256_file(path)
    expected_sha256 = spec.get("sha256")
    equal_hashes = spec.get("runtime_sha256") == spec.get("seed_sha256")
    return {
        "passed": bool(actual_sha256 and actual_sha256 == expected_sha256 and equal_hashes),
        "path": path.as_posix(),
        "sha256": actual_sha256,
        "expected_sha256": expected_sha256,
        "runtime_seed_equal": equal_hashes,
    }


def execute_control(
    *,
    context,
    surface: LocalSurface,
    workspace: Path,
    control: dict,
    timeout_ms: int,
    network: NetworkGuard,
) -> tuple[dict, list[dict]]:
    page = context.new_page()
    failures: list[dict] = []
    proof_steps: list[dict] = []
    network_start = len(network.events)
    status, navigation_error = goto(page, surface, control["source_path"], timeout_ms)
    proof_steps.append({
        "action": "navigate_source",
        "path": control["source_path"],
        "status": status,
        "error": navigation_error,
    })
    try:
        if navigation_error or status is None or status >= 400:
            raise RuntimeError(navigation_error or f"source route returned HTTP {status}")
        for selector in control.get("reveal", []):
            reveal = page.locator(selector)
            if reveal.count() != 1:
                raise RuntimeError(f"reveal selector {selector!r} resolved {reveal.count()} elements")
            reveal.click(timeout=timeout_ms)
            proof_steps.append({"action": "reveal", "selector": selector})
        outcome = control["outcome"]
        for step in outcome.get("steps", []):
            proof_steps.append(apply_step(page, step, timeout_ms))
        execution_selector = control.get("runtime_selector") or control["selector"]
        locator = page.locator(execution_selector)
        if locator.count() != 1 or not locator.is_visible():
            raise RuntimeError(
                f"control selector {execution_selector!r} is not one unique visible element"
            )
        if locator.is_disabled() or locator.get_attribute("aria-disabled") == "true":
            raise RuntimeError("active-looking control is disabled")
        before = surface.logical_path(page.url)
        kind = outcome["kind"]
        actual_kind = kind
        if kind == "popup":
            with context.expect_page(timeout=timeout_ms) as popup_info:
                locator.click(timeout=timeout_ms)
            destination = popup_info.value
            try:
                destination.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                actual = surface.logical_path(destination.url)
                proof_steps.append({"action": "popup", "actual_path": actual})
            finally:
                destination.close()
            passed = actual == outcome["expected_path"]
        elif kind == "download":
            with page.expect_download(timeout=timeout_ms) as download_info:
                locator.click(timeout=timeout_ms)
            download = download_info.value
            suggested = download.suggested_filename
            proof_steps.append({"action": "download", "suggested_filename": suggested, "url": download.url})
            passed = bool(suggested) and (
                not outcome.get("filename_regex")
                or re.search(outcome["filename_regex"], suggested) is not None
            )
        else:
            action = outcome.get("action", "click")
            if action == "fill":
                locator.fill(str(outcome.get("value", "")), timeout=timeout_ms)
            elif action == "select":
                locator.select_option(str(outcome["value"]), timeout=timeout_ms)
            elif action == "check":
                locator.check(timeout=timeout_ms)
            elif action == "submit":
                submit = page.locator(outcome["submit_selector"])
                if submit.count() != 1:
                    raise RuntimeError(
                        f"submit selector {outcome['submit_selector']!r} resolved {submit.count()} elements"
                    )
                submit.click(timeout=timeout_ms)
            else:
                locator.click(timeout=timeout_ms)
            if kind == "navigation":
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    pass
                actual = surface.logical_path(page.url)
                actual_kind = "navigation" if actual != before else "in_page_state_change"
                proof_steps.append({
                    "action": action,
                    "before_path": before,
                    "actual_path": actual,
                    "expected_path": outcome["expected_path"],
                })
                passed = actual == outcome["expected_path"]
            else:
                actual = surface.logical_path(page.url)
                actual_kind = "in_page_state_change" if actual == before else "unexpected_navigation"
                assertions = [evaluate_assertion(page, item) for item in outcome["assertions"]]
                proof_steps.append({
                    "action": action,
                    "before_path": before,
                    "actual_path": actual,
                    "assertions": assertions,
                })
                passed = actual == before and all(item["passed"] for item in assertions)
                if kind == "mutation":
                    sqlite = sqlite_proof(workspace, outcome["sqlite"])
                    reset = reset_evidence_proof(workspace, outcome["reset_evidence"])
                    proof_steps.extend([
                        {"action": "sqlite_assert", **sqlite},
                        {"action": "reset_evidence", **reset},
                    ])
                    passed = passed and sqlite["passed"] and reset["passed"]
        if not passed:
            failures.append(failure(
                "control_outcome_mismatch",
                f"declared {kind} outcome did not occur",
                route=control["source_path"], control=control,
                evidence={"proof_steps": proof_steps},
            ))
    except Exception as exc:  # noqa: BLE001 - retain execution failure evidence
        actual_kind = "error"
        failures.append(failure(
            "control_execution_failed",
            f"{type(exc).__name__}: {exc}",
            route=control["source_path"], control=control,
            evidence={"proof_steps": proof_steps},
        ))
    finally:
        page.close()
    network_events = network.events[network_start:]
    if network_events:
        failures.append(failure(
            "unexpected_network",
            "control caused a blocked request outside the declared local surface",
            route=control["source_path"], control=control,
            evidence={"requests": network_events},
        ))
    return ({
        "id": control["id"],
        "source_path": control["source_path"],
        "selector": control["selector"],
        "accessible_name": control["accessible_name"],
        "control_type": control["control_type"],
        "declared_outcome": control["outcome"]["kind"],
        "observed_outcome": actual_kind,
        "critical": control["critical"],
        "proof_steps": proof_steps,
        "unexpected_network": network_events,
        "passed": not failures,
    }, failures)


def verify(
    *,
    workspace: Path,
    site: str,
    base_url: str,
    contract_path: Path,
    evidence_dir: Path,
    max_pages: int | None = None,
    max_depth: int | None = None,
    timeout_ms: int = 10_000,
    gateway_prefix: str | None = None,
    route_map_timeout_seconds: int = 30,
    candidate_site_path: Path | None = None,
    viewport: dict[str, int] | None = None,
    browser_path: Path | None = None,
    dependency_path: Path | None = None,
    assigned_ids: list[str] | None = None,
    shared_artifact_dir: Path | None = None,
    stage: str = "authoritative",
) -> tuple[dict, dict, dict]:
    workspace = workspace.resolve()
    site_root = (candidate_site_path or (workspace / "sites" / site)).resolve()
    if not site_root.is_dir():
        raise ContractError(f"missing site implementation: {site_root}")
    source_contract = load_contract(contract_path, expected_site=site)
    provenance_failures = provenance_file_failures(source_contract, workspace)
    if provenance_failures:
        raise ContractError("; ".join(provenance_failures))
    surface = LocalSurface.from_base_url(base_url, gateway_prefix)
    shared_artifact_dir = shared_artifact_dir.resolve() if shared_artifact_dir else None
    inventory_path = shared_artifact_dir / "code-interaction-inventory.json" if shared_artifact_dir else None
    if inventory_path and inventory_path.is_file():
        try:
            code_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            if not isinstance(code_inventory, dict) or code_inventory.get("inventory") != INVENTORY_SCHEMA:
                raise ValueError("invalid shared inventory")
        except (OSError, ValueError, UnicodeDecodeError):
            if shared_artifact_dir is not None:
                raise ContractError("shared preflight inventory is corrupt or has an unsupported schema")
            code_inventory = inventory_site(
                site_root, site=site, timeout_seconds=max(1, route_map_timeout_seconds),
                selected_root=site_root.as_posix(), dependency_path=dependency_path,
            )
    else:
        code_inventory = inventory_site(
            site_root,
            site=site,
            timeout_seconds=max(1, route_map_timeout_seconds),
            selected_root=site_root.as_posix(),
            dependency_path=dependency_path,
        )
        if inventory_path:
            inventory_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(inventory_path, code_inventory)
    declared_routes = {item["path"]: item for item in source_contract["route_inventory"]}
    declared_controls_by_route: dict[str, list[dict]] = {}
    for item in source_contract["control_inventory"]:
        declared_controls_by_route.setdefault(item["source_path"], []).append(item)
    # The preflight stage is authoritative static/browser baseline and emits
    # the complete standard artifacts once.  Delta stages alone narrow work to
    # their assigned route/control IDs.
    assigned_set = set(assigned_ids or []) if assigned_ids is not None else None
    assigned_route_ids = {
        value.split(":", 1)[1] for value in (assigned_set or set()) if value.startswith("route:")
    }
    assigned_control_ids = {
        value.split(":", 1)[1] for value in (assigned_set or set()) if value.startswith("control:")
    }
    assigned_route_paths = {
        canonical_query(item["path"]) or "/"
        for item in source_contract["route_inventory"]
        if item["id"] in assigned_route_ids
    }
    assigned_control_source_paths = {
        canonical_query(item["source_path"]) or "/"
        for item in source_contract["control_inventory"]
        if item["id"] in assigned_control_ids
    }
    failure_slice_paths = assigned_route_paths | assigned_control_source_paths
    # Matching/discovery remains contract-wide so an assigned route does not
    # manufacture ``undeclared_active_control`` failures for controls owned by
    # another batch.  Execution is narrowed below by ``controls_to_execute``;
    # this keeps each delta authoritative for its assignment without treating
    # the rest of the contract as absent.

    page_budget = max_pages if max_pages is not None else source_contract["crawl"]["max_pages"]
    depth_budget = max_depth if max_depth is not None else source_contract["crawl"]["max_depth"]
    if page_budget < 1 or depth_budget < 0:
        raise ContractError("page/depth budgets are invalid")

    failures: list[dict] = []
    unresolved: list[dict] = []
    route_results: list[dict] = []
    discovered_controls: list[dict] = []
    control_matches: dict[str, dict] = {}
    discovery_proofs: list[dict] = []
    source_dom_states: list[dict] = []
    observed_edges: list[dict] = []
    if assigned_set is None:
        initial_routes = list(source_contract["crawl"]["start_routes"])
        allowed_target_keys: set[str] | None = None
    else:
        initial_routes = [
            item["path"] for item in source_contract["route_inventory"]
            if item["id"] in assigned_route_ids
        ]
        initial_routes.extend(
            item["source_path"]
            for item in source_contract["control_inventory"]
            if item["id"] in assigned_control_ids
        )
        initial_routes = sorted(set(initial_routes)) or ["/"]
        allowed_target_keys = assigned_route_paths
    queue: deque[tuple[str, int]] = deque((route, 0) for route in initial_routes)
    queued = {canonical_query(route) or "/" for route, _ in queue}
    visited: set[str] = set()

    with sync_playwright() as playwright:
        launch_kwargs = {"headless": True}
        if browser_path is not None:
            launch_kwargs["executable_path"] = str(browser_path.resolve())
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport=viewport or source_contract["viewport"])
        network = NetworkGuard(surface)
        network.install(context)
        page = context.new_page()
        while queue:
            route, depth = queue.popleft()
            route_key = canonical_query(route) or "/"
            if route_key in visited:
                continue
            visited.add(route_key)
            route_contract = declared_routes.get(route)
            network_start = len(network.events)
            status, navigation_error = goto(page, surface, route, timeout_ms)
            route_result, route_failures = inspect_route(
                page,
                requested_route=route,
                status=status,
                navigation_error=navigation_error,
                surface=surface,
                contract_route=route_contract,
                min_body_chars=int(source_contract.get("minimum_body_chars", 80)),
            )
            route_result["depth"] = depth
            route_results.append(route_result)
            failures.extend(route_failures)
            route_network = network.events[network_start:]
            if route_network:
                failures.append(failure(
                    "unexpected_network",
                    "route requested a resource outside the declared local surface",
                    route=route,
                    contract_route=route_contract,
                    evidence={"requests": route_network},
                ))
            if navigation_error or status is None or status >= 400:
                continue
            controls, reveal_proofs = reveal_nested_controls(page, timeout_ms)
            discovery_proofs.extend({"source_path": route, **item} for item in reveal_proofs)
            source_dom_states.extend(probe_source_controls(
                page,
                route,
                code_inventory["controls"],
            ))
            for item in controls:
                item = {"source_path": route, **item}
                raw_target = item.get("href") or item.get("action")
                if raw_target:
                    _absolute_target, logical_target = surface.resolve(page.url, raw_target)
                    item["normalized_target"] = canonical_query(logical_target) if logical_target else None
                    item["target_kind"] = "local" if logical_target else "external"
                item["control_identity"] = canonical_control_identity(item, route=route)
                family_digest = hashlib.sha256(
                    "\0".join(semantic_control_key(item, route=route)).encode("utf-8")
                ).hexdigest()[:20]
                item["family_id"] = f"control-family-{family_digest}"
                discovered_controls.append(item)
                raw_target = item.get("href")
                if not raw_target:
                    continue
                absolute, logical = surface.resolve(page.url, raw_target)
                observed_edges.append({
                    "from": route,
                    "to": logical,
                    "absolute_url": absolute,
                    "selector": item["selector"],
                    "accessible_name": item["accessible_name"],
                    "control_type": item["control_type"],
                    "kind": "popup" if item.get("target", "").casefold() == "_blank" else "navigation",
                    "audit_id": item.get("audit_id"),
                })
                if item["control_type"] == "download":
                    observed_edges[-1]["kind"] = "download"
                    continue
                logical_key = canonical_query(logical) if logical else None
                if logical is None or logical_key in queued:
                    continue
                if allowed_target_keys is not None and logical_key not in allowed_target_keys:
                    continue
                if depth + 1 > depth_budget or len(queued) >= page_budget:
                    reason = "max_depth" if depth + 1 > depth_budget else "max_pages"
                    gap = {
                        "code": "unaudited_limit",
                        "route": logical or absolute,
                        "source_path": route,
                        "selector": item["selector"],
                        "accessible_name": item["accessible_name"],
                        "budget": reason,
                    }
                    unresolved.append(gap)
                    failures.append(failure(
                        "unaudited_limit",
                        f"discovered target remained after {reason} budget exhaustion",
                        route=route,
                        contract_route=route_contract,
                        selector=item["selector"],
                        evidence=gap,
                    ))
                    continue
                queued.add(logical_key or logical)
                queue.append((logical, depth + 1))
            declared = declared_controls_by_route.get(route, [])
            matches, match_failures = contract_matches(page, controls, declared)
            control_matches.update(matches)
            failures.extend(match_failures)
            for control in declared:
                outcome = control.get("outcome") or {}
                if outcome.get("kind") not in {"navigation", "popup"}:
                    continue
                if assigned_set is not None and control["id"] not in assigned_control_ids:
                    continue
                logical = outcome["expected_path"]
                observed_edges.append({
                    "from": route,
                    "to": logical,
                    "absolute_url": surface.to_url(logical),
                    "selector": control["selector"],
                    "accessible_name": control["accessible_name"],
                    "control_type": control["control_type"],
                    "kind": outcome["kind"],
                    "audit_id": None,
                })
                logical_key = canonical_query(logical) or logical
                if logical_key in queued:
                    continue
                if allowed_target_keys is not None and logical_key not in allowed_target_keys:
                    continue
                if depth + 1 > depth_budget or len(queued) >= page_budget:
                    reason = "max_depth" if depth + 1 > depth_budget else "max_pages"
                    gap = {
                        "code": "unaudited_limit",
                        "route": logical,
                        "source_path": route,
                        "selector": control["selector"],
                        "accessible_name": control["accessible_name"],
                        "budget": reason,
                    }
                    unresolved.append(gap)
                    failures.append(failure(
                        "unaudited_limit",
                        f"declared control target remained after {reason} budget exhaustion",
                        route=route,
                        contract_route=route_contract,
                        control=control,
                        evidence=gap,
                    ))
                    continue
                queued.add(logical_key)
                queue.append((logical, depth + 1))

        page.close()
        for route in sorted(
            route for route in declared_routes
            if (canonical_query(route) or "/") not in visited
            and (assigned_set is None or declared_routes[route]["id"] in assigned_route_ids)
        ):
            contract_route = declared_routes[route]
            failures.append(failure(
                "declared_route_unreached",
                "declared route was not reachable from the bounded route/control graph",
                route=route,
                contract_route=contract_route,
            ))

        control_results: list[dict] = []
        controls_to_execute = [
            control for control in source_contract["control_inventory"]
            if assigned_set is None or control["id"] in assigned_control_ids
        ]
        for control in controls_to_execute:
            if control["id"] not in control_matches:
                control_results.append({
                    "id": control["id"],
                    "source_path": control["source_path"],
                    "selector": control["selector"],
                    "accessible_name": control["accessible_name"],
                    "control_type": control["control_type"],
                    "declared_outcome": control["outcome"]["kind"],
                    "observed_outcome": "not_discovered",
                    "critical": control["critical"],
                    "proof_steps": [],
                    "unexpected_network": [],
                    "passed": False,
                })
                continue
            result, outcome_failures = execute_control(
                context=context,
                surface=surface,
                workspace=workspace,
                control=control,
                timeout_ms=timeout_ms,
                network=network,
            )
            control_results.append(result)
            failures.extend(outcome_failures)
        context.close()
        browser.close()

    signatures: dict[str, list[dict]] = {}
    for result in route_results:
        if result.get("content_signature") and result.get("route_id"):
            signatures.setdefault(result["content_signature"], []).append(result)
    for signature, items in sorted(signatures.items()):
        canonical_paths = {item["requested_path"] for item in items}
        if len(canonical_paths) < 2:
            continue
        non_alias = [
            item for item in items
            if not declared_routes[item["requested_path"]].get("alias_of")
        ]
        if len(non_alias) > 1:
            for item in items:
                failures.append(failure(
                    "generic_fallback_duplicate",
                    "multiple non-alias routes share the same title/context/main signature",
                    route=item["requested_path"],
                    contract_route=declared_routes[item["requested_path"]],
                    evidence={"signature": signature, "routes": sorted(canonical_paths)},
                ))

    control_by_id = {item["id"]: item for item in control_results}
    route_by_path = {item["requested_path"]: item for item in route_results}
    workflow_results = []
    workflows_to_check = [] if assigned_set is not None else source_contract["critical_workflows"]
    for workflow in workflows_to_check:
        route_checks = [
            {"path": route, "passed": bool(route_by_path.get(route, {}).get("passed"))}
            for route in workflow["routes"]
        ]
        control_checks = [
            {"id": control, "passed": bool(control_by_id.get(control, {}).get("passed"))}
            for control in workflow["controls"]
        ]
        passed = all(item["passed"] for item in [*route_checks, *control_checks])
        workflow_results.append({
            "id": workflow["id"],
            "routes": route_checks,
            "controls": control_checks,
            "passed": passed,
        })
        if not passed:
            failures.append(failure(
                "critical_workflow_failed",
                f"critical workflow {workflow['id']} did not preserve all declared semantics",
                evidence={"routes": route_checks, "controls": control_checks},
            ))

    canonical_discovered_controls = canonicalize_controls(discovered_controls)
    derived_control_contracts = derive_browser_control_contracts(
        source_contract,
        canonical_discovered_controls,
        observed_edges,
    )
    # Source declarations remain authoritative for their exact controls.  A
    # family already covered by a source declaration is not duplicated in the
    # effective contract; every other browser family is added as an explicit,
    # fail-closed run-local declaration for triangulation and closure evidence.
    effective_derived_controls = [
        item for item in derived_control_contracts
        if not item.get("source_basis_ids")
    ]
    effective_contract = dict(source_contract)
    effective_contract["control_inventory"] = [
        *source_contract["control_inventory"],
        *effective_derived_controls,
    ]
    effective_contract["_source_contract_view"] = source_contract
    derived_failures: list[dict] = []
    for control in effective_derived_controls:
        control_results.append({
            "id": control["id"],
            "source_path": control["source_path"],
            "selector": control["selector"],
            "accessible_name": control["accessible_name"],
            "control_type": control["control_type"],
            "declared_outcome": control["outcome"]["kind"],
            "observed_outcome": "not_proved",
            "critical": False,
            "derived": True,
            "proof_steps": [],
            "unexpected_network": [],
            "passed": False,
        })
        derived_failures.append(failure(
            "derived_control_unproved",
            "run-local derived control has browser discovery but no source-grounded proof",
            route=control["source_path"],
            control=control,
            selector=control["selector"],
            evidence={
                "family_id": control.get("family_id"),
                "raw_observation_count": control.get("raw_observation_count", 0),
                "source_basis_ids": control.get("source_basis_ids", []),
            },
        ))
    source_contract_sha256 = sha256_bytes(canonical_json(source_contract))
    canonical_edges = canonicalize_edges(observed_edges)
    failures.extend(derived_failures)
    interaction_diff = build_interaction_diff(
        site=site,
        inventory=code_inventory,
        contract=effective_contract,
        contract_sha256=source_contract_sha256,
        discovered_controls=discovered_controls,
        control_results=control_results,
        observed_edges=canonical_edges,
        source_dom_states=source_dom_states,
        existing_failures=failures,
    )
    # The diff retains canonical families; normalize its hard-gap lists once
    # more because browser outcomes can repeat the same semantic failure across
    # reveal states or query-order variants.
    for category, items in interaction_diff["gap_categories"].items():
        interaction_diff["gap_categories"][category], _ = deduplicate_failures(items)
    interaction_diff["hard_gaps"], _ = deduplicate_failures(interaction_diff["hard_gaps"])
    interaction_diff["hard_gap_count"] = len(interaction_diff["hard_gaps"])
    interaction_diff["status"] = "passed" if not interaction_diff["hard_gaps"] else "failed"
    interaction_diff["browser_identity"].update({
        "raw_controls": len(discovered_controls),
        "canonical_control_families": len(canonicalize_controls(discovered_controls)),
        "raw_observed_edges": len(observed_edges),
        "canonical_observed_edges": len(canonical_edges),
    })
    interaction_diff["payload_sha256"] = hashlib.sha256(canonical_json(interaction_diff)).hexdigest()
    failures.extend(interaction_diff["hard_gaps"])

    # A delta is authoritative only for its assigned route/control slice.  The
    # browser still performs full triangulation so source/contract evidence is
    # not weakened, but global workflow or another batch's control failures
    # must not poison this batch's terminal status.  The owning batch will
    # report those failures and the final merge will aggregate them.
    if assigned_set is not None:
        def belongs_to_slice(item: dict) -> bool:
            control_id = item.get("control_id")
            route = canonical_query(item.get("route")) if item.get("route") else None
            return (
                (control_id in assigned_control_ids)
                or (route in failure_slice_paths)
                or (control_id is None and route is None)
                or item.get("code") in {"terminal_error", "candidate_mutated"}
            )

        interaction_diff["hard_gaps"] = [
            item for item in interaction_diff.get("hard_gaps", []) if belongs_to_slice(item)
        ]
        interaction_diff["hard_gap_count"] = len(interaction_diff["hard_gaps"])
        interaction_diff["status"] = "passed" if not interaction_diff["hard_gaps"] else "failed"
        interaction_diff["payload_sha256"] = hashlib.sha256(canonical_json(interaction_diff)).hexdigest()
        failures = [item for item in failures if belongs_to_slice(item)]

    raw_failure_count = len(failures)
    failures, _ = deduplicate_failures(failures)
    unresolved, _ = deduplicate_failures(unresolved)

    failures = sorted(
        failures,
        key=lambda item: (
            str(item.get("route") or ""),
            str((item.get("source_location") or {}).get("file") or ""),
            int((item.get("source_location") or {}).get("line") or 0),
            str(item.get("control_id") or ""),
            str(item.get("code") or ""),
            str(item.get("selector") or ""),
        ),
    )
    contract_surface = derive_site_contract_surface(
        source_contract,
        route_results=route_results,
        control_families=canonical_discovered_controls,
    )
    derived_by_family = {
        item.get("family_id"): item for item in derived_control_contracts
    }
    for family in contract_surface["control_families"]:
        derived = derived_by_family.get(family.get("family_id"))
        if derived:
            family.update({
                "selector": derived["selector"],
                "source_path": derived["source_path"],
                "derived_control_id": derived["id"],
                "source_basis_ids": derived.get("source_basis_ids", []),
                "proof_status": "source_proved" if derived.get("source_basis_ids") else "unproved",
            })
    contract_surface["derived_control_contracts"] = derived_control_contracts
    contract_surface["coverage"]["effective_declared_control_count"] = len(
        effective_contract["control_inventory"]
    )
    contract_surface["coverage"]["derived_control_contract_count"] = len(
        derived_control_contracts
    )
    if assigned_set is not None:
        assigned_results = [item for item in control_results if item.get("id") in assigned_control_ids]
        discovered_total = len(assigned_results)
        resolved_discovered = sum(1 for item in assigned_results if item.get("passed"))
    else:
        discovered_total = len(canonical_discovered_controls)
        resolved_discovered = None
    passed_control_ids = {item["id"] for item in control_results if item["passed"]}
    passed_controls = [
        control for control in source_contract["control_inventory"]
        if control["id"] in passed_control_ids
    ]
    resolved_family_ids = {
        family.get("family_id")
        for family in canonical_discovered_controls
        if any(
            control.get("selector") == family.get("selector")
            and any(route_equivalent(rule, family.get("source_path")) for rule in [control.get("source_path")])
            for control in passed_controls
        )
    }
    if resolved_discovered is None:
        resolved_discovered = sum(
            1 for family in canonical_discovered_controls
            if family.get("family_id") in resolved_family_ids
        )
    resolution_rate = round(100 * resolved_discovered / discovered_total, 2) if discovered_total else 0.0
    critical_passed = sum(1 for item in workflow_results if item["passed"])
    critical_rate = 100.0 if assigned_set is not None else round(100 * critical_passed / len(workflow_results), 2)
    dead_codes = {"dead_destination", "empty_destination", "navigation_error"}
    dead_count = sum(item["code"] in dead_codes for item in failures)
    defensive_count = sum(item["code"] == "defensive_page" for item in failures)
    unexpected_requests = sorted(
        {json.dumps(item, sort_keys=True): item for item in network.events}.values(),
        key=lambda item: (item["url"], item["kind"], item["resource_type"]),
    )
    complete = (
        not failures
        and resolution_rate == 100.0
        and critical_rate == 100.0
        and dead_count == 0
        and defensive_count == 0
        and not unexpected_requests
        and not unresolved
        and interaction_diff["hard_gap_count"] == 0
    )
    fingerprint = candidate_fingerprint(
        site_root,
        source_contract,
        code_inventory_sha256=code_inventory["payload_sha256"],
        interaction_diff_sha256=interaction_diff["payload_sha256"],
        analyzer=ANALYZER_TOOL,
    )
    code_inventory["candidate_fingerprint"] = fingerprint
    interaction_diff["candidate_fingerprint"] = fingerprint
    normalized_contract = contract_artifact(source_contract, contract_path, fingerprint)
    normalized_contract["derived_surface"] = contract_surface
    closure = {
        "schema_version": 2,
        "closure": CLOSURE_SCHEMA,
        "tool": VERIFIER_TOOL,
        "status": "passed" if complete else "failed",
        "site": site,
        "profile": "immersive",
        "candidate_fingerprint": fingerprint,
        "code_inventory": {
            "path": (evidence_dir / "code-interaction-inventory.json").as_posix(),
            "schema": INVENTORY_SCHEMA,
            "analyzer": ANALYZER_TOOL,
            "analyzer_implementation_sha256": code_inventory["analyzer_implementation_sha256"],
            "payload_sha256": code_inventory["payload_sha256"],
        },
        "interaction_diff": {
            "path": (evidence_dir / "interaction-diff.json").as_posix(),
            "schema": interaction_diff["diff"],
            "analyzer": ANALYZER_TOOL,
            "payload_sha256": interaction_diff["payload_sha256"],
            "hard_gap_count": interaction_diff["hard_gap_count"],
        },
        "contract_identity": normalized_contract["source_contract"],
        "surface": {
            "base_url": surface.root_url,
            "origin": surface.origin,
            "gateway_prefix": surface.gateway_prefix or None,
        },
        "viewport": viewport or source_contract["viewport"],
        "crawl": {
            "max_pages": page_budget,
            "max_depth": depth_budget,
            "visited_pages": len(visited),
            "maximum_depth_visited": max((item["depth"] for item in route_results), default=0),
            "inventory_complete": not unresolved,
        },
        "route_graph": {
            "declared_edges": source_contract["route_graph"],
            "observed_edges": sorted(
                canonical_edges,
                key=lambda item: (item["from"], str(item["to"]), item["selector"]),
            ),
        },
        "page_archetypes": sorted(route_results, key=lambda item: item["requested_path"]),
        "control_inventory": sorted(
            discovered_controls,
            key=lambda item: (item["source_path"], int(item["audit_id"])),
        ),
        "source_control_dom_states": sorted(
            source_dom_states,
            key=lambda item: (item["route"], item["source_id"]),
        ),
        "control_outcomes": sorted(control_results, key=lambda item: item["id"]),
        "disclosure_proofs": sorted(
            discovery_proofs,
            key=lambda item: (item["source_path"], item["selector"]),
        ),
        "critical_workflows": workflow_results,
        "canonical_control_families": canonical_discovered_controls,
        "site_contract_derivation": contract_surface,
        "loaded_local_resources": sorted(
            {json.dumps(item, sort_keys=True): item for item in network.loaded}.values(),
            key=lambda item: (item["url"], item["resource_type"]),
        ),
        "unexpected_requests": unexpected_requests,
        "unresolved_items": failures,
        "budget_unresolved_items": unresolved,
        "failures": failures,
        "raw_counts": {
            "declared_routes": len(declared_routes),
            "visited_routes": len(visited),
            "declared_controls": len(effective_contract["control_inventory"]),
            "source_declared_controls": len(source_contract["control_inventory"]),
            "derived_declared_controls": len(effective_derived_controls),
            "derived_control_contracts": len(derived_control_contracts),
            "inventoried_active_controls": discovered_total,
            "raw_inventoried_active_controls": len(discovered_controls),
            "canonical_control_families": len(canonical_discovered_controls),
            "resolved_active_controls": resolved_discovered,
            "source_active_controls": len(code_inventory["controls"]),
            "actual_public_get_routes": len(code_inventory["actual_route_map"]["public_get_routes"]),
            "triangulation_hard_gaps": interaction_diff["hard_gap_count"],
            "critical_workflows": len(workflow_results),
            "critical_workflows_passed": critical_passed,
            "dead_empty_or_404_destinations": dead_count,
            "defensive_pages": defensive_count,
            "unexpected_external_requests": len(unexpected_requests),
            "unaudited_limit": sum(item["code"] == "unaudited_limit" for item in failures),
            "raw_failure_observations": raw_failure_count,
            "deduplicated_failure_items": len(failures),
            "raw_observed_edges": len(observed_edges),
            "deduplicated_observed_edges": len(canonical_edges),
            "loaded_local_resources": len(network.loaded),
        },
        "measured_rates": {
            "active_control_resolution_percent": resolution_rate,
            "critical_workflow_semantics_percent": critical_rate,
        },
        # Compatibility view consumed by the existing SDK closure loop.  The
        # immersive profile still uses the stricter raw gates above.
        "clickthrough": {
            "requested_min_percent": 100.0,
            "meaningful_local_percent": resolution_rate,
        },
        "unimplemented_links": failures,
        "resource_issues": unexpected_requests,
        "browser_errors": [item["message"] for item in failures if item["code"] == "navigation_error"],
        "next_candidates": sorted({
            str(item.get("route")) for item in failures
            if item.get("route") and item["code"] in {
                "undeclared_route", "dead_destination", "empty_destination",
                "declared_route_unreached", "unaudited_limit",
            }
        }),
    }
    repair = build_repair_package(
        site=site,
        fingerprint=fingerprint,
        failures=failures,
    )
    closure["repair_package"] = {
        "path": (evidence_dir / "builder-repair-package.json").as_posix(),
        "failure_count": repair["failure_count"],
        "group_count": repair["group_count"],
    }
    if assigned_ids is not None or stage == "delta":
        # Delta workers must never replicate the complete closure or contract.
        # Their caller receives the bounded in-memory result and checkpoints
        # only the assigned outcomes/failures plus shared-artifact references.
        atomic_json(evidence_dir / "delta-result.json", {
            "schema": "webharbor-interaction-verification-delta-v1",
            "assigned_ids": sorted(assigned_ids or []),
            "status": closure["status"],
            "outcomes": [
                item for item in control_results
                if assigned_ids is None or f"control:{item.get('id')}" in set(assigned_ids)
            ],
            "failures": [
                item for item in failures
                if assigned_ids is None
                or f"control:{item.get('control_id')}" in set(assigned_ids)
                or f"route:{item.get('route')}" in set(assigned_ids)
            ],
        })
    else:
        atomic_json(evidence_dir / "interaction-contract.json", normalized_contract)
        atomic_json(evidence_dir / "code-interaction-inventory.json", code_inventory)
        atomic_json(evidence_dir / "interaction-diff.json", interaction_diff)
        atomic_json(evidence_dir / "interaction-closure.json", closure)
        atomic_json(evidence_dir / "builder-repair-package.json", repair)
    return normalized_contract, closure, repair


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="Workspace containing sites/<site>.")
    parser.add_argument("--site", required=True, help="Evidence site id and site implementation directory.")
    parser.add_argument("--site-path", type=Path, help="Explicit candidate site directory; never inferred when supplied.")
    parser.add_argument("--base-url", required=True, help="Running local mirror root, direct or /p/<port>/ gateway URL.")
    parser.add_argument(
        "--contract",
        type=Path,
        help="Immersive declaration; defaults to sites/<site>/interaction_contract.json.",
    )
    parser.add_argument(
        "--route-map-timeout-seconds",
        type=int,
        default=30,
        help="Bound for isolated Flask route-map import.",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="Output directory; defaults to mirror_factory/evidence/<site>/.",
    )
    parser.add_argument("--max-pages", type=int, help="Override the declared page budget; exhaustion still fails closed.")
    parser.add_argument("--max-depth", type=int, help="Override the declared depth budget; exhaustion still fails closed.")
    parser.add_argument("--timeout-ms", type=int, default=10_000)
    parser.add_argument("--browser-path", type=Path, help="Explicit Chromium executable path.")
    parser.add_argument("--dependency-path", type=Path, help="Explicit Python dependency directory for isolated route import.")
    parser.add_argument("--viewport-width", type=int, help="Override contract viewport width.")
    parser.add_argument("--viewport-height", type=int, help="Override contract viewport height.")
    parser.add_argument(
        "--gateway-prefix",
        help="Explicit /p/<port>/ prefix; otherwise inferred from --base-url.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    contract_path = (
        args.contract.resolve()
        if args.contract
        else workspace / "sites" / args.site / "interaction_contract.json"
    )
    evidence_dir = (
        args.evidence_dir.resolve()
        if args.evidence_dir
        else workspace / "mirror_factory" / "evidence" / args.site
    )
    viewport = None
    if args.viewport_width is not None or args.viewport_height is not None:
        if args.viewport_width is None or args.viewport_height is None or args.viewport_width < 1 or args.viewport_height < 1:
            print(json.dumps({"status": "invalid_contract", "error": "viewport width and height must be positive together"}, sort_keys=True))
            return 3
        viewport = {"width": args.viewport_width, "height": args.viewport_height}
    try:
        _contract, closure, repair = verify(
            workspace=workspace,
            site=args.site,
            base_url=args.base_url,
            contract_path=contract_path,
            evidence_dir=evidence_dir,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            timeout_ms=max(1000, args.timeout_ms),
            gateway_prefix=args.gateway_prefix,
            route_map_timeout_seconds=max(1, args.route_map_timeout_seconds),
            candidate_site_path=args.site_path.resolve() if args.site_path else None,
            viewport=viewport,
            browser_path=args.browser_path.resolve() if args.browser_path else None,
            dependency_path=args.dependency_path.resolve() if args.dependency_path else None,
        )
    except ContractError as exc:
        print(json.dumps({
            "status": "invalid_contract",
            "error": str(exc),
            "contract": contract_path.as_posix(),
        }, ensure_ascii=False, sort_keys=True))
        return 3
    print(json.dumps({
        "status": closure["status"],
        "closure": (evidence_dir / "interaction-closure.json").as_posix(),
        "contract": (evidence_dir / "interaction-contract.json").as_posix(),
        "repair_package": (evidence_dir / "builder-repair-package.json").as_posix(),
        "code_inventory": (evidence_dir / "code-interaction-inventory.json").as_posix(),
        "interaction_diff": (evidence_dir / "interaction-diff.json").as_posix(),
        "analyzer": ANALYZER_TOOL,
        "triangulation_hard_gaps": closure["raw_counts"]["triangulation_hard_gaps"],
        "failures": len(closure["failures"]),
        "repair_groups": repair["group_count"],
        "active_control_resolution_percent": closure["measured_rates"]["active_control_resolution_percent"],
        "critical_workflow_semantics_percent": closure["measured_rates"]["critical_workflow_semantics_percent"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if closure["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
