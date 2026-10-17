"""Acquire a bounded URL surface and compile a frozen synthetic replay bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .release import atomic_json, sha256_file
from .synthetic_replay import compile_synthetic_replay_contract
from .synthetic_replay_capture import freeze_replay_capture, trusted_dom_topology_script
from .synthetic_visual import (
    AuthExpiredError,
    CONTENT_STRATEGY,
    CaptureFailedError,
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    GEOMETRY_SCRIPT,
    REFERENCE_SCHEMA,
    SANITIZER_VERSION,
    SourceBlockedError,
    SyntheticVisualError,
    _check_robots,
    _is_private_host,
    _origin,
    canonical_sha256,
    geometry_metrics,
    geometry_quality,
    load_auth_state,
    load_visual_reference,
    sanitize_screenshot,
    utc_now,
    validate_geometry,
)


REPLAY_ACQUISITION_SCHEMA = "webanything-replay-acquisition-v1"
SENSITIVE_QUERY_RE = re.compile(
    r"(?:^|_)(?:auth|code|cookie|credential|key|password|secret|session|token)(?:_|$)",
    re.IGNORECASE,
)
UNSAFE_ROUTE_RE = re.compile(
    r"(?:^|[/_-])(?:delete|destroy|logout|remove|signout|unsubscribe)(?:[/_.-]|$)",
    re.IGNORECASE,
)
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
ACTION_RE = re.compile(r"[^a-z0-9_]+")
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
CAPTURE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
CAPTURE_BROWSER_ARGS = ("--disable-dev-shm-usage", "--disable-http2")
CAPTURE_RESOURCE_TYPES = {"stylesheet", "font", "image", "media"}
CONTENT_SUFFIXES = {
    "text/css": ".css",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}
SUFFIXES = {value for value in CONTENT_SUFFIXES.values()}

VIDEO_PROFILE = "video_portal_v1"
COMMERCE_PROFILE = "commerce_portal_v1"
TRAVEL_PROFILE = "travel_portal_v1"
CONTENT_PROFILE = "content_portal_v1"
STATIC_LANDING_PROFILE = "static_landing_v1"
GENERIC_PROFILE = "generic_web_app_v1"
VIDEO_ROUTE_KINDS = ["home", "browse", "search", "detail", "favorites", "history", "profile"]
VIDEO_WORKFLOWS = [
    "login", "logout", "reset", "navigate", "search", "playback", "favorite", "comment", "history", "profile",
]
VIDEO_SYNTHETIC_ROUTES = [
    {
        "id": "route_category_video",
        "path": "/category/video",
        "kind": "browse",
        "required_selectors": ['[data-route-kind="browse"]', '[data-video-feed]'],
        "required_action_ids": ["open_video_vid_demo_001"],
    },
    {
        "id": "route_search",
        "path": "/search?q=benchmark",
        "kind": "search",
        "required_selectors": ['[data-route-kind="search"]', '[data-search-query="benchmark"]'],
        "required_action_ids": ["search_result_vid_demo_001"],
    },
    {
        "id": "route_video_vid_demo_001",
        "path": "/video/vid_demo_001",
        "kind": "detail",
        "required_selectors": ['[data-route-kind="detail"]', '[data-video-id="vid_demo_001"]', '[data-comment-list]'],
        "required_action_ids": ["playback_toggle", "favorite_video", "comment_input", "comment_submit", "open_favorites", "open_history"],
    },
    {
        "id": "route_favorites",
        "path": "/favorites",
        "kind": "favorites",
        "required_selectors": ['[data-route-kind="favorites"]', '[data-favorites-list]'],
        "required_action_ids": [],
    },
    {
        "id": "route_history",
        "path": "/history",
        "kind": "history",
        "required_selectors": ['[data-route-kind="history"]', '[data-history-list]'],
        "required_action_ids": [],
    },
    {
        "id": "route_profile_usr_demo_001",
        "path": "/profile/usr_demo_001",
        "kind": "profile",
        "required_selectors": ['[data-route-kind="profile"]', '[data-user-id="usr_demo_001"]'],
        "required_action_ids": [],
    },
]
COMMERCE_ROUTE_KINDS = ["home", "browse", "search", "detail", "cart", "orders", "profile"]
COMMERCE_WORKFLOWS = [
    "login", "logout", "reset", "navigate", "search", "add_to_cart", "checkout", "orders", "profile",
]
COMMERCE_SYNTHETIC_ROUTES = [
    {
        "id": "route_category_featured",
        "path": "/category/featured",
        "kind": "browse",
        "required_selectors": ['[data-route-kind="browse"]', '[data-product-grid]'],
        "required_action_ids": ["open_product_prd_demo_001"],
    },
    {
        "id": "route_search",
        "path": "/search?q=benchmark",
        "kind": "search",
        "required_selectors": ['[data-route-kind="search"]', '[data-search-query="benchmark"]'],
        "required_action_ids": ["search_result_prd_demo_001"],
    },
    {
        "id": "route_product_prd_demo_001",
        "path": "/product/prd_demo_001",
        "kind": "detail",
        "required_selectors": ['[data-route-kind="detail"]', '[data-product-id="prd_demo_001"]'],
        "required_action_ids": ["add_to_cart", "open_cart"],
    },
    {
        "id": "route_cart",
        "path": "/cart",
        "kind": "cart",
        "required_selectors": ['[data-route-kind="cart"]', '[data-cart-list]'],
        "required_action_ids": ["checkout_cart"],
    },
    {
        "id": "route_orders",
        "path": "/orders",
        "kind": "orders",
        "required_selectors": ['[data-route-kind="orders"]', '[data-orders-list]'],
        "required_action_ids": [],
    },
    {
        "id": "route_profile_usr_demo_001",
        "path": "/profile/usr_demo_001",
        "kind": "profile",
        "required_selectors": ['[data-route-kind="profile"]', '[data-user-id="usr_demo_001"]'],
        "required_action_ids": [],
    },
]
TRAVEL_ROUTE_KINDS = ["home", "browse", "search", "detail", "bookings", "profile"]
TRAVEL_WORKFLOWS = [
    "login", "logout", "reset", "navigate", "search", "book", "bookings", "profile",
]
TRAVEL_SYNTHETIC_ROUTES = [
    {
        "id": "route_travel_advisories",
        "path": "/travel/advisories",
        "kind": "browse",
        "required_selectors": ['[data-route-kind="browse"]', '[data-travel-advisories]'],
        "required_action_ids": [],
    },
    {
        "id": "route_flight_search",
        "path": "/flights/search?from=AAA&to=BBB&date=2026-06-20",
        "kind": "search",
        "required_selectors": ['[data-route-kind="search"]', '[data-flight-results]'],
        "required_action_ids": ["open_flight_flt_demo_001"],
    },
    {
        "id": "route_flight_flt_demo_001",
        "path": "/flights/flt_demo_001",
        "kind": "detail",
        "required_selectors": ['[data-route-kind="detail"]', '[data-flight-id="flt_demo_001"]'],
        "required_action_ids": ["book_flight", "open_bookings"],
    },
    {
        "id": "route_bookings",
        "path": "/bookings",
        "kind": "bookings",
        "required_selectors": ['[data-route-kind="bookings"]', '[data-bookings-list]'],
        "required_action_ids": [],
    },
    {
        "id": "route_profile_usr_demo_001",
        "path": "/profile/usr_demo_001",
        "kind": "profile",
        "required_selectors": ['[data-route-kind="profile"]', '[data-user-id="usr_demo_001"]'],
        "required_action_ids": [],
    },
]
PRODUCT_PROFILES = (
    VIDEO_PROFILE,
    COMMERCE_PROFILE,
    TRAVEL_PROFILE,
    CONTENT_PROFILE,
    STATIC_LANDING_PROFILE,
    GENERIC_PROFILE,
)


class ReplayAcquisitionError(RuntimeError):
    """A live URL could not be converted into a safe frozen replay input."""


def _route_kind(path: str) -> str:
    parsed = urlsplit(path)
    value = parsed.path.casefold()
    host = (parsed.hostname or "").casefold()
    if value in {"", "/"}:
        return "home"
    if any(token in value for token in ("/video/", "/watch", "/play/")):
        return "detail"
    if "search" in value:
        return "search"
    if "cart" in value or "basket" in value:
        return "cart"
    if "order" in value:
        return "orders"
    if "booking" in value or "reservation" in value:
        return "bookings"
    if any(token in value for token in ("favorite", "favlist", "collection", "bookmark")):
        return "favorites"
    if "history" in value:
        return "history"
    if (
        any(token in value for token in ("profile", "account", "member", "space/", "space."))
        or host.startswith("space.")
    ):
        return "profile"
    return "browse"


def _infer_profile(seed_url: str, routes: list[dict[str, Any]]) -> str:
    host = (urlsplit(seed_url).hostname or "").casefold()
    haystack = " ".join([
        host,
        *(str(route.get("title") or "") for route in routes),
        *(str(action.get("label") or "") for route in routes for action in route.get("actions") or []),
        *(str(action.get("target_local_path") or "") for route in routes for action in route.get("actions") or []),
    ]).casefold()
    strong_hosts = ("bilibili", "youtube", "vimeo", "dailymotion", "twitch")
    signals = ("/video/", "/watch", "视频", "播放", "番剧", "收藏", "video", "playlist")
    if any(token in host for token in strong_hosts) or sum(token in haystack for token in signals) >= 2:
        return VIDEO_PROFILE
    commerce_hosts = ("shein", "amazon", "shop", "store", "mall")
    commerce_signals = ("product", "shop", "cart", "basket", "checkout", "order", "category", "商品", "购物车")
    if any(token in host for token in commerce_hosts) or sum(token in haystack for token in commerce_signals) >= 2:
        return COMMERCE_PROFILE
    travel_hosts = ("ctrip", "ceair", "csair", "delta", "airbnb", "booking.com")
    travel_signals = ("flight", "check-in", "baggage", "boarding", "booking", "航班", "值机", "行李")
    if any(token in host for token in travel_hosts) or sum(token in haystack for token in travel_signals) >= 2:
        return TRAVEL_PROFILE
    content_hosts = ("accaglobal", "cicpa")
    public_content_suffixes = (".edu", ".edu.cn", ".gov", ".gov.cn", ".gov.sg", ".gc.ca")
    content_signals = (
        "article", "news", "research", "education", "university", "government", "public service",
        "publication", "guidance", "media",
    )
    if (
        any(token in host for token in content_hosts)
        or host.endswith(public_content_suffixes)
        or sum(token in haystack for token in content_signals) >= 2
    ):
        return CONTENT_PROFILE
    if len(routes) == 1:
        return STATIC_LANDING_PROFILE
    return GENERIC_PROFILE


def _coverage(profile: str, routes: list[dict[str, Any]]) -> dict[str, Any]:
    if profile == VIDEO_PROFILE:
        return {
            "profile": VIDEO_PROFILE,
            "release_tier": "product_complete",
            "required_route_kinds": VIDEO_ROUTE_KINDS,
            "required_workflows": VIDEO_WORKFLOWS,
            "generic_fallback_allowed": False,
        }
    if profile == COMMERCE_PROFILE:
        return {
            "profile": COMMERCE_PROFILE,
            "release_tier": "product_complete",
            "required_route_kinds": COMMERCE_ROUTE_KINDS,
            "required_workflows": COMMERCE_WORKFLOWS,
            "generic_fallback_allowed": False,
        }
    if profile == TRAVEL_PROFILE:
        return {
            "profile": TRAVEL_PROFILE,
            "release_tier": "product_complete",
            "required_route_kinds": TRAVEL_ROUTE_KINDS,
            "required_workflows": TRAVEL_WORKFLOWS,
            "generic_fallback_allowed": False,
        }
    if profile == CONTENT_PROFILE:
        observed = sorted({_route_kind(route["local_path"]) for route in routes}) or ["home"]
        return {
            "profile": CONTENT_PROFILE,
            "release_tier": "product_complete",
            "required_route_kinds": observed,
            "required_workflows": ["login", "logout", "reset", "navigate", "stateful_action"],
            "generic_fallback_allowed": False,
        }
    if profile == STATIC_LANDING_PROFILE:
        return {
            "profile": STATIC_LANDING_PROFILE,
            "release_tier": "product_complete",
            "required_route_kinds": ["home"],
            "required_workflows": ["login", "logout", "reset", "stateful_action"],
            "generic_fallback_allowed": False,
        }
    observed = sorted({_route_kind(route["local_path"]) for route in routes}) or ["home"]
    return {
        "profile": GENERIC_PROFILE,
        "release_tier": "smoke",
        "required_route_kinds": observed,
        "required_workflows": ["login", "logout", "reset", "stateful_action"],
        "generic_fallback_allowed": False,
    }


SNAPSHOT_SCRIPT = r"""({routeId, maxActions, privateSelectors}) => {
  const allNodes = root => {
    const values = [...root.querySelectorAll('*')];
    for (let index = 0; index < values.length; index += 1) {
      if (values[index].shadowRoot) values.push(...values[index].shadowRoot.querySelectorAll('*'));
    }
    return values;
  };
  const visible = node => {
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number.parseFloat(style.opacity || '1') > 0.01
      && rect.width > 1 && rect.height > 1
      && rect.bottom > 0 && rect.right > 0
      && rect.top < innerHeight && rect.left < innerWidth;
  };
  const hitTestPoint = node => {
    const rect = node.getBoundingClientRect();
    const left = Math.max(0, rect.left);
    const top = Math.max(0, rect.top);
    const right = Math.min(innerWidth, rect.right);
    const bottom = Math.min(innerHeight, rect.bottom);
    if (right - left <= 1 || bottom - top <= 1) return null;
    const insetX = Math.min(3, Math.max(0.5, (right - left) / 4));
    const insetY = Math.min(3, Math.max(0.5, (bottom - top) / 4));
    const xs = [left + insetX, (left + right) / 2, right - insetX];
    const ys = [top + insetY, (top + bottom) / 2, bottom - insetY];
    const root = node.getRootNode();
    const elementAt = root && typeof root.elementFromPoint === 'function'
      ? (x, y) => root.elementFromPoint(x, y)
      : (x, y) => document.elementFromPoint(x, y);
    for (const x of xs) {
      for (const y of ys) {
        const hit = elementAt(x, y);
        if (hit === node || (hit instanceof Node && node.contains(hit))) {
          return {x: Math.round(x), y: Math.round(y)};
        }
      }
    }
    return null;
  };
  const slug = value => String(value || '').toLowerCase().replace(/[^a-z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '').slice(0, 48);
  let privateFieldsRemoved = 0;
  for (const item of privateSelectors || []) {
    let matches = [];
    try { matches = [...document.querySelectorAll(item.selector)]; } catch (_) { continue; }
    for (const node of matches) {
      if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
        node.value = item.replacement || 'demo@example.test';
        node.setAttribute('value', item.replacement || 'demo@example.test');
      } else {
        node.textContent = item.replacement || 'Demo User';
      }
      privateFieldsRemoved += 1;
    }
  }
  const nodes = allNodes(document);
  let nodeIndex = 0;
  for (const node of nodes) {
    if (!node.getAttribute('data-webanything-node-id')) {
      node.setAttribute('data-webanything-node-id', `${routeId}_node_${String(++nodeIndex).padStart(4, '0')}`);
    }
    if (node instanceof HTMLVideoElement) {
      try { node.pause(); } catch (_) {}
      node.removeAttribute('autoplay');
    }
    if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
      const sensitive = node.type === 'password' || /(?:password|token|secret|email)/i.test(node.autocomplete || '');
      if (sensitive && (node.value || node.getAttribute('value'))) privateFieldsRemoved += 1;
      node.value = '';
      node.removeAttribute('value');
      node.removeAttribute('checked');
    }
  }
  document.documentElement.setAttribute('data-webanything-route', routeId);
  const interactiveSelector = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=tab]',
    '[role=menuitem]', '[contenteditable=true]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');
  const visibleCandidates = allNodes(document).filter(node => {
    try { return node.matches(interactiveSelector) && visible(node); } catch (_) { return false; }
  });
  const candidates = visibleCandidates.map(node => {
    try { return {node, hitPoint: hitTestPoint(node)}; } catch (_) { return {node, hitPoint: null}; }
  }).filter(candidate => candidate.hitPoint !== null);
  const used = new Set();
  const actions = [];
  for (const [index, candidate] of candidates.entries()) {
    if (actions.length >= maxActions) break;
    const {node, hitPoint} = candidate;
    const basis = slug(node.getAttribute('data-action-id') || node.id || node.getAttribute('name')
      || node.getAttribute('aria-label') || node.textContent || node.tagName) || 'control';
    const actionBase = `${routeId}_${basis}`.slice(0, 54).replace(/_+$/g, '');
    let actionId = actionBase;
    let serial = 2;
    while (used.has(actionId)) actionId = `${actionBase}_${serial++}`.slice(0, 63);
    used.add(actionId);
    node.setAttribute('data-action-id', actionId);
    const tag = node.tagName.toLowerCase();
    const type = (node.getAttribute('type') || '').toLowerCase();
    let actionType = 'click';
    let value = null;
    if (tag === 'textarea' || (tag === 'input' && !['button','submit','checkbox','radio','file','image','reset'].includes(type))) {
      actionType = 'fill'; value = 'benchmark';
    } else if (tag === 'select') {
      actionType = 'select'; value = node.options && node.options.length ? node.options[0].value : '';
    } else if (type === 'checkbox' || node.getAttribute('role') === 'checkbox') actionType = 'check';
    const rect = node.getBoundingClientRect();
    actions.push({
      id: actionId,
      selector: `[data-action-id="${actionId}"]`,
      action_type: actionType,
      value,
      tag,
      role: node.getAttribute('role') || null,
      input_type: type || null,
      label: String(node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || node.getAttribute('placeholder') || tag)
        .replace(/\s+/g, ' ').trim().slice(0, 160),
      href: node.href || null,
      rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)},
      hit_point: hitPoint,
    });
  }
  const links = [...document.querySelectorAll('a[href]')].map(node => node.href).filter(Boolean);
  const domTopology = [...document.querySelectorAll('body [data-webanything-node-id]')]
    .filter(node => node.parentElement && node.parentElement.hasAttribute('data-webanything-node-id'))
    .map(node => ({
      node_id: node.getAttribute('data-webanything-node-id'),
      parent_node_id: node.parentElement.getAttribute('data-webanything-node-id'),
    }));
  const styleProperties = [
    'display','position','boxSizing','width','height','minWidth','minHeight','maxWidth','maxHeight',
    'top','right','bottom','left','inset','transform','transformOrigin',
    'marginTop','marginRight','marginBottom','marginLeft','paddingTop','paddingRight','paddingBottom','paddingLeft',
    'fontFamily','fontSize','fontWeight','lineHeight','color','backgroundColor','backgroundImage',
    'borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth','borderRadius',
    'boxShadow','opacity','overflowX','overflowY','flexDirection','justifyContent','alignItems',
    'gridTemplateColumns','gap','zIndex'
  ];
  const computed = [];
  const backgroundAssets = [];
  const seenBackgroundAssets = new Set();
  for (const node of nodes) {
    if (computed.length >= 1400 || !visible(node)) continue;
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    const values = {};
    for (const property of styleProperties) values[property] = style[property];
    const parent = node.parentElement;
    if (parent && ['static', 'relative'].includes(style.position) && style.float === 'none') {
      const parentStyle = getComputedStyle(parent);
      const parentRect = parent.getBoundingClientRect();
      const leftGap = rect.left - parentRect.left;
      const rightGap = parentRect.right - rect.right;
      if (parentStyle.display === 'block' && leftGap > 1 && Math.abs(leftGap - rightGap) <= 1) {
        // getComputedStyle() can resolve auto margins to 0px. Preserve the
        // centering constraint instead of freezing the misleading used value.
        values.marginLeft = 'auto';
        values.marginRight = 'auto';
      }
    }
    for (const match of String(style.backgroundImage || '').matchAll(/url\(\s*['\"]?(https?:\/\/[^)'\"]+)/gi)) {
      const url = match[1];
      if (!seenBackgroundAssets.has(url)) {
        seenBackgroundAssets.add(url);
        backgroundAssets.push(url);
      }
    }
    computed.push({
      node_id: node.getAttribute('data-webanything-node-id'), tag: node.tagName.toLowerCase(),
      rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)},
      style: values,
    });
  }
  const cleanMarkup = markup => {
    const template = document.createElement('template');
    template.innerHTML = markup;
    for (const node of template.content.querySelectorAll('*')) {
      if (['SCRIPT','NOSCRIPT'].includes(node.tagName)) { node.remove(); continue; }
      for (const attribute of [...node.attributes]) {
        const name = attribute.name.toLowerCase();
        const value = attribute.value || '';
        if (name.startsWith('on') || name === 'nonce' || name === 'integrity'
            || /^(?:javascript|vbscript):/i.test(value)
            || /(?:^|[_-])(?:auth|cookie|credential|password|secret|session|token)(?:[_-]|$)/i.test(name)) {
          node.removeAttribute(attribute.name); privateFieldsRemoved += 1;
        }
      }
      if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
        node.removeAttribute('value'); node.removeAttribute('checked');
      }
    }
    return template.innerHTML;
  };
  const clone = document.documentElement.cloneNode(true);
  for (const node of clone.querySelectorAll('script,noscript')) node.remove();
  for (const node of clone.querySelectorAll('*')) {
    for (const attribute of [...node.attributes]) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value || '';
      if (name.startsWith('on') || name === 'nonce' || name === 'integrity'
          || /^(?:javascript|vbscript):/i.test(value)
          || /(?:^|[_-])(?:auth|cookie|credential|password|secret|session|token)(?:[_-]|$)/i.test(name)) {
        node.removeAttribute(attribute.name); privateFieldsRemoved += 1;
      }
    }
    if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
      node.removeAttribute('value'); node.removeAttribute('checked');
    }
  }
  const shadowRoots = [];
  const findCloneNode = nodeId => {
    const selector = `[data-webanything-node-id="${CSS.escape(nodeId)}"]`;
    const roots = [clone];
    while (roots.length) {
      const root = roots.shift();
      const found = root.querySelector(selector);
      if (found) return found;
      for (const template of root.querySelectorAll('template[shadowrootmode="open"]')) {
        roots.push(template.content);
      }
    }
    return null;
  };
  for (const node of nodes) {
    if (!node.shadowRoot) continue;
    const hostNodeId = node.getAttribute('data-webanything-node-id');
    const shadowHtml = cleanMarkup(node.shadowRoot.innerHTML);
    shadowRoots.push({host_node_id: hostNodeId, html: shadowHtml});
    const cloneHost = findCloneNode(hostNodeId);
    if (cloneHost) {
      const template = document.createElement('template');
      template.setAttribute('shadowrootmode', 'open');
      template.setAttribute('data-webanything-shadow-root', 'v1');
      template.innerHTML = shadowHtml;
      cloneHost.prepend(template);
    }
  }
  const mediaSurfaces = nodes.filter(node => node instanceof HTMLVideoElement && visible(node)).map(node => ({
    node_id: node.getAttribute('data-webanything-node-id'),
    tag: 'video',
  }));
  return {
    html: '<!doctype html>\n' + clone.outerHTML,
    title: document.title || '',
    links,
    actions,
    action_count: candidates.length,
    visible_action_count: visibleCandidates.length,
    occluded_action_count: visibleCandidates.length - candidates.length,
    actions_truncated: candidates.length > maxActions,
    shadow_roots: shadowRoots,
    media_surfaces: mediaSurfaces,
    dom_topology: domTopology,
    computed_styles: computed,
    computed_styles_truncated: computed.length >= 1400,
    background_assets: backgroundAssets,
    private_fields_removed: privateFieldsRemoved,
  };
}"""


def _canonical_url(
    raw: str,
    *,
    expected_origin: str | None = None,
    expected_origins: set[str] | None = None,
) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ReplayAcquisitionError("capture URLs must be credential-free absolute HTTP(S) URLs")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if expected_origin is not None and expected_origins is not None:
        raise ReplayAcquisitionError("capture URL validation must use one origin policy")
    allowed_origins = {expected_origin} if expected_origin is not None else expected_origins
    if allowed_origins is not None and origin not in allowed_origins:
        raise ReplayAcquisitionError("capture route is outside the seed origin")
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if SENSITIVE_QUERY_RE.search(key):
            raise ReplayAcquisitionError(f"capture route contains a sensitive query parameter: {key}")
        pairs.append((key, value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(sorted(pairs)), ""))


def _local_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    return path + (f"?{parsed.query}" if parsed.query else "")


def _replay_local_path(url: str, primary_origin: str) -> str:
    local_path = _local_path(url)
    if _origin(url) == primary_origin:
        return local_path
    host = (urlsplit(url).hostname or "source").casefold()
    return f"/__webanything/source/{host}{local_path}"


def _safe_read_route(url: str) -> bool:
    return not UNSAFE_ROUTE_RE.search(urlsplit(url).path)


def _seed_navigation_candidates(url: str) -> list[str]:
    """Return conservative public-home variants for a user-supplied seed URL."""
    parsed = urlsplit(url)
    candidates = [url]
    host = parsed.hostname or ""
    hosts = [host]
    if host and "." in host and not host.casefold().startswith("www."):
        hosts.append(f"www.{host}")
    paths = [parsed.path or "/"]
    if (parsed.path or "/") == "/":
        paths.append("/home")
    for candidate_host in hosts:
        netloc = candidate_host
        if parsed.port:
            netloc = f"{candidate_host}:{parsed.port}"
        for path in paths:
            candidate = urlunsplit(
                (parsed.scheme, netloc, path, parsed.query if path == paths[0] else "", "")
            )
            candidates.append(_canonical_url(candidate))
    return list(dict.fromkeys(candidates))


def _interactive_challenge_marker(text: str) -> str | None:
    normalized = " ".join(str(text or "").casefold().split())
    phrases = {
        "confirm you are human": "human_verification",
        "verify you are human": "human_verification",
        "let's confirm you are human": "human_verification",
        "complete the security check": "security_check",
        "press and hold to confirm": "security_check",
    }
    return next((marker for phrase, marker in phrases.items() if phrase in normalized), None)


def _site_domain(hostname: str) -> str:
    labels = hostname.casefold().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    compound_suffixes = {"co.uk", "com.cn", "edu.cn", "gc.ca", "gov.cn", "gov.sg"}
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in compound_suffixes else suffix


def _same_site_url(left: str, right: str) -> bool:
    left_host = urlsplit(left).hostname or ""
    right_host = urlsplit(right).hostname or ""
    return bool(left_host and right_host and _site_domain(left_host) == _site_domain(right_host))


def _visible_interactive_challenge(page: Any) -> str | None:
    marker = _interactive_challenge_marker(page.locator("body").inner_text(timeout=5_000))
    if marker:
        return marker
    selectors = (
        'iframe[src*="captcha" i]',
        '[id*="captcha" i]',
        '[class*="captcha" i]',
        '[data-testid*="captcha" i]',
    )
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() and locator.first.is_visible():
            return "captcha_surface"
    return None


def _is_optional_discovered_route(route_url: str, required_routes: set[str]) -> bool:
    return route_url not in required_routes


def _page_has_capture_surface(page: Any) -> bool:
    try:
        return page.evaluate(
            """() => Boolean(
              document.body
              && document.body.getBoundingClientRect().width > 0
              && document.body.getBoundingClientRect().height > 0
              && document.body.querySelectorAll('*').length >= 5
            )"""
        ) is True
    except Exception:
        return False


def _route_id(url: str, used: set[str]) -> str:
    parsed = urlsplit(url)
    stem = ACTION_RE.sub("_", parsed.path.strip("/").casefold()) or "home"
    if parsed.query:
        stem = f"{stem}_{hashlib.sha256(parsed.query.encode()).hexdigest()[:8]}"
    stem = stem[:48].strip("_") or "home"
    candidate = f"route_{stem}"
    serial = 2
    while candidate in used:
        candidate = f"route_{stem}_{serial}"
        serial += 1
    used.add(candidate)
    return candidate


def _suffix(content_type: str, url: str) -> str | None:
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type in CONTENT_SUFFIXES:
        return CONTENT_SUFFIXES[media_type]
    candidate = Path(urlsplit(url).path).suffix.casefold()
    return candidate if candidate in SUFFIXES else None


def _safe_private_selectors(values: list[str]) -> list[dict[str, str]]:
    defaults = [
        "input[type=password]",
        "input[autocomplete=current-password]",
        "input[autocomplete=email]",
        "[data-webanything-private]",
    ]
    selectors: list[dict[str, str]] = []
    for value in [*defaults, *values]:
        if not isinstance(value, str) or not value.strip() or len(value) > 300:
            raise ReplayAcquisitionError("private selectors must be nonempty CSS selectors under 300 characters")
        if value not in {item["selector"] for item in selectors}:
            selectors.append({"selector": value, "replacement": "Demo User"})
    return selectors


def _scrub_visible_text(value: Any) -> str:
    text = str(value or "")
    text = EMAIL_RE.sub("demo@example.test", text)
    return re.sub(
        r"(?i)(?:access[_-]?token|auth[_-]?token|session[_-]?id|password)\s*[:=]\s*[^\s,;]+",
        "private_field=[removed]",
        text,
    )[:160]


def _sanitize_actions(
    actions: list[dict[str, Any]],
    source_origin: str,
    allowed_route_origins: set[str] | None = None,
) -> list[dict[str, Any]]:
    allowed = allowed_route_origins or {source_origin}
    sanitized: list[dict[str, Any]] = []
    for source in actions:
        action = dict(source)
        action["label"] = _scrub_visible_text(action.get("label"))
        href = action.pop("href", None)
        if isinstance(href, str) and href:
            try:
                absolute = _canonical_url(href)
                if _origin(absolute) in allowed:
                    action["target_local_path"] = _replay_local_path(absolute, source_origin)
                else:
                    action["target_origin_sha256"] = hashlib.sha256(_origin(absolute).encode()).hexdigest()
            except (ReplayAcquisitionError, SyntheticVisualError):
                pass
        sanitized.append(action)
    return sanitized


def _rewrite_css(text: str, source_url: str, asset_paths: dict[str, str], css_path: Path) -> str:
    import posixpath

    def replacement(match: re.Match[str]) -> str:
        quote = match.group(1) or ""
        raw = match.group(2).strip()
        if raw.startswith(("data:", "#")):
            return match.group(0)
        if raw.startswith("blob:"):
            return 'url("")'
        absolute = urljoin(source_url, raw)
        target = asset_paths.get(absolute)
        if not target:
            return "url(\"\")"
        relative = posixpath.relpath(target.removeprefix("capture/"), css_path.parent.as_posix().removeprefix("capture/") or ".")
        return f"url({quote}{relative}{quote})"

    text = re.sub(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", replacement, text, flags=re.IGNORECASE)
    text = re.sub(
        r"@import\s+(?:url\()?\s*['\"]?https?://[^;]+;",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _rewrite_markup(
    markup: str,
    source_origin: str,
    asset_paths: dict[str, str],
    *,
    primary_origin: str | None = None,
    allowed_route_origins: set[str] | None = None,
) -> str:
    primary = primary_origin or source_origin
    allowed = allowed_route_origins or {primary}

    def attribute(match: re.Match[str]) -> str:
        name, quote, raw = match.group(1), match.group(2), match.group(3)
        if raw.startswith(("data:", "#")):
            return match.group(0)
        if raw.startswith("blob:"):
            return f"{name}={quote}{'#' if name.casefold() == 'href' else ''}{quote}"
        if raw.startswith(("mailto:", "tel:")):
            return match.group(0)
        absolute = urljoin(source_origin + "/", raw)
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"}:
            return match.group(0)
        target = asset_paths.get(absolute)
        if target:
            return f"{name}={quote}{target.removeprefix('capture/')}{quote}"
        if name.casefold() == "href" and _origin(absolute) in allowed:
            canonical = _canonical_url(absolute, expected_origins=allowed)
            return f"{name}={quote}{_replay_local_path(canonical, primary)}{quote}"
        return f"{name}={quote}{'#' if name.casefold() == 'href' else ''}{quote}"

    markup = re.sub(
        r"\b(src|href|poster)\s*=\s*(['\"])([^'\"]+)\2",
        attribute,
        markup,
        flags=re.IGNORECASE,
    )

    def srcset(match: re.Match[str]) -> str:
        quote, raw = match.group(1), match.group(2)
        if raw.strip().startswith("data:"):
            return match.group(0)
        rewritten = []
        for item in raw.split(","):
            parts = item.strip().split()
            if not parts:
                continue
            absolute = urljoin(source_origin + "/", parts[0])
            target = asset_paths.get(absolute)
            if target:
                rewritten.append(" ".join([target.removeprefix("capture/"), *parts[1:]]))
        return f"srcset={quote}{', '.join(rewritten)}{quote}"

    markup = re.sub(
        r"\bsrcset\s*=\s*(['\"])([^'\"]+)\1",
        srcset,
        markup,
        flags=re.IGNORECASE,
    )

    def style_block(match: re.Match[str]) -> str:
        opening, raw, closing = match.group(1), match.group(2), match.group(3)
        rewritten = _rewrite_css(
            raw,
            source_origin + "/",
            asset_paths,
            Path("capture/pages/inline.css"),
        )
        return f"{opening}{rewritten}{closing}"

    markup = re.sub(
        r"(<style\b[^>]*>)(.*?)(</style\s*>)",
        style_block,
        markup,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def inline_style(match: re.Match[str]) -> str:
        quote, raw = match.group(1), match.group(2)
        rewritten = _rewrite_css(raw, source_origin + "/", asset_paths, Path("capture/pages/inline.css"))
        return f"style={quote}{rewritten}{quote}"

    return re.sub(
        r"\bstyle\s*=\s*(['\"])([^'\"]+)\1",
        inline_style,
        markup,
        flags=re.IGNORECASE,
    )


def _strip_html_comments(markup: str) -> str:
    """Remove inert source comments before executable-markup validation."""
    return re.sub(r"<!--.*?-->", "", markup, flags=re.DOTALL)


def _accepted_replay_quality_failure_codes(
    geometry: dict[str, Any],
    quality: dict[str, Any],
) -> list[str]:
    """Accept only known-safe replay-specific geometry edge cases."""
    metrics = quality.get("metrics") or {}
    viewport = geometry.get("viewport") or {}
    viewport_area = max(int(viewport.get("width", 0)) * int(viewport.get("height", 0)), 1)
    elements = geometry.get("elements") or []
    dominant_media = any(
        item.get("category") == "media"
        and int(item.get("width", 0)) * int(item.get("height", 0)) / viewport_area >= 0.88
        for item in elements
        if isinstance(item, dict)
    )
    non_media_regions = sum(
        isinstance(item, dict) and item.get("category") != "media" for item in elements
    )
    healthy_full_bleed = (
        geometry.get("truncated") is False
        and dominant_media
        and non_media_regions >= 3
        and int(metrics.get("landmark_count", 0)) >= 1
        and float(metrics.get("max_overlap_ratio", 5.0)) <= 4.0
    )
    healthy_structured_saturation = (
        geometry.get("truncated") is False
        and len(elements) >= 12
        and int(metrics.get("landmark_count", 0)) >= 2
        and float(metrics.get("max_rectangle_coverage", 1.0)) < 0.88
        and float(metrics.get("max_overlap_ratio", 5.0)) <= 4.0
    )
    accepted: list[str] = []
    for failure in quality.get("failures") or []:
        code = failure.get("code")
        if code == "landmarks_missing":
            accepted.append(code)
        elif code == "column_saturation" and (
            healthy_full_bleed or healthy_structured_saturation
        ):
            accepted.append(code)
        elif code == "page_covering_rectangle" and healthy_full_bleed:
            # A full-bleed source media hero can legitimately cover the entire
            # viewport. Require independent structure and landmark evidence.
            accepted.append(code)
    return sorted(set(accepted))


def _inject_computed_style_freeze(markup: str, metadata: dict[str, Any]) -> str:
    rules: list[str] = []
    for record in metadata.get("computed_styles") or []:
        if not isinstance(record, dict):
            continue
        node_id = record.get("node_id")
        style = record.get("style")
        if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", node_id):
            continue
        if not isinstance(style, dict):
            continue
        declarations: list[str] = []
        for name, value in style.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            if value == "captured-local-asset" or any(token in value.casefold() for token in ("</style", "javascript:")):
                continue
            property_name = re.sub(r"(?<!^)([A-Z])", r"-\1", name).casefold()
            if not re.fullmatch(r"[a-z-]+", property_name):
                continue
            declarations.append(f"{property_name}:{value}!important")
        if declarations:
            rules.append(f'[data-webanything-node-id="{node_id}"]' + "{" + ";".join(declarations) + "}")
    if not rules:
        return markup
    frozen = '<style data-webanything-computed-style-freeze="v1">' + "".join(rules) + "</style>"
    if re.search(r"</head\s*>", markup, flags=re.IGNORECASE):
        return re.sub(r"</head\s*>", frozen + "</head>", markup, count=1, flags=re.IGNORECASE)
    return frozen + markup


def _localize_computed_style_backgrounds(
    metadata: dict[str, Any],
    source_url: str,
    asset_paths: dict[str, str],
) -> None:
    """Bind computed CSS backgrounds to frozen local assets."""
    for computed in metadata.get("computed_styles") or []:
        style = computed.get("style") if isinstance(computed, dict) else None
        if not isinstance(style, dict):
            continue
        background = style.get("backgroundImage")
        if not isinstance(background, str) or "http" not in background:
            continue
        style["backgroundImage"] = _rewrite_css(
            background,
            source_url,
            asset_paths,
            Path("capture/pages/inline.css"),
        )


def _inject_captured_media_posters(markup: str, metadata: dict[str, Any]) -> str:
    """Bind frozen video frames without turning a whole page into a screenshot."""
    for item in metadata.get("media_posters") or []:
        if not isinstance(item, dict):
            continue
        node_id = item.get("node_id")
        path = item.get("path")
        if (
            not isinstance(node_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", node_id)
            or not isinstance(path, str)
            or not re.fullmatch(r"assets/[a-f0-9]{24}\.png", path)
        ):
            continue
        pattern = re.compile(
            rf'<video\b(?=[^>]*\bdata-webanything-node-id=(["\']){re.escape(node_id)}\1)[^>]*>',
            flags=re.IGNORECASE,
        )

        def replace(match: re.Match[str]) -> str:
            tag = match.group(0)
            tag = re.sub(r"\s+autoplay(?:\s*=\s*(?:['\"][^'\"]*['\"]|[^\s>]+))?", "", tag, flags=re.IGNORECASE)
            tag = re.sub(r"\s+poster\s*=\s*(['\"])[^'\"]*\1", "", tag, flags=re.IGNORECASE)
            tag = re.sub(r"\s+preload\s*=\s*(['\"])[^'\"]*\1", "", tag, flags=re.IGNORECASE)
            return tag[:-1] + f' poster="{path}" preload="metadata">'

        markup = pattern.sub(replace, markup, count=1)
    return markup


def _inject_dom_topology_restore(markup: str, metadata: dict[str, Any]) -> str:
    topology = [
        {"node_id": item.get("node_id"), "parent_node_id": item.get("parent_node_id")}
        for item in metadata.get("dom_topology") or []
        if isinstance(item, dict)
        and isinstance(item.get("node_id"), str)
        and isinstance(item.get("parent_node_id"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]+", item["node_id"])
        and re.fullmatch(r"[A-Za-z0-9_-]+", item["parent_node_id"])
    ]
    if not topology:
        return markup
    script = trusted_dom_topology_script(topology)
    if re.search(r"</body\s*>", markup, flags=re.IGNORECASE):
        return re.sub(r"</body\s*>", script + "</body>", markup, count=1, flags=re.IGNORECASE)
    return markup + script


def _write_seed(path: Path, routes: list[dict[str, Any]], *, profile: str = GENERIC_PROFILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA foreign_keys=ON;
            CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL);
            CREATE TABLE benchmark_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE captured_actions(
                id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                label TEXT NOT NULL,
                action_type TEXT NOT NULL,
                PRIMARY KEY(route_id, id)
            );
            CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL, value TEXT NOT NULL);
            """
        )
        connection.execute(
            "INSERT INTO users VALUES(?,?,?,?)",
            ("usr_demo_001", "demo@example.test", "Demo User", "member"),
        )
        connection.executemany(
            "INSERT INTO benchmark_state VALUES(?,?)",
            [("authenticated", "false"), ("current_user_id", "usr_demo_001"), ("benchmark_toggle", "false")],
        )
        rows = []
        for route in routes:
            for action in route["actions"]:
                rows.append((action["id"], route["id"], action["label"] or action["tag"], action["action_type"]))
        connection.executemany("INSERT INTO captured_actions VALUES(?,?,?,?)", sorted(rows))
        if profile == VIDEO_PROFILE:
            connection.executescript(
                """
                CREATE TABLE videos(id TEXT PRIMARY KEY, title TEXT NOT NULL, creator TEXT NOT NULL, duration_seconds INTEGER NOT NULL);
                CREATE TABLE comments(id TEXT PRIMARY KEY, video_id TEXT NOT NULL, user_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE favorites(user_id TEXT NOT NULL, video_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(user_id, video_id));
                CREATE TABLE history(user_id TEXT NOT NULL, video_id TEXT NOT NULL, viewed_at TEXT NOT NULL, PRIMARY KEY(user_id, video_id));
                CREATE TABLE playback_state(user_id TEXT NOT NULL, video_id TEXT NOT NULL, playing INTEGER NOT NULL, position_seconds INTEGER NOT NULL, PRIMARY KEY(user_id, video_id));
                INSERT INTO videos VALUES('vid_demo_001', 'Benchmark Journey', 'Demo Creator', 372);
                INSERT INTO comments VALUES('cmt_seed_001', 'vid_demo_001', 'usr_demo_001', 'A deterministic seed comment.', '2026-06-15T09:00:00Z');
                INSERT INTO playback_state VALUES('usr_demo_001', 'vid_demo_001', 0, 0);
                """
            )
        elif profile == COMMERCE_PROFILE:
            connection.executescript(
                """
                CREATE TABLE products(id TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL, category TEXT NOT NULL, inventory INTEGER NOT NULL);
                CREATE TABLE cart_items(user_id TEXT NOT NULL, product_id TEXT NOT NULL, quantity INTEGER NOT NULL, added_at TEXT NOT NULL, PRIMARY KEY(user_id, product_id));
                CREATE TABLE orders(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, total_cents INTEGER NOT NULL, created_at TEXT NOT NULL);
                INSERT INTO products VALUES('prd_demo_001', 'Benchmark Carry Kit', 12900, 'featured', 25);
                INSERT INTO orders VALUES('ord_seed_001', 'usr_demo_001', 'fulfilled', 8900, '2026-06-10T09:00:00Z');
                """
            )
        elif profile == TRAVEL_PROFILE:
            connection.executescript(
                """
                CREATE TABLE flights(id TEXT PRIMARY KEY, origin TEXT NOT NULL, destination TEXT NOT NULL, departure_at TEXT NOT NULL, arrival_at TEXT NOT NULL, fare_cents INTEGER NOT NULL);
                CREATE TABLE flight_searches(id TEXT PRIMARY KEY, origin TEXT NOT NULL, destination TEXT NOT NULL, departure_date TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE bookings(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, flight_id TEXT NOT NULL, status TEXT NOT NULL, booked_at TEXT NOT NULL);
                INSERT INTO flights VALUES('flt_demo_001', 'AAA', 'BBB', '2026-06-20T08:00:00Z', '2026-06-20T10:30:00Z', 42800);
                """
            )
        elif profile == CONTENT_PROFILE:
            connection.executescript(
                """
                CREATE TABLE content_items(id TEXT PRIMARY KEY, title TEXT NOT NULL, section TEXT NOT NULL, published_at TEXT NOT NULL);
                CREATE TABLE bookmarks(user_id TEXT NOT NULL, content_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(user_id, content_id));
                INSERT INTO content_items VALUES('cnt_demo_001', 'Benchmark Guidance', 'featured', '2026-06-12T09:00:00Z');
                """
            )
        connection.commit()
        connection.execute("VACUUM")


def _base_tasks() -> list[dict[str, Any]]:
    return [
        {
            "task_id": "login_task",
            "workflow": "login",
            "initial_state": "anonymous",
            "start_url": "/__webanything/login",
            "instruction": "Sign in with the local demo account.",
            "actions": [{"type": "click", "selector": "[data-action-id=login_submit]"}],
            "evaluator_id": "eval_login",
            "success": [
                {"surface": "dom", "selector": '[data-webanything-auth-state="authenticated"]', "visible": True},
                {"surface": "sqlite", "table": "benchmark_state", "where": {"key": "authenticated", "value": "true"}, "count_equals": 1},
            ],
        },
        {
            "task_id": "logout_task",
            "workflow": "logout",
            "initial_state": "authenticated",
            "start_url": "/__webanything/logout",
            "instruction": "Sign out of the local demo account.",
            "actions": [{"type": "click", "selector": "[data-action-id=logout_submit]"}],
            "evaluator_id": "eval_logout",
            "success": [
                {"surface": "dom", "selector": "[data-webanything-auth-entry]", "visible": True},
                {"surface": "sqlite", "table": "benchmark_state", "where": {"key": "authenticated", "value": "false"}, "count_equals": 1},
            ],
        },
        {
            "task_id": "reset_task",
            "workflow": "reset",
            "initial_state": "anonymous",
            "start_url": "/__webanything/reset.html",
            "instruction": "Restore the benchmark to its byte-identical initial state.",
            "actions": [],
            "evaluator_id": "eval_reset",
            "success": [
                {"surface": "dom", "selector": "[data-page=reset]", "visible": True},
                {"surface": "sqlite", "table": "benchmark_state", "where": {"key": "authenticated", "value": "false"}, "count_equals": 1},
            ],
        },
    ]


def _video_tasks(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks = _base_tasks()
    tasks.extend([
        {
            "task_id": "navigate_video_task",
            "workflow": "navigate",
            "start_url": "/",
            "instruction": "Open the deterministic demo video from the home feed.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_video_vid_demo_001"]'}],
            "evaluator_id": "eval_navigate_video",
            "success": [
                {"surface": "url", "path_equals": "/video/vid_demo_001"},
                {"surface": "dom", "selector": '[data-video-id="vid_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "browse_video_task",
            "workflow": "navigate",
            "start_url": "/category/video",
            "instruction": "Open the deterministic demo video from the video category.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_video_vid_demo_001"]'}],
            "evaluator_id": "eval_browse_video",
            "success": [
                {"surface": "url", "path_equals": "/video/vid_demo_001"},
                {"surface": "dom", "selector": '[data-video-id="vid_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "search_video_task",
            "workflow": "search",
            "start_url": "/",
            "instruction": "Search for benchmark and show the deterministic demo video.",
            "actions": [
                {"type": "fill", "selector": '[data-action-id="search_input"]', "value": "benchmark"},
                {"type": "click", "selector": '[data-action-id="search_submit"]'},
            ],
            "evaluator_id": "eval_search_video",
            "success": [
                {"surface": "url", "path_equals": "/search", "query_equals": "q=benchmark"},
                {"surface": "dom", "selector": '[data-search-query="benchmark"]', "visible": True},
                {"surface": "dom", "selector": '[data-video-id="vid_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "search_result_task",
            "workflow": "navigate",
            "start_url": "/search?q=benchmark",
            "instruction": "Open the deterministic video from search results.",
            "actions": [{"type": "click", "selector": '[data-action-id="search_result_vid_demo_001"]'}],
            "evaluator_id": "eval_search_result",
            "success": [
                {"surface": "url", "path_equals": "/video/vid_demo_001"},
                {"surface": "dom", "selector": '[data-video-id="vid_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "playback_task",
            "workflow": "playback",
            "start_url": "/video/vid_demo_001",
            "instruction": "Start local playback for the deterministic demo video.",
            "actions": [{"type": "click", "selector": '[data-action-id="playback_toggle"]'}],
            "evaluator_id": "eval_playback",
            "success": [
                {"surface": "dom", "selector": '[data-video-id="vid_demo_001"]', "attribute": "data-playing", "equals": "true"},
                {"surface": "sqlite", "table": "playback_state", "where": {"user_id": "usr_demo_001", "video_id": "vid_demo_001", "playing": 1}, "count_equals": 1},
            ],
        },
        {
            "task_id": "favorite_video_task",
            "workflow": "favorite",
            "start_url": "/video/vid_demo_001",
            "instruction": "Favorite the demo video and confirm it appears in Favorites.",
            "actions": [
                {"type": "click", "selector": '[data-action-id="favorite_video"]'},
                {"type": "click", "selector": '[data-action-id="open_favorites"]'},
            ],
            "evaluator_id": "eval_favorite_video",
            "success": [
                {"surface": "url", "path_equals": "/favorites"},
                {"surface": "dom", "selector": '[data-favorite-video-id="vid_demo_001"]', "visible": True},
                {"surface": "sqlite", "table": "favorites", "where": {"user_id": "usr_demo_001", "video_id": "vid_demo_001"}, "count_equals": 1},
            ],
        },
        {
            "task_id": "comment_video_task",
            "workflow": "comment",
            "start_url": "/video/vid_demo_001",
            "instruction": "Post a deterministic comment on the demo video.",
            "actions": [
                {"type": "fill", "selector": '[data-action-id="comment_input"]', "value": "Great benchmark video"},
                {"type": "click", "selector": '[data-action-id="comment_submit"]'},
            ],
            "evaluator_id": "eval_comment_video",
            "success": [
                {"surface": "dom", "selector": '[data-comment-id="cmt_task_001"]', "text_equals": "Great benchmark video"},
                {"surface": "sqlite", "table": "comments", "where": {"id": "cmt_task_001", "video_id": "vid_demo_001", "body": "Great benchmark video"}, "count_equals": 1},
            ],
        },
        {
            "task_id": "history_task",
            "workflow": "history",
            "start_url": "/",
            "instruction": "Open the demo video and confirm it appears in viewing history.",
            "actions": [
                {"type": "click", "selector": '[data-action-id="open_video_vid_demo_001"]'},
                {"type": "click", "selector": '[data-action-id="open_history"]'},
            ],
            "evaluator_id": "eval_history",
            "success": [
                {"surface": "url", "path_equals": "/history"},
                {"surface": "dom", "selector": '[data-history-video-id="vid_demo_001"]', "visible": True},
                {"surface": "sqlite", "table": "history", "where": {"user_id": "usr_demo_001", "video_id": "vid_demo_001"}, "count_equals": 1},
            ],
        },
        {
            "task_id": "profile_task",
            "workflow": "profile",
            "start_url": "/",
            "instruction": "Open the local synthetic profile.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_profile"]'}],
            "evaluator_id": "eval_profile",
            "success": [
                {"surface": "url", "path_equals": "/profile/usr_demo_001"},
                {"surface": "dom", "selector": '[data-user-id="usr_demo_001"]', "visible": True},
            ],
        },
    ])
    route_by_path = {route["local_path"]: route for route in routes}
    navigation_number = 0
    for route in routes:
        for action in route.get("actions") or []:
            target = action.get("target_local_path")
            if target not in route_by_path or target == route["local_path"]:
                continue
            navigation_number += 1
            target_route = route_by_path[target]
            tasks.append({
                "task_id": f"captured_navigation_{navigation_number:04d}",
                "workflow": "navigate",
                "start_url": route["local_path"],
                "instruction": f"Navigate using the captured control: {action['label'] or action['tag']}.",
                "actions": [{"type": "click", "selector": action["selector"]}],
                "evaluator_id": f"eval_captured_navigation_{navigation_number:04d}",
                "success": [
                    {"surface": "url", "path_equals": urlsplit(target).path, "query_equals": urlsplit(target).query},
                    {"surface": "dom", "selector": f'[data-webanything-route="{target_route["id"]}"]', "visible": True},
                ],
            })
    for task in tasks:
        task.setdefault(
            "initial_state",
            "authenticated"
            if task["workflow"] in {"favorite", "comment", "history", "profile"}
            else "anonymous",
        )
    return tasks


def _commerce_tasks() -> list[dict[str, Any]]:
    tasks = _base_tasks()
    tasks.extend([
        {
            "task_id": "navigate_product_task",
            "workflow": "navigate",
            "initial_state": "anonymous",
            "start_url": "/",
            "instruction": "Open the deterministic demo product from the home surface.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_product_prd_demo_001"]'}],
            "evaluator_id": "eval_navigate_product",
            "success": [
                {"surface": "url", "path_equals": "/product/prd_demo_001"},
                {"surface": "dom", "selector": '[data-product-id="prd_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "browse_product_task",
            "workflow": "navigate",
            "initial_state": "anonymous",
            "start_url": "/category/featured",
            "instruction": "Open the deterministic product from the featured category.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_product_prd_demo_001"]'}],
            "evaluator_id": "eval_browse_product",
            "success": [
                {"surface": "url", "path_equals": "/product/prd_demo_001"},
                {"surface": "dom", "selector": '[data-product-id="prd_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "search_product_task",
            "workflow": "search",
            "initial_state": "anonymous",
            "start_url": "/",
            "instruction": "Search for benchmark and show the deterministic demo product.",
            "actions": [
                {"type": "fill", "selector": '[data-action-id="search_input"]', "value": "benchmark"},
                {"type": "click", "selector": '[data-action-id="search_submit"]'},
            ],
            "evaluator_id": "eval_search_product",
            "success": [
                {"surface": "url", "path_equals": "/search", "query_equals": "q=benchmark"},
                {"surface": "dom", "selector": '[data-search-query="benchmark"]', "visible": True},
                {"surface": "dom", "selector": '[data-product-id="prd_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "search_result_task",
            "workflow": "navigate",
            "initial_state": "anonymous",
            "start_url": "/search?q=benchmark",
            "instruction": "Open the deterministic product from search results.",
            "actions": [{"type": "click", "selector": '[data-action-id="search_result_prd_demo_001"]'}],
            "evaluator_id": "eval_search_result",
            "success": [
                {"surface": "url", "path_equals": "/product/prd_demo_001"},
                {"surface": "dom", "selector": '[data-product-id="prd_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "add_to_cart_task",
            "workflow": "add_to_cart",
            "initial_state": "authenticated",
            "start_url": "/product/prd_demo_001",
            "instruction": "Add the deterministic demo product to the local cart.",
            "actions": [{"type": "click", "selector": '[data-action-id="add_to_cart"]'}],
            "evaluator_id": "eval_add_to_cart",
            "success": [
                {"surface": "dom", "selector": '[data-cart-product-id="prd_demo_001"]', "visible": True},
                {"surface": "sqlite", "table": "cart_items", "where": {"user_id": "usr_demo_001", "product_id": "prd_demo_001", "quantity": 1}, "count_equals": 1},
            ],
        },
        {
            "task_id": "checkout_task",
            "workflow": "checkout",
            "initial_state": "authenticated",
            "start_url": "/product/prd_demo_001",
            "instruction": "Add the demo product, open the cart, and complete the local checkout.",
            "actions": [
                {"type": "click", "selector": '[data-action-id="add_to_cart"]'},
                {"type": "click", "selector": '[data-action-id="open_cart"]'},
                {"type": "click", "selector": '[data-action-id="checkout_cart"]'},
            ],
            "evaluator_id": "eval_checkout",
            "success": [
                {"surface": "url", "path_equals": "/orders"},
                {"surface": "dom", "selector": '[data-order-id="ord_task_001"]', "visible": True},
                {"surface": "sqlite", "table": "orders", "where": {"id": "ord_task_001", "user_id": "usr_demo_001", "status": "placed", "total_cents": 12900}, "count_equals": 1},
            ],
        },
        {
            "task_id": "orders_task",
            "workflow": "orders",
            "initial_state": "authenticated",
            "start_url": "/",
            "instruction": "Open the local order history.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_orders"]'}],
            "evaluator_id": "eval_orders",
            "success": [
                {"surface": "url", "path_equals": "/orders"},
                {"surface": "dom", "selector": '[data-order-id="ord_seed_001"]', "visible": True},
            ],
        },
        {
            "task_id": "profile_task",
            "workflow": "profile",
            "initial_state": "authenticated",
            "start_url": "/",
            "instruction": "Open the local synthetic customer profile.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_profile"]'}],
            "evaluator_id": "eval_profile",
            "success": [
                {"surface": "url", "path_equals": "/profile/usr_demo_001"},
                {"surface": "dom", "selector": '[data-user-id="usr_demo_001"]', "visible": True},
            ],
        },
    ])
    return tasks


def _travel_tasks() -> list[dict[str, Any]]:
    tasks = _base_tasks()
    tasks.extend([
        {
            "task_id": "search_flights_task",
            "workflow": "search",
            "initial_state": "anonymous",
            "start_url": "/",
            "instruction": "Search the local timetable from AAA to BBB on 2026-06-20.",
            "actions": [
                {"type": "fill", "selector": '[data-action-id="travel_origin_input"]', "value": "AAA"},
                {"type": "fill", "selector": '[data-action-id="travel_destination_input"]', "value": "BBB"},
                {"type": "fill", "selector": '[data-action-id="travel_date_input"]', "value": "2026-06-20"},
                {"type": "click", "selector": '[data-action-id="travel_search_submit"]'},
            ],
            "evaluator_id": "eval_search_flights",
            "success": [
                {"surface": "url", "path_equals": "/flights/search", "query_equals": "from=AAA&to=BBB&date=2026-06-20"},
                {"surface": "dom", "selector": '[data-flight-id="flt_demo_001"]', "visible": True},
                {"surface": "sqlite", "table": "flight_searches", "where": {"id": "src_task_001", "origin": "AAA", "destination": "BBB", "departure_date": "2026-06-20"}, "count_equals": 1},
            ],
        },
        {
            "task_id": "open_flight_task",
            "workflow": "navigate",
            "initial_state": "anonymous",
            "start_url": "/flights/search?from=AAA&to=BBB&date=2026-06-20",
            "instruction": "Open the deterministic flight from the local search results.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_flight_flt_demo_001"]'}],
            "evaluator_id": "eval_open_flight",
            "success": [
                {"surface": "url", "path_equals": "/flights/flt_demo_001"},
                {"surface": "dom", "selector": '[data-flight-id="flt_demo_001"]', "visible": True},
            ],
        },
        {
            "task_id": "book_flight_task",
            "workflow": "book",
            "initial_state": "authenticated",
            "start_url": "/flights/flt_demo_001",
            "instruction": "Book the deterministic flight and open local bookings.",
            "actions": [
                {"type": "click", "selector": '[data-action-id="book_flight"]'},
                {"type": "click", "selector": '[data-action-id="open_bookings"]'},
            ],
            "evaluator_id": "eval_book_flight",
            "success": [
                {"surface": "url", "path_equals": "/bookings"},
                {"surface": "dom", "selector": '[data-booking-id="bkg_task_001"]', "visible": True},
                {"surface": "sqlite", "table": "bookings", "where": {"id": "bkg_task_001", "user_id": "usr_demo_001", "flight_id": "flt_demo_001", "status": "confirmed"}, "count_equals": 1},
            ],
        },
        {
            "task_id": "bookings_task",
            "workflow": "bookings",
            "initial_state": "authenticated",
            "start_url": "/",
            "instruction": "Open the local bookings area.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_bookings"]'}],
            "evaluator_id": "eval_bookings",
            "success": [
                {"surface": "url", "path_equals": "/bookings"},
                {"surface": "dom", "selector": '[data-bookings-list]', "visible": True},
            ],
        },
        {
            "task_id": "profile_task",
            "workflow": "profile",
            "initial_state": "authenticated",
            "start_url": "/",
            "instruction": "Open the local synthetic traveler profile.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_profile"]'}],
            "evaluator_id": "eval_profile",
            "success": [
                {"surface": "url", "path_equals": "/profile/usr_demo_001"},
                {"surface": "dom", "selector": '[data-user-id="usr_demo_001"]', "visible": True},
            ],
        },
    ])
    return tasks


def _content_tasks(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content_route = next((route for route in routes if route["local_path"] != "/"), None)
    if content_route is None:
        raise ReplayAcquisitionError("content_portal_v1 requires at least one captured content route")
    target = content_route["local_path"]
    tasks = _base_tasks()
    tasks.extend([
        {
            "task_id": "navigate_content_task",
            "workflow": "navigate",
            "initial_state": "anonymous",
            "start_url": "/",
            "instruction": "Open the first deterministic captured content section.",
            "actions": [{"type": "click", "selector": '[data-action-id="open_content_route_001"]'}],
            "evaluator_id": "eval_navigate_content",
            "success": [
                {"surface": "url", "path_equals": urlsplit(target).path, "query_equals": urlsplit(target).query},
                {"surface": "dom", "selector": f'[data-webanything-route="{content_route["id"]}"]', "visible": True},
            ],
        },
        {
            "task_id": "bookmark_content_task",
            "workflow": "stateful_action",
            "initial_state": "authenticated",
            "start_url": "/",
            "instruction": "Bookmark the deterministic local guidance item.",
            "actions": [{"type": "click", "selector": '[data-action-id="bookmark_content"]'}],
            "evaluator_id": "eval_bookmark_content",
            "success": [
                {"surface": "dom", "selector": '[data-bookmarked-content-id="cnt_demo_001"]', "visible": True},
                {"surface": "sqlite", "table": "bookmarks", "where": {"user_id": "usr_demo_001", "content_id": "cnt_demo_001"}, "count_equals": 1},
            ],
        },
    ])
    return tasks


def _write_tasks(path: Path, routes: list[dict[str, Any]], *, profile: str = GENERIC_PROFILE) -> int:
    if profile == VIDEO_PROFILE:
        tasks = _video_tasks(routes)
        path.write_text("".join(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n" for task in tasks), encoding="utf-8")
        return len(tasks)
    if profile == COMMERCE_PROFILE:
        tasks = _commerce_tasks()
        path.write_text("".join(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n" for task in tasks), encoding="utf-8")
        return len(tasks)
    if profile == TRAVEL_PROFILE:
        tasks = _travel_tasks()
        path.write_text("".join(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n" for task in tasks), encoding="utf-8")
        return len(tasks)
    if profile == CONTENT_PROFILE:
        tasks = _content_tasks(routes)
        path.write_text("".join(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n" for task in tasks), encoding="utf-8")
        return len(tasks)
    tasks: list[dict[str, Any]] = [
        *_base_tasks(),
    ]
    action_number = 0
    for route in routes:
        for action in route["actions"]:
            action_number += 1
            replay: dict[str, Any] = {"type": action["action_type"], "selector": action["selector"]}
            if action["action_type"] in {"fill", "select", "press"}:
                replay["value"] = action.get("value") or "benchmark"
            tasks.append({
                "task_id": f"action_{action_number:04d}",
                "workflow": "stateful_action",
                "initial_state": "anonymous",
                "start_url": route["local_path"],
                "instruction": f"Use the local control: {action['label'] or action['tag']}.",
                "actions": [replay],
                "evaluator_id": f"eval_action_{action_number:04d}",
                "success": [
                    *(
                        [{"surface": "dom", "selector": f'[data-action-result="{action["id"]}"]', "visible": True}]
                        if profile == STATIC_LANDING_PROFILE else []
                    ),
                    {"surface": "sqlite", "table": "events", "where": {"action_id": action["id"]}, "count_equals": 1},
                ],
            })
    if action_number == 0:
        tasks.append({
            "task_id": "benchmark_state_task",
            "workflow": "stateful_action",
            "initial_state": "anonymous",
            "start_url": "/__webanything/benchmark",
            "instruction": "Toggle the local benchmark state.",
            "actions": [{"type": "click", "selector": "[data-action-id=benchmark_toggle]"}],
            "evaluator_id": "eval_benchmark_toggle",
            "success": [
                *(
                    [{"surface": "dom", "selector": '[data-benchmark-state="true"]', "visible": True}]
                    if profile == STATIC_LANDING_PROFILE else []
                ),
                {"surface": "sqlite", "table": "benchmark_state", "where": {"key": "benchmark_toggle", "value": "true"}, "count_equals": 1},
            ],
        })
    path.write_text("".join(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n" for task in tasks), encoding="utf-8")
    return len(tasks)


def _visual_spec(
    *,
    site: str,
    source_origin: str,
    routes: list[dict[str, Any]],
    viewport: dict[str, int],
    observed_origins: set[str],
    allowed_route_origins: set[str],
    auth_required: bool,
    authenticated_selector: str | None,
    private_selectors: list[dict[str, str]],
    allow_private_network: bool,
) -> dict[str, Any]:
    return {
        "schema": "webanything-synthetic-visual-capture-spec-v1",
        "site": site,
        "source_origin": source_origin,
        "robots_policy": "not_applicable_local_test" if allow_private_network else "respect",
        "user_agent": "WebAnythingReplayCapture/1.0",
        "viewport": viewport,
        "browser_profile": {"locale": "en-US", "timezone_id": "Etc/UTC", "accept_language": "en-US,en;q=0.9"},
        "auth": {"required": auth_required},
        "allowed_resource_origins": sorted(observed_origins - {source_origin}),
        "allowed_route_origins": sorted(allowed_route_origins - {source_origin}),
        "routes": [
            {
                "id": route["id"],
                "source_url": route["source_url"],
                "synthetic_route": route["local_path"],
                "comparison_mode": "source_visible",
                "wait_for": "body",
                "settle_ms": 1000,
                "private_selectors": private_selectors,
                "allow_quality_failures": ["landmarks_missing"],
                **({"authenticated_selector": authenticated_selector} if authenticated_selector else {}),
            }
            for route in routes
        ],
    }


def _freeze_synchronous_visual_reference(
    *,
    output_dir: Path,
    spec_path: Path,
    spec: dict[str, Any],
    route_records: list[dict[str, Any]],
    auth: dict[str, Any],
    private_fields_replaced: int,
    blocked_origins: set[str],
) -> dict[str, Any]:
    """Freeze visual artifacts captured in the same page state as replay DOM."""
    manifest = {
        "schema": REFERENCE_SCHEMA,
        "schema_version": 1,
        "state": "reference_frozen",
        "created_at": utc_now(),
        "site": spec["site"],
        "source_origin_sha256": hashlib.sha256(spec["source_origin"].encode("utf-8")).hexdigest(),
        "capture_spec_sha256": sha256_file(spec_path),
        "viewport": spec["viewport"],
        "browser_profile": spec["browser_profile"],
        "sanitizer_version": SANITIZER_VERSION,
        "content_strategy": CONTENT_STRATEGY,
        "auth": auth,
        "private_fields_replaced": private_fields_replaced,
        "blocked_resource_origin_sha256s": sorted(
            hashlib.sha256(origin.encode("utf-8")).hexdigest() for origin in blocked_origins
        ),
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "weights": dict(DEFAULT_WEIGHTS),
        "routes": route_records,
        "raw_material_persisted": True,
    }
    manifest["reference_sha256"] = canonical_sha256(manifest)
    target = output_dir / "synthetic_visual_reference.json"
    atomic_json(target, manifest)
    return load_visual_reference(target)


def _replay_spec(
    *,
    site: str,
    seed_url: str,
    routes: list[dict[str, Any]],
    reference: dict[str, Any],
    profile: str = GENERIC_PROFILE,
) -> dict[str, Any]:
    contract_routes = []
    for route in routes:
        action_ids = [action["id"] for action in route["actions"]]
        if profile == VIDEO_PROFILE:
            if route["local_path"] == "/":
                action_ids = [
                    *action_ids,
                    "open_video_vid_demo_001",
                    "open_category_video",
                    "search_input",
                    "search_submit",
                    "open_favorites",
                    "open_history",
                    "open_profile",
                ]
        elif profile == COMMERCE_PROFILE and route["local_path"] == "/":
            action_ids = [
                *action_ids,
                "open_product_prd_demo_001",
                "open_category_featured",
                "search_input",
                "search_submit",
                "open_cart",
                "open_orders",
                "open_profile",
            ]
        elif profile == TRAVEL_PROFILE and route["local_path"] == "/":
            action_ids = [
                *action_ids,
                "travel_origin_input",
                "travel_destination_input",
                "travel_date_input",
                "travel_search_submit",
                "open_bookings",
                "open_profile",
            ]
        elif profile == CONTENT_PROFILE and route["local_path"] == "/":
            action_ids = [
                *action_ids,
                "open_content_route_001",
                "bookmark_content",
            ]
        contract_routes.append({
            "id": route["id"],
            "path": route["local_path"],
            "kind": _route_kind(route["local_path"]),
            "source": "captured",
            "required_selectors": [
                f'[data-webanything-route="{route["id"]}"]',
                f'[data-route-kind="{_route_kind(route["local_path"])}"]',
            ],
            "required_action_ids": list(dict.fromkeys(action_ids)),
        })
    declared_paths = {route["path"] for route in contract_routes}
    if profile == VIDEO_PROFILE:
        for route in VIDEO_SYNTHETIC_ROUTES:
            if route["path"] not in declared_paths:
                contract_routes.append({**route, "source": "synthetic"})
    elif profile == COMMERCE_PROFILE:
        for route in COMMERCE_SYNTHETIC_ROUTES:
            if route["path"] not in declared_paths:
                contract_routes.append({**route, "source": "synthetic"})
    elif profile == TRAVEL_PROFILE:
        for route in TRAVEL_SYNTHETIC_ROUTES:
            if route["path"] not in declared_paths:
                contract_routes.append({**route, "source": "synthetic"})
    contract_routes.extend([
        {"id": "route_login", "path": "/__webanything/login", "kind": "auth", "source": "system", "required_selectors": ["[data-page=login]"], "required_action_ids": ["login_submit"]},
        {"id": "route_logout", "path": "/__webanything/logout", "kind": "auth", "source": "system", "required_selectors": ["[data-page=logout]"], "required_action_ids": ["logout_submit"]},
        {"id": "route_reset", "path": "/__webanything/reset.html", "kind": "reset", "source": "system", "required_selectors": ["[data-page=reset]"], "required_action_ids": []},
    ])
    if not any(route["actions"] for route in routes):
        contract_routes.append({"id": "route_benchmark", "path": "/__webanything/benchmark", "kind": "benchmark", "source": "system", "required_selectors": ["[data-page=benchmark]"], "required_action_ids": ["benchmark_toggle"]})
    return {
        "schema": "webanything-synthetic-replay-spec-v1",
        "seed_url": seed_url,
        "site_id": site,
        "product_archetype": profile,
        "coverage": _coverage(profile, routes),
        "clock": {"fixed_time": "2026-06-15T10:00:00Z", "timezone": "UTC"},
        "runtime": {"kind": "flask_sqlite", "entrypoint": "app.py", "health_path": "/_health", "proxy_prefix_aware": True},
        "storage": {"engine": "SQLite", "seed_path": "fixtures/seed.sqlite3", "runtime_path": "instance/runtime.sqlite3", "reset_mode": "atomic_seed_replace"},
        "auth": {"mode": "local_synthetic", "initial_state": "explicit_login", "state_scope": "instance", "demo_user_ids": ["usr_demo_001"], "source_credentials_persisted": False},
        "routes": contract_routes,
        "capture_manifest": {"path": "capture/manifest.json"},
        "visual_reference": {"path": "visual-reference/synthetic_visual_reference.json", "reference_sha256": reference["reference_sha256"]},
        "tasks": {"path": "tasks.jsonl"},
        "reset": {"supported": True, "entrypoint": "/__webanything/reset.html", "endpoint": "/api/reset", "method": "POST"},
        "evaluator": {"endpoint_template": "/api/evaluators/{evaluator_id}", "result_field": "passed", "authoritative_surfaces": ["dom", "url", "SQLite", "network"]},
        "network_policy": {"external_requests": 0, "allowed_origins": [], "upstream_javascript_execution": False},
        "content_policy": {"mode": "source_fidelity", "private_user_data_allowed": False, "source_assets_local_only": True},
        "concurrency": {"isolation": "isolated_instance", "max_parallel_sessions": 1},
    }


def capture_replay_url(
    *,
    url: str,
    site: str,
    output_dir: Path,
    route_values: list[str] | None = None,
    max_pages: int = 12,
    max_assets: int = 500,
    max_asset_bytes: int = 8_000_000,
    max_actions_per_route: int = 200,
    viewport_width: int = 1440,
    viewport_height: int = 1000,
    auth_profile_path: Path | None = None,
    key_path: Path | None = None,
    browser_path: Path | None = None,
    dependency_path: Path | None = None,
    private_selectors: list[str] | None = None,
    authenticated_selector: str | None = None,
    allow_private_network: bool = False,
    timeout_ms: int = 30_000,
    product_profile: str | None = None,
) -> dict[str, Any]:
    """Capture a bounded live surface and emit a compiled replay contract."""
    if not SITE_RE.fullmatch(site):
        raise ReplayAcquisitionError("site must match ^[a-z0-9][a-z0-9_]*$")
    if not 1 <= max_pages <= 100 or not 1 <= max_assets <= 10_000:
        raise ReplayAcquisitionError("max_pages and max_assets are outside supported bounds")
    if not 1 <= max_actions_per_route <= 1_000 or max_asset_bytes < 1_024:
        raise ReplayAcquisitionError("action or asset byte limits are outside supported bounds")
    if product_profile is not None and product_profile not in PRODUCT_PROFILES:
        raise ReplayAcquisitionError(f"unsupported product profile: {product_profile}")
    requested_seed_url = _canonical_url(url)
    seed_url = requested_seed_url
    if not _safe_read_route(seed_url):
        raise ReplayAcquisitionError("seed URL looks state-changing and is not safe for read-only capture")
    seed_local_path = _local_path(seed_url)
    source_origin = _origin(seed_url)
    allowed_route_origins = {source_origin}
    output_dir = output_dir.expanduser().resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        if any(output_dir.iterdir()):
            raise ReplayAcquisitionError("capture output already exists and is not empty")
        output_dir.rmdir()
    storage_state = None
    auth_public: dict[str, Any] = {"used": False, "profile_id": None}
    auth_required = auth_profile_path is not None or key_path is not None
    if auth_required:
        if auth_profile_path is None or key_path is None:
            raise AuthExpiredError("authorized capture requires both --auth-profile and --key-file")
        storage_state, metadata = load_auth_state(profile_path=auth_profile_path, key_path=key_path, site=site)
        if source_origin not in set(metadata["allowed_origins"]):
            raise ReplayAcquisitionError("authorized profile does not permit the seed origin")
        allowed_route_origins.update(metadata["allowed_origins"])
        auth_public = {"used": True, "profile_id": metadata["profile_id"]}
    if dependency_path:
        import sys
        sys.path.insert(0, str(dependency_path.expanduser().resolve()))
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CaptureFailedError("Playwright is required for replay acquisition") from exc

    private = _safe_private_selectors(private_selectors or [])
    if authenticated_selector is not None and (
        not authenticated_selector.strip() or len(authenticated_selector) > 300
    ):
        raise ReplayAcquisitionError("authenticated_selector must be a nonempty CSS selector under 300 characters")
    if authenticated_selector and not auth_required:
        raise ReplayAcquisitionError("authenticated_selector requires an authorized capture profile")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    capture_dir = temporary / "capture"
    assets_dir = capture_dir / "assets"
    pages_dir = capture_dir / "pages"
    visual_dir = temporary / "visual-reference"
    visual_routes_dir = visual_dir / "routes"
    assets_dir.mkdir(parents=True)
    pages_dir.mkdir(parents=True)
    visual_routes_dir.mkdir(parents=True)
    initial_routes = [seed_url]
    for value in route_values or []:
        candidate = urljoin(source_origin + "/", value)
        candidate = _canonical_url(candidate, expected_origins=allowed_route_origins)
        if not _safe_read_route(candidate):
            raise ReplayAcquisitionError(f"explicit route looks state-changing: {value}")
        initial_routes.append(candidate)
    queue = deque(dict.fromkeys(initial_routes))
    required_routes = set(queue)
    queued = set(queue)
    max_discovered_per_kind = max(2, min(6, max_pages // 2))
    queued_kind_counts: dict[str, int] = {}
    for item in queue:
        kind = _route_kind(_local_path(item))
        queued_kind_counts[kind] = queued_kind_counts.get(kind, 0) + 1
    captured_routes: list[dict[str, Any]] = []
    used_route_ids: set[str] = {"route_login", "route_logout", "route_reset", "route_benchmark"}
    asset_records: dict[str, dict[str, Any]] = {}
    captured_media_posters: list[dict[str, Any]] = []
    observed_origins: set[str] = {source_origin}
    private_fields_removed = 0
    visual_route_records: list[dict[str, Any]] = []
    blocked_origins: set[str] = set()
    robots_cache: dict[str, Any] = {}
    skipped_discovered_routes = 0
    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {
                "headless": True,
                "args": list(CAPTURE_BROWSER_ARGS),
            }
            if browser_path:
                launch["executable_path"] = str(browser_path.expanduser().resolve())
            browser = playwright.chromium.launch(**launch)
            context_options: dict[str, Any] = {
                "viewport": {"width": viewport_width, "height": viewport_height},
                "device_scale_factor": 1,
                "locale": "en-US",
                "timezone_id": "Etc/UTC",
                "user_agent": CAPTURE_USER_AGENT,
                "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
            }
            if storage_state is not None:
                context_options["storage_state"] = storage_state
            context = browser.new_context(**context_options)
            context_options.pop("storage_state", None)
            storage_state = None

            def request_route(route: Any) -> None:
                request_url = route.request.url
                parsed = urlsplit(request_url)
                if parsed.scheme not in {"http", "https"}:
                    route.continue_()
                    return
                try:
                    origin = _origin(request_url)
                    host = parsed.hostname or ""
                    if _is_private_host(host) and not allow_private_network:
                        blocked_origins.add(origin)
                        route.abort("blockedbyclient")
                        return
                    observed_origins.add(origin)
                    route.continue_()
                except SyntheticVisualError:
                    route.abort("blockedbyclient")

            context.route("**/*", request_route)
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            active_route_sha256: str | None = None

            def response_capture(response: Any) -> None:
                if len(asset_records) >= max_assets or response.status >= 400:
                    return
                resource_type = response.request.resource_type
                content_type = (response.headers.get("content-type") or "").split(";", 1)[0].casefold()
                if resource_type not in CAPTURE_RESOURCE_TYPES and content_type not in CONTENT_SUFFIXES:
                    return
                response_url = urlunsplit((*urlsplit(response.url)[:4], ""))
                if response_url in asset_records:
                    if active_route_sha256:
                        route_hashes = asset_records[response_url].setdefault("route_source_url_sha256s", [])
                        if active_route_sha256 not in route_hashes:
                            route_hashes.append(active_route_sha256)
                    return
                suffix = _suffix(content_type, response_url)
                if suffix is None:
                    return
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_asset_bytes:
                    return
                try:
                    body = response.body()
                except PlaywrightError:
                    return
                if not body or len(body) > max_asset_bytes:
                    return
                relative = f"capture/assets/{hashlib.sha256(response_url.encode()).hexdigest()[:24]}{suffix}"
                target = temporary / relative
                target.write_bytes(body)
                asset_records[response_url] = {
                    "path": relative,
                    "source_url": response_url,
                    "source_url_sha256": hashlib.sha256(response_url.encode()).hexdigest(),
                    "source_origin": _origin(response_url),
                    "content_type": content_type or "application/octet-stream",
                    "bytes": len(body),
                    "resource_type": resource_type,
                    "route_source_url_sha256s": [active_route_sha256] if active_route_sha256 else [],
                }

            page.on("response", response_capture)
            while queue and len(captured_routes) < max_pages:
                route_url = queue.popleft()
                seed_navigation = route_url == seed_url and not captured_routes and not auth_required
                navigation_candidates = _seed_navigation_candidates(route_url) if seed_navigation else [route_url]
                response = None
                selected_url = None
                last_navigation_failure: Exception | None = None
                for navigation_url in navigation_candidates:
                    active_route_sha256 = hashlib.sha256(navigation_url.encode()).hexdigest()
                    assets_before = set(asset_records)
                    try:
                        _check_robots(
                            navigation_url,
                            "WebAnythingReplayCapture/1.0",
                            "not_applicable_local_test" if allow_private_network else "respect",
                            allow_private_network=allow_private_network,
                            cache=robots_cache,
                        )
                        response = page.goto(navigation_url, wait_until="commit", timeout=timeout_ms)
                        if response is None:
                            raise CaptureFailedError(
                                f"capture navigation returned no response: {navigation_url}"
                            )
                        if response.status in {401, 403, 407, 429}:
                            if auth_required and response.status == 401:
                                raise AuthExpiredError(f"authorized state was rejected: {navigation_url}")
                            raise SourceBlockedError(
                                f"source returned blocking HTTP status {response.status}: {navigation_url}"
                            )
                        if response.status >= 400:
                            raise CaptureFailedError(
                                f"source returned HTTP status {response.status}: {navigation_url}"
                            )
                        final_url = _canonical_url(response.url)
                        if _is_private_host(urlsplit(final_url).hostname or "") and not allow_private_network:
                            raise SourceBlockedError("capture redirect reached a private or loopback host")
                        final_origin = _origin(final_url)
                        if final_origin not in allowed_route_origins:
                            if auth_required:
                                raise ReplayAcquisitionError(
                                    "authorized capture redirected outside its allowed origins"
                                )
                            if not seed_navigation and not _same_site_url(navigation_url, final_url):
                                raise SourceBlockedError(
                                    "capture route redirected outside the source site"
                                )
                            allowed_route_origins.add(final_origin)
                        if final_url != navigation_url:
                            _check_robots(
                                final_url,
                                "WebAnythingReplayCapture/1.0",
                                "not_applicable_local_test" if allow_private_network else "respect",
                                allow_private_network=allow_private_network,
                                cache=robots_cache,
                            )
                        page.wait_for_selector("body", state="visible", timeout=timeout_ms)
                        page.wait_for_timeout(1_200)
                        selected_url = final_url
                        break
                    except PlaywrightTimeoutError as exc:
                        if _page_has_capture_surface(page):
                            page.wait_for_timeout(1_200)
                            timed_url = _canonical_url(page.url)
                            timed_host = urlsplit(timed_url).hostname or ""
                            if _is_private_host(timed_host) and not allow_private_network:
                                last_navigation_failure = SourceBlockedError(
                                    "capture redirect reached a private or loopback host"
                                )
                                continue
                            timed_origin = _origin(timed_url)
                            if timed_origin not in allowed_route_origins:
                                if auth_required:
                                    last_navigation_failure = ReplayAcquisitionError(
                                        "authorized capture redirected outside its allowed origins"
                                    )
                                    continue
                                if not seed_navigation and not _same_site_url(navigation_url, timed_url):
                                    last_navigation_failure = SourceBlockedError(
                                        "capture route redirected outside the source site"
                                    )
                                    continue
                                allowed_route_origins.add(timed_origin)
                            if timed_url != navigation_url:
                                _check_robots(
                                    timed_url,
                                    "WebAnythingReplayCapture/1.0",
                                    "not_applicable_local_test" if allow_private_network else "respect",
                                    allow_private_network=allow_private_network,
                                    cache=robots_cache,
                                )
                            selected_url = timed_url
                            break
                        last_navigation_failure = CaptureFailedError(
                            f"capture route timed out: {navigation_url}"
                        )
                        last_navigation_failure.__cause__ = exc
                    except (AuthExpiredError, CaptureFailedError, ReplayAcquisitionError, SourceBlockedError) as exc:
                        last_navigation_failure = exc
                    for asset_url in set(asset_records) - assets_before:
                        asset_path = temporary / asset_records[asset_url]["path"]
                        asset_path.unlink(missing_ok=True)
                        del asset_records[asset_url]
                    if not seed_navigation:
                        break
                if selected_url is None:
                    if route_url in required_routes:
                        if last_navigation_failure is not None:
                            raise last_navigation_failure
                        raise CaptureFailedError(f"capture navigation failed: {route_url}")
                    skipped_discovered_routes += 1
                    continue
                if seed_navigation:
                    source_origin = _origin(selected_url)
                    allowed_route_origins.add(source_origin)
                    observed_origins.add(source_origin)
                    seed_url = selected_url
                    required_routes.add(selected_url)
                    queued.add(selected_url)
                route_url = selected_url
                active_route_sha256 = hashlib.sha256(route_url.encode()).hexdigest()
                challenge_marker = _visible_interactive_challenge(page)
                if challenge_marker:
                    raise AuthExpiredError(
                        f"interactive authorization required ({challenge_marker}): {route_url}"
                    )
                if auth_required and authenticated_selector and page.locator(authenticated_selector).count() == 0:
                    raise AuthExpiredError(f"authorized state no longer reaches the expected surface: {route_url}")
                local_path = (
                    seed_local_path if route_url == seed_url else _replay_local_path(route_url, source_origin)
                )
                if any(route["local_path"] == local_path for route in captured_routes):
                    skipped_discovered_routes += 1
                    continue
                route_id = _route_id(route_url, used_route_ids)
                snapshot = page.evaluate(
                    SNAPSHOT_SCRIPT,
                    {"routeId": route_id, "maxActions": max_actions_per_route, "privateSelectors": private},
                )
                for background_url in snapshot.get("background_assets") or []:
                    if len(asset_records) >= max_assets:
                        break
                    if not isinstance(background_url, str):
                        continue
                    try:
                        background_parts = urlsplit(background_url)
                        if (
                            background_parts.scheme not in {"http", "https"}
                            or not background_parts.hostname
                            or background_parts.username
                            or background_parts.password
                        ):
                            continue
                        absolute_background_url = urlunsplit((
                            background_parts.scheme,
                            background_parts.netloc,
                            background_parts.path or "/",
                            background_parts.query,
                            "",
                        ))
                        background_origin = _origin(absolute_background_url)
                        background_host = urlsplit(absolute_background_url).hostname or ""
                    except SyntheticVisualError:
                        continue
                    if absolute_background_url in asset_records:
                        route_hashes = asset_records[absolute_background_url].setdefault(
                            "route_source_url_sha256s", []
                        )
                        if active_route_sha256 and active_route_sha256 not in route_hashes:
                            route_hashes.append(active_route_sha256)
                        continue
                    if _is_private_host(background_host) and not allow_private_network:
                        blocked_origins.add(background_origin)
                        continue
                    observed_origins.add(background_origin)
                    try:
                        background_response = context.request.get(
                            absolute_background_url,
                            timeout=timeout_ms,
                        )
                    except PlaywrightError:
                        continue
                    if background_response.status >= 400:
                        continue
                    content_type = (
                        background_response.headers.get("content-type") or ""
                    ).split(";", 1)[0].casefold()
                    suffix = _suffix(content_type, absolute_background_url)
                    if suffix not in {".png", ".jpg", ".webp", ".avif", ".gif", ".svg", ".ico"}:
                        continue
                    length = background_response.headers.get("content-length")
                    if length and length.isdigit() and int(length) > max_asset_bytes:
                        continue
                    try:
                        body = background_response.body()
                    except PlaywrightError:
                        continue
                    if not body or len(body) > max_asset_bytes:
                        continue
                    relative = (
                        "capture/assets/"
                        f"{hashlib.sha256(absolute_background_url.encode()).hexdigest()[:24]}{suffix}"
                    )
                    (temporary / relative).write_bytes(body)
                    asset_records[absolute_background_url] = {
                        "path": relative,
                        "source_url": absolute_background_url,
                        "source_url_sha256": hashlib.sha256(
                            absolute_background_url.encode()
                        ).hexdigest(),
                        "source_origin": background_origin,
                        "content_type": content_type or "application/octet-stream",
                        "bytes": len(body),
                        "resource_type": "computed_background_image",
                        "route_source_url_sha256s": (
                            [active_route_sha256] if active_route_sha256 else []
                        ),
                    }
                if snapshot.get("actions_truncated"):
                    raise ReplayAcquisitionError(
                        f"route {route_url} exposes {snapshot.get('action_count')} controls; increase --max-actions-per-route"
                    )
                route_private_fields_removed = int(snapshot.get("private_fields_removed") or 0)
                private_fields_removed += route_private_fields_removed
                page_relative = f"capture/pages/{route_id}.html"
                metadata_relative = f"capture/pages/{route_id}.json"
                route_record = {
                    "id": route_id,
                    "source_url": route_url,
                    "local_path": local_path,
                    "title": str(snapshot.get("title") or "")[:500],
                    "html_path": page_relative,
                    "metadata_path": metadata_relative,
                    "actions": _sanitize_actions(
                        snapshot.get("actions") or [], source_origin, allowed_route_origins
                    ),
                    "visible_action_count": int(snapshot.get("visible_action_count") or 0),
                    "occluded_action_count": int(snapshot.get("occluded_action_count") or 0),
                }
                media_posters: list[dict[str, str]] = []
                for media in snapshot.get("media_surfaces") or []:
                    if not isinstance(media, dict) or media.get("tag") != "video":
                        continue
                    node_id = media.get("node_id")
                    if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", node_id):
                        continue
                    locator = page.locator(f'[data-webanything-node-id="{node_id}"]')
                    if locator.count() != 1:
                        continue
                    try:
                        png = locator.screenshot(type="png", animations="disabled")
                    except PlaywrightError:
                        continue
                    if not png or len(png) > max_asset_bytes:
                        continue
                    poster_name = hashlib.sha256(f"{route_id}:{node_id}:poster".encode()).hexdigest()[:24] + ".png"
                    poster_relative = f"capture/assets/{poster_name}"
                    (temporary / poster_relative).write_bytes(png)
                    media_posters.append({
                        "node_id": node_id,
                        "path": poster_relative.removeprefix("capture/"),
                        "sha256": hashlib.sha256(png).hexdigest(),
                    })
                    captured_media_posters.append({
                        "path": poster_relative,
                        "source_url_sha256": hashlib.sha256(f"captured-media-poster:{route_id}:{node_id}".encode()).hexdigest(),
                        "source_origin": source_origin,
                        "content_type": "image/png",
                        "bytes": len(png),
                        "resource_type": "captured_media_poster",
                        "route_ids": [route_id],
                    })
                (temporary / page_relative).write_text(str(snapshot["html"]), encoding="utf-8")
                atomic_json(temporary / metadata_relative, {
                    "schema": "webanything-captured-route-v1",
                    **route_record,
                    "source_url": None,
                    "source_url_sha256": hashlib.sha256(route_url.encode()).hexdigest(),
                    "shadow_roots": snapshot.get("shadow_roots") or [],
                    "dom_topology": snapshot.get("dom_topology") or [],
                    "computed_styles": snapshot.get("computed_styles") or [],
                    "computed_styles_truncated": snapshot.get("computed_styles_truncated") is True,
                    "media_posters": media_posters,
                })
                geometry = validate_geometry(page.evaluate(GEOMETRY_SCRIPT))
                quality = geometry_quality(geometry)
                accepted_quality_failure_codes = _accepted_replay_quality_failure_codes(geometry, quality)
                remaining_quality_failures = [
                    item for item in quality["failures"] if item["code"] not in set(accepted_quality_failure_codes)
                ]
                if remaining_quality_failures:
                    codes = ", ".join(item["code"] for item in remaining_quality_failures)
                    if _is_optional_discovered_route(route_url, required_routes):
                        skipped_discovered_routes += 1
                        private_fields_removed -= route_private_fields_removed
                        (temporary / page_relative).unlink(missing_ok=True)
                        (temporary / metadata_relative).unlink(missing_ok=True)
                        for poster in media_posters:
                            (capture_dir / poster["path"]).unlink(missing_ok=True)
                        captured_media_posters[:] = [
                            item for item in captured_media_posters
                            if route_id not in item.get("route_ids", [])
                        ]
                        continue
                    raise ReplayAcquisitionError(f"captured replay visual quality failed for {route_id}: {codes}")
                captured_routes.append(route_record)
                raw_png = page.screenshot(type="png", full_page=False, animations="disabled")
                comparison_png = sanitize_screenshot(raw_png, geometry)
                screenshot_relative = f"routes/{route_id}.source-visible.png"
                comparison_relative = f"routes/{route_id}.comparison.png"
                geometry_relative = f"routes/{route_id}.geometry.json"
                screenshot_path = visual_dir / screenshot_relative
                comparison_path = visual_dir / comparison_relative
                geometry_path = visual_dir / geometry_relative
                screenshot_path.write_bytes(raw_png)
                comparison_path.write_bytes(comparison_png)
                atomic_json(geometry_path, geometry)
                effective_quality = quality if not accepted_quality_failure_codes else {
                    **quality,
                    "status": "passed",
                    "failures": [],
                }
                visual_route_records.append({
                    "id": route_id,
                    "synthetic_route": local_path,
                    "comparison_mode": "source_visible",
                    "source_url_sha256": hashlib.sha256(route_url.encode("utf-8")).hexdigest(),
                    "screenshot": {"path": screenshot_relative, "sha256": sha256_file(screenshot_path)},
                    "comparison_screenshot": {"path": comparison_relative, "sha256": sha256_file(comparison_path)},
                    "geometry": {"path": geometry_relative, "sha256": sha256_file(geometry_path)},
                    "metrics": geometry_metrics(geometry),
                    "quality": effective_quality,
                    "accepted_quality_failure_codes": accepted_quality_failure_codes,
                })
                discovered: list[str] = []
                for link in snapshot.get("links") or []:
                    try:
                        candidate = _canonical_url(link, expected_origins=allowed_route_origins)
                    except ReplayAcquisitionError:
                        continue
                    if not _safe_read_route(candidate):
                        continue
                    discovered.append(candidate)
                kind_priority = {
                    "favorites": 0,
                    "history": 1,
                    "profile": 2,
                    "search": 3,
                    "detail": 4,
                    "browse": 5,
                    "home": 6,
                }
                for candidate in sorted(
                    dict.fromkeys(discovered),
                    key=lambda item: (
                        kind_priority.get(_route_kind(item), 9),
                        bool(urlsplit(item).query),
                    ),
                ):
                    candidate_kind = _route_kind(candidate)
                    if queued_kind_counts.get(candidate_kind, 0) >= max_discovered_per_kind:
                        continue
                    if candidate not in queued and len(queued) < max_pages * 20:
                        queued.add(candidate)
                        queue.append(candidate)
                        queued_kind_counts[candidate_kind] = queued_kind_counts.get(candidate_kind, 0) + 1
            context.close()
            browser.close()

        asset_paths = {record["source_url"]: record["path"] for record in asset_records.values()}
        for record in asset_records.values():
            path = temporary / record["path"]
            if path.suffix.casefold() == ".css":
                text = path.read_text(encoding="utf-8", errors="replace")
                path.write_text(_rewrite_css(text, record["source_url"], asset_paths, Path(record["path"])), encoding="utf-8")
        for route in captured_routes:
            html_path = temporary / route["html_path"]
            route_origin = _origin(route["source_url"])
            markup = _strip_html_comments(
                _rewrite_markup(
                    html_path.read_text(encoding="utf-8"),
                    route_origin,
                    asset_paths,
                    primary_origin=source_origin,
                    allowed_route_origins=allowed_route_origins,
                )
            )
            metadata_path = temporary / route["metadata_path"]
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            for shadow in metadata.get("shadow_roots") or []:
                shadow["html"] = _strip_html_comments(
                    _rewrite_markup(
                        str(shadow.get("html") or ""),
                        route_origin,
                        asset_paths,
                        primary_origin=source_origin,
                        allowed_route_origins=allowed_route_origins,
                    )
                )
            _localize_computed_style_backgrounds(
                metadata,
                route["source_url"],
                asset_paths,
            )
            markup = _inject_computed_style_freeze(markup, metadata)
            markup = _inject_captured_media_posters(markup, metadata)
            html_path.write_text(_inject_dom_topology_restore(markup, metadata), encoding="utf-8")
            atomic_json(metadata_path, metadata)
        route_id_by_source_sha256 = {
            hashlib.sha256(route["source_url"].encode()).hexdigest(): route["id"]
            for route in captured_routes
        }
        atomic_json(capture_dir / "asset-map.json", {
            "schema": "webanything-captured-assets-v1",
            "assets": [
                *[
                    {
                        **{
                            key: value
                            for key, value in record.items()
                            if key not in {"source_url", "route_source_url_sha256s"}
                        },
                        "route_ids": sorted({
                            route_id_by_source_sha256[value]
                            for value in record.get("route_source_url_sha256s") or []
                            if value in route_id_by_source_sha256
                        }),
                    }
                    for record in sorted(asset_records.values(), key=lambda item: item["path"])
                ],
                *sorted(captured_media_posters, key=lambda item: item["path"]),
            ],
        })
        route_map = {"routes": [{"source_url": route["source_url"], "local_path": route["local_path"]} for route in captured_routes]}
        atomic_json(temporary / "capture-routes.json", route_map)
        privacy_report = {
            "schema": "webanything-replay-privacy-report-v1",
            "status": "passed",
            "source_credentials_removed": True,
            "private_fields_removed": private_fields_removed,
            "source_auth_persisted": False,
            "storage_state_persisted": False,
            "raw_response_headers_persisted": False,
        }
        atomic_json(temporary / "privacy-report.json", privacy_report)
        freeze_replay_capture(
            bundle_root=temporary,
            capture_dir=capture_dir,
            source_origin=source_origin,
            allowed_source_origins=allowed_route_origins,
            routes_path=temporary / "capture-routes.json",
            privacy_report_path=temporary / "privacy-report.json",
            output_path=capture_dir / "manifest.json",
        )
        visual_spec = _visual_spec(
            site=site,
            source_origin=source_origin,
            routes=captured_routes,
            viewport={"width": viewport_width, "height": viewport_height},
            observed_origins=observed_origins,
            allowed_route_origins=allowed_route_origins,
            auth_required=auth_required,
            authenticated_selector=authenticated_selector,
            private_selectors=private,
            allow_private_network=allow_private_network,
        )
        atomic_json(temporary / "visual-capture-spec.json", visual_spec)
        reference = _freeze_synchronous_visual_reference(
            output_dir=visual_dir,
            spec_path=temporary / "visual-capture-spec.json",
            spec=visual_spec,
            route_records=visual_route_records,
            auth=auth_public,
            private_fields_replaced=private_fields_removed,
            blocked_origins=blocked_origins,
        )
        profile = product_profile or _infer_profile(requested_seed_url, captured_routes)
        _write_seed(temporary / "fixtures/seed.sqlite3", captured_routes, profile=profile)
        task_count = _write_tasks(temporary / "tasks.jsonl", captured_routes, profile=profile)
        spec = _replay_spec(
            site=site,
            seed_url=requested_seed_url,
            routes=captured_routes,
            reference=reference,
            profile=profile,
        )
        atomic_json(temporary / "replay-spec.json", spec)
        contract = compile_synthetic_replay_contract(
            temporary / "replay-spec.json", temporary / "synthetic_replay_contract.json"
        )
        acquisition = {
            "schema": REPLAY_ACQUISITION_SCHEMA,
            "status": "completed",
            "site": site,
            "seed_url": requested_seed_url,
            "source_origin": source_origin,
            "routes": len(captured_routes),
            "actions": sum(len(route["actions"]) for route in captured_routes),
            "visible_actions_observed": sum(route["visible_action_count"] for route in captured_routes),
            "occluded_actions_excluded": sum(route["occluded_action_count"] for route in captured_routes),
            "assets": len(asset_records),
            "asset_bytes": sum(record["bytes"] for record in asset_records.values()),
            "tasks": task_count,
            "skipped_discovered_routes": skipped_discovered_routes,
            "product_profile": profile,
            "coverage": spec["coverage"],
            "auth_used": auth_required,
            "source_auth_persisted": False,
            "private_fields_removed": private_fields_removed,
            "capture_bundle_sha256": contract["capture_manifest"]["bundle_sha256"],
            "contract_sha256": sha256_file(temporary / "synthetic_replay_contract.json"),
            "contract_path": "synthetic_replay_contract.json",
        }
        atomic_json(temporary / "acquisition.json", acquisition)
        os.replace(temporary, output_dir)
        return {**acquisition, "output_dir": str(output_dir), "contract_path": str(output_dir / "synthetic_replay_contract.json")}
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _browser_selector_xpath(selector: str) -> str:
    """Translate the deliberately small browser-export selector dialect."""
    selector = selector.strip().removesuffix(">").strip()
    parts = [part for part in selector.split(" > ") if part]
    converted: list[str] = []
    for part in parts:
        match = re.fullmatch(r"([a-z][a-z0-9-]*)(?::nth-of-type\(([1-9][0-9]*)\))?", part)
        if not match:
            raise ReplayAcquisitionError(f"browser export contains an unsupported selector: {selector}")
        tag, index = match.groups()
        converted.append(f"{tag}[{index}]" if index else tag)
    return "/" + "/".join(converted)


def _inject_browser_export_ids(
    markup: str,
    *,
    topology: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> str:
    try:
        from lxml import etree, html as lxml_html
    except ImportError as exc:
        raise ReplayAcquisitionError(
            "authenticated browser import requires the WebAnything browser extra (lxml)"
        ) from exc
    document = lxml_html.document_fromstring(markup)
    for record in topology:
        selector = record.get("source_selector")
        node_id = record.get("node_id")
        if not isinstance(selector, str) or not isinstance(node_id, str):
            continue
        matches = document.xpath(_browser_selector_xpath(selector))
        if len(matches) == 1:
            matches[0].set("data-webanything-node-id", node_id)
    for action in actions:
        selector = action.get("source_selector")
        action_id = action.get("id")
        if not isinstance(selector, str) or not isinstance(action_id, str):
            continue
        matches = document.xpath(_browser_selector_xpath(selector))
        if len(matches) == 1:
            matches[0].set("data-action-id", action_id)
    for executable in document.xpath("//script|//noscript"):
        parent = executable.getparent()
        if parent is not None:
            parent.remove(executable)
    for element in document.iter():
        for name in list(element.attrib):
            if name.casefold().startswith("on") or name.casefold() in {
                "nonce", "integrity", "value", "checked", "selected", "data-token", "data-session"
            }:
                del element.attrib[name]
    return etree.tostring(
        document,
        encoding="unicode",
        method="html",
        doctype="<!doctype html>",
    )


def _browser_export_geometry(snapshot: dict[str, Any], *, width: int, height: int) -> dict[str, Any]:
    action_nodes = {
        item.get("source_selector")
        for item in snapshot.get("actions") or []
        if isinstance(item, dict)
    }
    elements: list[dict[str, Any]] = []
    landmarks: dict[str, dict[str, int]] = {}
    topology = {
        item.get("node_id"): item
        for item in snapshot.get("dom_topology") or []
        if isinstance(item, dict)
    }
    for record in snapshot.get("computed_styles") or []:
        if not isinstance(record, dict):
            continue
        rect = record.get("rect") or {}
        try:
            x = max(0, min(int(rect["x"]), width - 1))
            y = max(0, min(int(rect["y"]), height - 1))
            rectangle_width = max(0, min(int(rect["width"]), width - x))
            rectangle_height = max(0, min(int(rect["height"]), height - y))
        except (KeyError, TypeError, ValueError):
            continue
        if rectangle_width < 2 or rectangle_height < 2:
            continue
        tag = str(record.get("tag") or "div").casefold()
        selector = record.get("source_selector")
        styles = record.get("styles") or {}
        if selector in action_nodes or tag in {"button", "input", "select", "textarea", "summary"}:
            category = "control"
        elif tag in {"img", "picture", "video", "canvas", "svg"}:
            category = "media"
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "span", "label", "li"}:
            category = "text"
        else:
            category = "container"
        position = str(styles.get("position") or "flow").casefold()
        if position not in {"fixed", "sticky", "absolute"}:
            position = "flow"
        radius_value = str(styles.get("border-radius") or styles.get("borderRadius") or "0")
        radius_match = re.match(r"([0-9]+)", radius_value)
        background = str(styles.get("background-color") or styles.get("backgroundColor") or "")
        surface = category in {"control", "media"} or bool(background and background not in {"transparent", "rgba(0, 0, 0, 0)"})
        elements.append({
            "category": category,
            "x": x,
            "y": y,
            "width": rectangle_width,
            "height": rectangle_height,
            "position": position,
            "radius": int(radius_match.group(1)) if radius_match else 0,
            "surface": surface,
        })
        topology_record = topology.get(record.get("node_id")) or {}
        landmark = str(topology_record.get("tag") or "").casefold()
        if landmark in {"header", "nav", "main", "aside", "footer"} and landmark not in landmarks:
            landmarks[landmark] = {
                "x": x, "y": y, "width": rectangle_width, "height": rectangle_height,
            }
    geometry = validate_geometry({
        "schema": "webanything-sanitized-dom-geometry-v1",
        "viewport": {"width": width, "height": height},
        "elements": elements,
        "landmarks": landmarks,
        "truncated": False,
    })
    return geometry


def import_authorized_browser_capture(
    *,
    baseline_dir: Path,
    browser_export_dir: Path,
    output_dir: Path,
    site: str,
    seed_url: str,
    product_profile: str,
) -> dict[str, Any]:
    """Merge a credential-free authorized browser export into a replay capture.

    The export contains rendered DOM, computed styles, public assets and screenshots;
    cookies, browser storage and response headers are intentionally unsupported.
    """
    if not SITE_RE.fullmatch(site) or product_profile not in PRODUCT_PROFILES:
        raise ReplayAcquisitionError("authorized browser import site/profile is invalid")
    baseline_dir = baseline_dir.expanduser().resolve()
    browser_export_dir = browser_export_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not (baseline_dir / "synthetic_replay_contract.json").is_file():
        raise ReplayAcquisitionError("authorized browser import baseline is incomplete")
    routes_dir = browser_export_dir / "routes"
    snapshots = sorted(
        path for path in routes_dir.glob("route_*.json") if not path.name.endswith(".assets.json")
    )
    if not snapshots:
        raise ReplayAcquisitionError("authorized browser export contains no routes")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ReplayAcquisitionError("authorized browser import output already exists and is not empty")
    requested_seed_url = _canonical_url(seed_url)
    source_origin = _origin(requested_seed_url)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        shutil.copytree(baseline_dir, temporary, dirs_exist_ok=True)
        capture_dir = temporary / "capture"
        visual_dir = temporary / "visual-reference"
        baseline_routes = json.loads((temporary / "capture-routes.json").read_text())["routes"]
        route_source_by_local = {item["local_path"]: item["source_url"] for item in baseline_routes}
        captured_routes: list[dict[str, Any]] = []
        for metadata_path in sorted((capture_dir / "pages").glob("route_*.json")):
            metadata = json.loads(metadata_path.read_text())
            local_path = str(metadata.get("local_path") or "")
            source_url = route_source_by_local.get(local_path)
            if not source_url:
                continue
            captured_routes.append({
                "id": metadata["id"],
                "source_url": source_url,
                "local_path": local_path,
                "title": metadata.get("title") or "",
                "html_path": metadata["html_path"],
                "metadata_path": metadata["metadata_path"],
                "actions": metadata.get("actions") or [],
                "visible_action_count": metadata.get("visible_action_count") or 0,
                "occluded_action_count": metadata.get("occluded_action_count") or 0,
            })
        asset_map_path = capture_dir / "asset-map.json"
        asset_map = json.loads(asset_map_path.read_text())
        asset_records = list(asset_map.get("assets") or [])
        new_assets: dict[str, dict[str, Any]] = {}
        observed_origins = {source_origin}
        new_routes: list[dict[str, Any]] = []
        visual_route_records = list(
            json.loads((visual_dir / "synthetic_visual_reference.json").read_text()).get("routes") or []
        )
        from PIL import Image
        for snapshot_path in snapshots:
            snapshot = json.loads(snapshot_path.read_text())
            route_id = snapshot_path.stem
            source_url = _canonical_url(str(snapshot.get("source_url") or ""), expected_origin=source_origin)
            local_path = _local_path(source_url)
            if any(route["local_path"] == local_path for route in captured_routes):
                continue
            actions = _sanitize_actions(snapshot.get("actions") or [], source_origin)
            markup = _inject_browser_export_ids(
                str(snapshot.get("html") or ""),
                topology=snapshot.get("dom_topology") or [],
                actions=snapshot.get("actions") or [],
            )
            bundle_path = snapshot_path.with_name(f"{route_id}.assets.json")
            if not bundle_path.is_file():
                raise ReplayAcquisitionError(f"authorized browser route lacks an asset bundle: {route_id}")
            bundle = json.loads(bundle_path.read_text())
            for item in bundle.get("assets") or []:
                if not isinstance(item, dict):
                    continue
                source_asset = Path(str(item.get("path") or "")).resolve()
                asset_url = str(item.get("url") or "")
                if not source_asset.is_file() or not asset_url.startswith(("http://", "https://")):
                    continue
                parsed_asset = urlsplit(asset_url)
                if parsed_asset.username or parsed_asset.password:
                    continue
                observed_origins.add(_origin(asset_url))
                if asset_url in new_assets:
                    continue
                content_type = str(item.get("contentType") or "application/octet-stream")
                suffix = _suffix(content_type, asset_url) or source_asset.suffix.casefold()
                if suffix not in SUFFIXES:
                    continue
                relative = f"capture/assets/{hashlib.sha256(asset_url.encode()).hexdigest()[:24]}{suffix}"
                target = temporary / relative
                if not target.exists():
                    shutil.copy2(source_asset, target)
                new_assets[asset_url] = {
                    "path": relative,
                    "source_url_sha256": hashlib.sha256(asset_url.encode()).hexdigest(),
                    "source_origin": _origin(asset_url),
                    "content_type": content_type,
                    "bytes": target.stat().st_size,
                    "resource_type": str(item.get("kind") or "asset"),
                    "route_ids": [route_id],
                }
            page_relative = f"capture/pages/{route_id}.html"
            metadata_relative = f"capture/pages/{route_id}.json"
            topology = [
                {key: value for key, value in item.items() if key != "source_selector"}
                for item in snapshot.get("dom_topology") or [] if isinstance(item, dict)
            ]
            computed = [
                {
                    "node_id": item.get("node_id"),
                    "style": item.get("styles") or {},
                    "rect": item.get("rect") or {},
                    "tag": item.get("tag") or "div",
                }
                for item in snapshot.get("computed_styles") or [] if isinstance(item, dict)
            ]
            public_actions = [
                {key: value for key, value in item.items() if key != "source_selector"}
                for item in actions
            ]
            route_record = {
                "id": route_id,
                "source_url": source_url,
                "local_path": local_path,
                "title": str(snapshot.get("title") or "")[:500],
                "html_path": page_relative,
                "metadata_path": metadata_relative,
                "actions": public_actions,
                "visible_action_count": int(snapshot.get("visible_action_count") or len(public_actions)),
                "occluded_action_count": int(snapshot.get("occluded_action_count") or 0),
            }
            (temporary / page_relative).write_text(markup, encoding="utf-8")
            atomic_json(temporary / metadata_relative, {
                "schema": "webanything-captured-route-v1",
                **route_record,
                "source_url": None,
                "source_url_sha256": hashlib.sha256(source_url.encode()).hexdigest(),
                "shadow_roots": [],
                "dom_topology": topology,
                "computed_styles": computed,
                "computed_styles_truncated": bool(snapshot.get("computed_styles_truncated")),
                "media_posters": [],
            })
            screenshot_source = snapshot_path.with_name(f"{route_id}.source-visible.png")
            with Image.open(screenshot_source) as screenshot_image:
                width, height = screenshot_image.size
            geometry = _browser_export_geometry(snapshot, width=width, height=height)
            quality = geometry_quality(geometry)
            accepted = _accepted_replay_quality_failure_codes(geometry, quality)
            remaining = [item for item in quality["failures"] if item["code"] not in set(accepted)]
            if remaining:
                raise ReplayAcquisitionError(
                    f"authorized browser geometry failed for {route_id}: "
                    + ", ".join(item["code"] for item in remaining)
                )
            screenshot_relative = f"routes/{route_id}.source-visible.png"
            comparison_relative = f"routes/{route_id}.comparison.png"
            geometry_relative = f"routes/{route_id}.geometry.json"
            shutil.copy2(screenshot_source, visual_dir / screenshot_relative)
            (visual_dir / comparison_relative).write_bytes(
                sanitize_screenshot(screenshot_source.read_bytes(), geometry)
            )
            atomic_json(visual_dir / geometry_relative, geometry)
            visual_route_records.append({
                "id": route_id,
                "synthetic_route": local_path,
                "comparison_mode": "source_visible",
                "source_url_sha256": hashlib.sha256(source_url.encode()).hexdigest(),
                "screenshot": {"path": screenshot_relative, "sha256": sha256_file(visual_dir / screenshot_relative)},
                "comparison_screenshot": {"path": comparison_relative, "sha256": sha256_file(visual_dir / comparison_relative)},
                "geometry": {"path": geometry_relative, "sha256": sha256_file(visual_dir / geometry_relative)},
                "metrics": geometry_metrics(geometry),
                "quality": {**quality, "status": "passed", "failures": []},
                "accepted_quality_failure_codes": accepted,
            })
            new_routes.append(route_record)
        if not new_routes:
            raise ReplayAcquisitionError("authorized browser import did not add any routes")
        asset_paths = {url: record["path"] for url, record in new_assets.items()}
        for url, record in new_assets.items():
            path = temporary / record["path"]
            if path.suffix.casefold() == ".css":
                path.write_text(
                    _rewrite_css(path.read_text(encoding="utf-8", errors="replace"), url, asset_paths, Path(record["path"])),
                    encoding="utf-8",
                )
        for route in new_routes:
            html_path = temporary / route["html_path"]
            metadata_path = temporary / route["metadata_path"]
            metadata = json.loads(metadata_path.read_text())
            markup = _strip_html_comments(_rewrite_markup(
                html_path.read_text(encoding="utf-8"), route["source_url"], asset_paths,
                primary_origin=source_origin, allowed_route_origins={source_origin},
            ))
            _localize_computed_style_backgrounds(metadata, route["source_url"], asset_paths)
            markup = _inject_computed_style_freeze(markup, metadata)
            html_path.write_text(_inject_dom_topology_restore(markup, metadata), encoding="utf-8")
            atomic_json(metadata_path, metadata)
        captured_routes.extend(new_routes)
        asset_records.extend(new_assets.values())
        atomic_json(asset_map_path, {"schema": "webanything-captured-assets-v1", "assets": asset_records})
        atomic_json(temporary / "capture-routes.json", {
            "routes": [
                {"source_url": route["source_url"], "local_path": route["local_path"]}
                for route in captured_routes
            ]
        })
        privacy = json.loads((temporary / "privacy-report.json").read_text())
        privacy["private_fields_removed"] = int(privacy.get("private_fields_removed") or 0)
        atomic_json(temporary / "privacy-report.json", privacy)
        freeze_replay_capture(
            bundle_root=temporary,
            capture_dir=capture_dir,
            source_origin=source_origin,
            routes_path=temporary / "capture-routes.json",
            privacy_report_path=temporary / "privacy-report.json",
            output_path=capture_dir / "manifest.json",
        )
        viewport = json.loads(
            (visual_dir / visual_route_records[0]["geometry"]["path"]).read_text()
        )["viewport"]
        if any(
            json.loads((visual_dir / item["geometry"]["path"]).read_text())["viewport"] != viewport
            for item in visual_route_records
        ):
            raise ReplayAcquisitionError("authorized browser routes must use the baseline viewport")
        visual_spec = _visual_spec(
            site=site,
            source_origin=source_origin,
            routes=captured_routes,
            viewport=viewport,
            observed_origins=observed_origins,
            allowed_route_origins={source_origin},
            auth_required=True,
            authenticated_selector=None,
            private_selectors=_safe_private_selectors([]),
            allow_private_network=False,
        )
        atomic_json(temporary / "visual-capture-spec.json", visual_spec)
        reference = _freeze_synchronous_visual_reference(
            output_dir=visual_dir,
            spec_path=temporary / "visual-capture-spec.json",
            spec=visual_spec,
            route_records=visual_route_records,
            auth={"used": True, "profile_id": canonical_sha256({"site": site, "origin": source_origin})},
            private_fields_replaced=privacy["private_fields_removed"],
            blocked_origins=set(),
        )
        (temporary / "fixtures/seed.sqlite3").unlink(missing_ok=True)
        _write_seed(temporary / "fixtures/seed.sqlite3", captured_routes, profile=product_profile)
        task_count = _write_tasks(temporary / "tasks.jsonl", captured_routes, profile=product_profile)
        spec = _replay_spec(
            site=site,
            seed_url=requested_seed_url,
            routes=captured_routes,
            reference=reference,
            profile=product_profile,
        )
        atomic_json(temporary / "replay-spec.json", spec)
        contract = compile_synthetic_replay_contract(
            temporary / "replay-spec.json", temporary / "synthetic_replay_contract.json"
        )
        acquisition = {
            "schema": REPLAY_ACQUISITION_SCHEMA,
            "status": "completed",
            "site": site,
            "seed_url": requested_seed_url,
            "source_origin": source_origin,
            "routes": len(captured_routes),
            "actions": sum(len(route["actions"]) for route in captured_routes),
            "visible_actions_observed": sum(route["visible_action_count"] for route in captured_routes),
            "occluded_actions_excluded": sum(route["occluded_action_count"] for route in captured_routes),
            "assets": len(asset_records),
            "asset_bytes": sum(int(record.get("bytes") or 0) for record in asset_records),
            "tasks": task_count,
            "skipped_discovered_routes": 0,
            "product_profile": product_profile,
            "coverage": spec["coverage"],
            "auth_used": True,
            "source_auth_persisted": False,
            "private_fields_removed": privacy["private_fields_removed"],
            "capture_bundle_sha256": contract["capture_manifest"]["bundle_sha256"],
            "contract_sha256": sha256_file(temporary / "synthetic_replay_contract.json"),
            "contract_path": "synthetic_replay_contract.json",
        }
        atomic_json(temporary / "acquisition.json", acquisition)
        os.replace(temporary, output_dir)
        return {**acquisition, "output_dir": str(output_dir), "contract_path": str(output_dir / "synthetic_replay_contract.json")}
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
