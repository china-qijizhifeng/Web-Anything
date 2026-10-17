#!/usr/bin/env python3
"""Bounded, robots-aware capture of public upstream pages and static assets."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit, urlunsplit

import requests

from robots_policy import RobotsPolicy
from page_content_policy import content_error_reason


TOOL_VERSION = "webharbor-upstream-capture-v15"
DEFAULT_USER_AGENT = "WebHarborCapture/1.0 (deterministic offline benchmark)"
PAGE_REQUEST_TIMEOUT = (10.0, 45.0)
ASSET_REQUEST_TIMEOUT = (10.0, 12.0)
LAZY_ASSET_URL_ATTRS = (
    "data-src", "data-lazy-src", "data-original", "data-bg", "data-background",
    "data-background-image",
)
ASSET_SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset", "imagesrcset")
SENSITIVE_QUERY_MARKERS = {
    "account", "address", "auth", "card", "credential", "csrf", "email", "key",
    "login", "mail", "name", "oauth", "password", "payment", "phone", "profile",
    "secret", "session", "signin", "signup", "ssn", "token", "upload", "user",
}
BLOCKED_REDIRECT_PATH_PARTS = {
    "account", "admin", "auth", "basket", "cart", "checkout", "dashboard", "login",
    "logout", "oauth", "password", "payment", "profile", "register", "session", "signin",
    "signup", "submit", "upload", "user",
}
BLOCKED_PRERENDER_PATH_PARTS = {
    "_bm", "_sec", "akam", "captcha", "challenge", "interstitial"
}
REDIRECT_DOCUMENT_EXTENSIONS = {
    ".7z", ".csv", ".doc", ".docx", ".gz", ".json", ".pdf", ".ppt", ".pptx",
    ".rar", ".tar", ".xls", ".xlsx", ".xml", ".zip",
}
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?(['\"])(.*?)\1\s*\)?",
    re.IGNORECASE,
)
SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_ASSET_PATH_SEGMENT_BYTES = 120
MAX_PRERENDER_RESOURCE_RECORDS = 2048
RESOURCE_TYPE_PRIORITIES = {
    "stylesheet": 0,
    "script": 1,
    "fetch": 1,
    "xhr": 1,
    "font": 2,
    "image": 3,
    "media": 3,
}
DEFAULT_ASSET_PRIORITY = 3
LAZY_ASSET_PRIORITY = 4
ASSET_SUFFIX_PRIORITIES = {
    ".css": 0,
    ".js": 1,
    ".mjs": 1,
    ".eot": 2,
    ".otf": 2,
    ".ttf": 2,
    ".woff": 2,
    ".woff2": 2,
}
BLOCK_PAGE_MARKERS = (
    "attention required! | cloudflare",
    "cf-chl-",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "press and hold to confirm you are a human",
    "request rate threshold exceeded",
    "verify you are human",
)


class CaptureError(RuntimeError):
    pass


def resource_type_priority(resource_type: str) -> int:
    return RESOURCE_TYPE_PRIORITIES.get(
        resource_type.casefold(), DEFAULT_ASSET_PRIORITY
    )


def content_type_priority(content_type: str) -> int:
    media_type = content_type.partition(";")[0].strip().casefold()
    if media_type == "text/css":
        return resource_type_priority("stylesheet")
    if "javascript" in media_type or media_type.endswith("+javascript"):
        return resource_type_priority("script")
    if (
        media_type.startswith("font/")
        or "font" in media_type
        or media_type in {"application/vnd.ms-fontobject"}
    ):
        return resource_type_priority("font")
    return DEFAULT_ASSET_PRIORITY


def url_priority(url: str) -> int:
    suffix = Path(urlsplit(url).path).suffix.casefold()
    return ASSET_SUFFIX_PRIORITIES.get(suffix, DEFAULT_ASSET_PRIORITY)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def successful_http_status(status: int) -> bool:
    return 200 <= status < 300


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_url(base: str, candidate: str) -> str | None:
    candidate = candidate.strip()
    if not candidate or candidate.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
        return None
    absolute, _ = urldefrag(urljoin(base, candidate))
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    hostname = parsed.hostname.lower()
    port = parsed.port
    netloc = hostname
    if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def organizational_host(host: str) -> str:
    """Return a conservative organizational suffix for robots-only redirects."""
    labels = host.casefold().strip(".").removeprefix("www.").split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return ""
    common_second_level_suffixes = {"ac", "co", "com", "edu", "gov", "net", "org"}
    suffix_size = (
        3
        if len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in common_second_level_suffixes
        else 2
    )
    return ".".join(labels[-suffix_size:])


def robots_redirect_allowed(requested_url: str, candidate_url: str) -> bool:
    requested = urlsplit(requested_url)
    candidate = urlsplit(candidate_url)
    if candidate.scheme not in {requested.scheme, "https"}:
        return False
    left = requested.hostname or ""
    right = candidate.hostname or ""
    try:
        ipaddress.ip_address(left)
        return left.casefold() == right.casefold()
    except ValueError:
        pass
    organization = organizational_host(left)
    return bool(
        left
        and right
        and organization
        and organization == organizational_host(right)
    )


def assert_public_host(host: str) -> None:
    if host in {"localhost", "127.0.0.1"}:
        return
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise CaptureError(f"capture hostname did not resolve safely: {host}") from exc
    blocked = sorted(str(address) for address in addresses if not address.is_global)
    if blocked:
        raise CaptureError(f"capture host resolves to non-public address(es): {', '.join(blocked)}")


def block_page_reason(body: bytes) -> str | None:
    sample = body[:200_000].decode("utf-8", errors="ignore").casefold()
    for marker in BLOCK_PAGE_MARKERS:
        if marker in sample:
            return marker
    return content_error_reason(sample)


def response_challenge_reason(response: requests.Response) -> str | None:
    action = response.headers.get("x-amzn-waf-action", "").casefold()
    if action:
        return f"access challenge header x-amzn-waf-action={action}"
    return None


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page_links: list[str] = []
        self.asset_links: list[str] = []
        self._asset_priorities: list[int] = []
        self.in_style = False

    def _add_asset(self, value: str, priority: int) -> None:
        self.asset_links.append(value)
        self._asset_priorities.append(priority)

    def _collect_css_urls(self, value: str) -> None:
        for _, raw in CSS_URL_RE.findall(value):
            self._add_asset(raw, DEFAULT_ASSET_PRIORITY)

    def prioritized_asset_links(self) -> list[str]:
        """Return render-critical resources before lazy/responsive alternatives."""
        return [value for value, _ in self.prioritized_asset_candidates()]

    def prioritized_asset_candidates(self) -> list[tuple[str, int]]:
        indexed = enumerate(zip(self.asset_links, self._asset_priorities, strict=True))
        return [
            candidate
            for _, candidate in sorted(
                indexed, key=lambda item: (item[1][1], item[0])
            )
        ]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag == "style":
            self.in_style = True
        if values.get("style"):
            self._collect_css_urls(values["style"])
        if tag == "a" and values.get("href"):
            self.page_links.append(values["href"])
        if tag in {"img", "source", "video", "audio"} and values.get("src"):
            self._add_asset(values["src"], DEFAULT_ASSET_PRIORITY)
        if tag == "script" and values.get("src"):
            self._add_asset(values["src"], 1)
        if tag == "use":
            reference = values.get("href") or values.get("xlink:href")
            if reference and not reference.startswith("#"):
                self._add_asset(reference, DEFAULT_ASSET_PRIORITY)
        for attr in LAZY_ASSET_URL_ATTRS:
            if values.get(attr):
                self._add_asset(values[attr], LAZY_ASSET_PRIORITY)
        for attr in ASSET_SRCSET_ATTRS:
            if not values.get(attr):
                continue
            for entry in values[attr].split(","):
                parts = entry.strip().split()
                if parts:
                    self._add_asset(parts[0], LAZY_ASSET_PRIORITY)
        if tag == "link" and values.get("href"):
            rel = {part.lower() for part in values.get("rel", "").split()}
            if "stylesheet" in rel:
                self._add_asset(values["href"], 0)
            elif rel & {"preload", "modulepreload"}:
                self._add_asset(values["href"], 1)
            elif rel & {"icon", "apple-touch-icon"}:
                self._add_asset(values["href"], DEFAULT_ASSET_PRIORITY)
        if tag == "meta" and values.get("content"):
            marker = (values.get("property") or values.get("name") or "").lower()
            if marker in {"og:image", "twitter:image"}:
                self._add_asset(values["content"], DEFAULT_ASSET_PRIORITY)

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self._collect_css_urls(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self.in_style = False


@dataclass(frozen=True)
class CaptureSpec:
    site: str
    upstream_url: str
    start_urls: tuple[str, ...]
    page_hosts: frozenset[str]
    asset_hosts: frozenset[str]
    page_patterns: tuple[re.Pattern[str], ...]
    asset_patterns: tuple[re.Pattern[str], ...]
    max_pages: int
    max_assets: int
    max_depth: int
    max_page_bytes: int
    max_asset_bytes: int
    delay_seconds: float
    user_agent: str
    authorization_basis: str
    authorization_notes: str
    output_dir: str
    generated_get_queries: tuple[dict[str, Any], ...]
    prerender_resources: tuple[dict[str, str], ...]

    @classmethod
    def load(cls, path: Path) -> "CaptureSpec":
        raw = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "site", "upstream_url", "start_urls", "page_hosts", "asset_hosts",
            "page_allow_patterns", "authorization_basis", "authorization_notes",
        }
        missing = sorted(required - raw.keys())
        if missing:
            raise CaptureError(f"capture spec missing: {', '.join(missing)}")
        site = raw["site"]
        if not isinstance(site, str) or not re.fullmatch(r"[a-z0-9_]+", site):
            raise CaptureError("site must contain lowercase letters, digits, or underscores")
        upstream = normalize_url(raw["upstream_url"], raw["upstream_url"])
        if upstream is None:
            raise CaptureError("upstream_url must be HTTP(S)")
        parsed = urlsplit(upstream)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise CaptureError("upstream_url must use HTTPS")
        page_hosts = frozenset(str(value).lower() for value in raw["page_hosts"])
        asset_hosts = frozenset(str(value).lower() for value in raw["asset_hosts"])
        if parsed.hostname not in page_hosts:
            raise CaptureError("upstream host must be listed in page_hosts")
        start_urls = tuple(normalize_url(upstream, value) for value in raw["start_urls"])
        if not start_urls or any(value is None for value in start_urls):
            raise CaptureError("start_urls must contain at least one valid URL")
        patterns = tuple(re.compile(value) for value in raw["page_allow_patterns"])
        if not patterns:
            raise CaptureError("page_allow_patterns must not be empty")
        asset_patterns = tuple(
            re.compile(value) for value in raw.get("asset_allow_patterns", [r".*"])
        )
        if not asset_patterns:
            raise CaptureError("asset_allow_patterns must not be empty")
        basis = str(raw["authorization_basis"]).strip()
        notes = str(raw["authorization_notes"]).strip()
        if not basis or not notes:
            raise CaptureError("authorization basis and notes must be explicit")
        generated_get_queries = raw.get("generated_get_queries", [])
        if not isinstance(generated_get_queries, list):
            raise CaptureError("generated_get_queries must be a list")
        validated_queries: list[dict[str, Any]] = []
        for index, record in enumerate(generated_get_queries):
            if not isinstance(record, dict) or record.get("method") != "get":
                raise CaptureError(f"generated_get_queries[{index}] must be a GET record")
            kind = str(record.get("kind", "search"))
            if kind not in {"search", "filter"}:
                raise CaptureError(f"generated_get_queries[{index}] has an invalid kind")
            query_url = normalize_url(upstream, str(record.get("url", "")))
            source_page = normalize_url(upstream, str(record.get("source_page", "")))
            action = normalize_url(upstream, str(record.get("action", "")))
            if query_url not in start_urls:
                raise CaptureError(f"generated_get_queries[{index}] URL must be a start URL")
            if (
                query_url is None
                or source_page is None
                or action is None
                or urlsplit(query_url).hostname not in page_hosts
                or urlsplit(source_page).hostname not in page_hosts
                or urlsplit(action).hostname not in page_hosts
                or not any(pattern.search(query_url) for pattern in patterns)
            ):
                raise CaptureError(f"generated_get_queries[{index}] leaves page scope")
            fields = record.get("fields")
            if not isinstance(fields, list) or not 1 <= len(fields) <= 5:
                raise CaptureError(f"generated_get_queries[{index}] must contain 1-5 fields")
            validated_fields: list[dict[str, str]] = []
            variable_roles = 0
            for field_index, field in enumerate(fields):
                if not isinstance(field, dict):
                    raise CaptureError(f"generated_get_queries[{index}] field is invalid")
                name = str(field.get("name", ""))
                value = str(field.get("value", ""))
                default_role = "query" if kind == "search" and len(fields) == 1 else ""
                role = str(field.get("role") or default_role)
                markers = re.split(r"[^a-z0-9]+", name.casefold())
                if (
                    role not in {"fixed", "query", "filter"}
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name)
                    or any(marker in SENSITIVE_QUERY_MARKERS for marker in markers if marker)
                    or len(value) > 80
                    or (role != "fixed" and not value)
                    or "@" in value
                    or "://" in value
                ):
                    raise CaptureError(f"generated_get_queries[{index}] field is unsafe")
                variable_roles += role in {"query", "filter"}
                validated_field = {
                    "name": name,
                    "value": value,
                    "source": str(field.get("source", "captured_page"))[:80],
                    "role": role,
                }
                if field.get("label"):
                    validated_field["label"] = str(field["label"])[:100]
                validated_fields.append(validated_field)
            expected_role = "query" if kind == "search" else "filter"
            if (
                variable_roles != 1
                or sum(field["role"] == expected_role for field in validated_fields) != 1
            ):
                raise CaptureError(
                    f"generated_get_queries[{index}] must contain one {expected_role} role"
                )
            if parse_qsl(urlsplit(query_url).query, keep_blank_values=True) != [
                (field["name"], field["value"]) for field in validated_fields
            ]:
                raise CaptureError(f"generated_get_queries[{index}] fields do not match URL")
            validated_queries.append({
                "url": query_url,
                "source_page": source_page,
                "form_index": int(record.get("form_index", index)),
                "action": action,
                "method": "get",
                "kind": kind,
                "fields": validated_fields,
            })
        prerender_resources = raw.get("prerender_resources", [])
        if (
            not isinstance(prerender_resources, list)
            or len(prerender_resources) > MAX_PRERENDER_RESOURCE_RECORDS
        ):
            raise CaptureError(
                "prerender_resources must be a list of at most "
                f"{MAX_PRERENDER_RESOURCE_RECORDS} records"
            )
        validated_prerender_resources: list[dict[str, str]] = []
        seen_prerender_urls: set[str] = set()
        allowed_resource_types = {
            "fetch", "font", "image", "media", "script", "stylesheet", "xhr"
        }
        for index, record in enumerate(prerender_resources):
            if not isinstance(record, dict) or record.get("method") != "GET":
                raise CaptureError(f"prerender_resources[{index}] must be a GET record")
            resource_type = str(record.get("resource_type", ""))
            resource_url = normalize_url(upstream, str(record.get("url", "")))
            if resource_url is not None:
                resource_url = canonical_prerender_resource_url(resource_url)
            source_page = normalize_url(upstream, str(record.get("source_page", "")))
            if (
                resource_type not in allowed_resource_types
                or resource_url is None
                or source_page is None
                or not prerender_resource_allowed(
                    resource_url,
                    asset_hosts=asset_hosts,
                    asset_patterns=asset_patterns,
                )
                or urlsplit(source_page).hostname not in page_hosts
            ):
                raise CaptureError(f"prerender_resources[{index}] leaves safe asset scope")
            if resource_url in seen_prerender_urls:
                continue
            seen_prerender_urls.add(resource_url)
            validated_prerender_resources.append({
                "url": resource_url,
                "source_page": source_page,
                "method": "GET",
                "resource_type": resource_type,
                "source": "blocked_captured_dom_request",
            })
        return cls(
            site=site,
            upstream_url=upstream,
            start_urls=tuple(value for value in start_urls if value is not None),
            page_hosts=page_hosts,
            asset_hosts=asset_hosts,
            page_patterns=patterns,
            asset_patterns=asset_patterns,
            max_pages=int(raw.get("max_pages", 20)),
            max_assets=int(raw.get("max_assets", 200)),
            max_depth=int(raw.get("max_depth", 1)),
            max_page_bytes=int(raw.get("max_page_bytes", 5_000_000)),
            max_asset_bytes=int(raw.get("max_asset_bytes", 8_000_000)),
            delay_seconds=float(raw.get("delay_seconds", 1.0)),
            user_agent=str(raw.get("user_agent", DEFAULT_USER_AGENT)),
            authorization_basis=basis,
            authorization_notes=notes,
            output_dir=str(raw.get("output_dir", f"sites/{site}/static/external_cache/upstream")),
            generated_get_queries=tuple(validated_queries),
            prerender_resources=tuple(validated_prerender_resources),
        )

    def page_allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        return parsed.hostname in self.page_hosts and any(pattern.search(url) for pattern in self.page_patterns)

    def page_redirect_allowed(self, requested_url: str, final_url: str) -> bool:
        if not self.page_allowed(requested_url):
            return False
        parsed = urlsplit(final_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in self.page_hosts:
            return False
        if Path(parsed.path).suffix.casefold() in REDIRECT_DOCUMENT_EXTENSIONS:
            return False
        segments = {segment.casefold() for segment in parsed.path.split("/") if segment}
        if segments & BLOCKED_REDIRECT_PATH_PARTS:
            return False
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            markers = re.split(r"[^a-z0-9]+", key.casefold())
            if (
                any(marker in SENSITIVE_QUERY_MARKERS for marker in markers if marker)
                or len(key) > 80
                or len(value) > 200
                or "@" in value
            ):
                return False
        return True

    def asset_allowed(self, url: str) -> bool:
        return (
            urlsplit(url).hostname in self.asset_hosts
            and any(pattern.search(url) for pattern in self.asset_patterns)
        )


def prerender_resource_allowed(
    url: str,
    *,
    asset_hosts: frozenset[str],
    asset_patterns: tuple[re.Pattern[str], ...],
) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in asset_hosts:
        return False
    if not any(pattern.search(url) for pattern in asset_patterns):
        return False
    path_segments = {segment.casefold() for segment in parsed.path.split("/") if segment}
    if path_segments & (BLOCKED_REDIRECT_PATH_PARTS | BLOCKED_PRERENDER_PATH_PARTS):
        return False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        markers = re.split(r"[^a-z0-9]+", key.casefold())
        if (
            any(marker in SENSITIVE_QUERY_MARKERS for marker in markers if marker)
            or len(key) > 80
            or len(value) > 200
            or "@" in value
        ):
            return False
    return True


def canonical_prerender_resource_url(url: str) -> str:
    """Collapse volatile numeric cache busters for immutable JS/CSS chunk URLs."""
    parsed = urlsplit(url)
    if Path(parsed.path).suffix.casefold() not in {".js", ".css"} or not parsed.query:
        return url
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    numeric_flag = len(pairs) == 1 and pairs[0][0].isdigit() and not pairs[0][1]
    named_flag = bool(
        len(pairs) == 1
        and pairs[0][0].casefold() in {"_", "cb", "cachebust", "t", "timestamp", "v"}
        and pairs[0][1].isdigit()
    )
    if not (numeric_flag or named_flag):
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def response_body(response: requests.Response, limit: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared and int(declared) > limit:
        raise CaptureError(f"response exceeds byte limit: {declared} > {limit}")
    body = response.content
    if len(body) > limit:
        raise CaptureError(f"response exceeds byte limit: {len(body)} > {limit}")
    return body


def bounded_asset_segment(value: str, *, digest_source: str | None = None) -> str:
    """Keep one portable path component readable and below filesystem limits."""
    safe = SAFE_SEGMENT_RE.sub("_", value) or "asset"
    if len(safe.encode("ascii")) <= MAX_ASSET_PATH_SEGMENT_BYTES:
        return safe
    suffix = Path(safe).suffix
    if len(suffix) > 24:
        suffix = ""
    stem = safe[:-len(suffix)] if suffix else safe
    marker = "__h_" + hashlib.sha256(
        (digest_source if digest_source is not None else value).encode("utf-8")
    ).hexdigest()[:16]
    prefix_bytes = MAX_ASSET_PATH_SEGMENT_BYTES - len(marker) - len(suffix)
    prefix = stem[:prefix_bytes].rstrip("._-") or "asset"
    return f"{prefix}{marker}{suffix}"


def safe_asset_path(url: str, content_type: str) -> Path:
    parsed = urlsplit(url)
    raw_segments = [
        part for part in parsed.path.split("/") if part not in {"", ".", ".."}
    ]
    segments = [
        bounded_asset_segment(part, digest_source=part) for part in raw_segments
    ]
    if not segments or parsed.path.endswith("/"):
        segments.append("index")
    name = segments[-1]
    if "." not in name:
        extensions = {
            "text/css": ".css", "image/svg+xml": ".svg", "image/webp": ".webp",
            "image/png": ".png", "image/jpeg": ".jpg", "image/x-icon": ".ico",
            "font/woff2": ".woff2", "font/woff": ".woff",
        }
        name += extensions.get(content_type.split(";", 1)[0].lower(), ".bin")
    if parsed.query:
        stem, dot, suffix = name.rpartition(".")
        marker = hashlib.sha256(parsed.query.encode()).hexdigest()[:10]
        name = f"{stem or name}__q_{marker}{dot}{suffix}" if dot else f"{name}__q_{marker}"
    segments[-1] = bounded_asset_segment(name, digest_source=name)
    hostname = bounded_asset_segment(parsed.hostname or "unknown")
    return Path("assets") / hostname / Path(*segments)


class CaptureRunner:
    def __init__(
        self,
        spec: CaptureSpec,
        output_dir: Path,
        reuse_evidence: dict[str, Any] | None = None,
    ) -> None:
        self.spec = spec
        self.output_dir = output_dir
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": spec.user_agent, "Accept-Encoding": "gzip, deflate"})
        self.last_request_at = 0.0
        self.robots: dict[str, RobotsPolicy] = {}
        self.robots_records: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.assets: list[dict[str, Any]] = []
        self.failures: list[dict[str, str]] = []
        self.asset_failures: list[dict[str, str]] = []
        self.optional_page_failures: list[dict[str, str]] = []
        self.optional_query_failures: list[dict[str, str]] = []
        self.asset_priority_hints: dict[str, int] = {
            record["url"]: resource_type_priority(record["resource_type"])
            for record in self.spec.prerender_resources
        }
        self.generated_query_urls = {
            record["url"] for record in self.spec.generated_get_queries
        }
        first_start_host = urlsplit(self.spec.start_urls[0]).hostname or ""
        upstream_host = urlsplit(self.spec.upstream_url).hostname or ""
        self.core_entry_hosts = {first_start_host, upstream_host}
        self.optional_query_hosts = {
            urlsplit(url).hostname or ""
            for url in self.generated_query_urls
        } - self.core_entry_hosts
        self.unavailable_optional_hosts: set[str] = set()
        reuse_evidence = reuse_evidence or {}
        self.reuse_pages = {
            record.get("url"): record
            for record in reuse_evidence.get("pages", [])
            if isinstance(record, dict) and record.get("url")
        }
        self.reuse_assets = {
            record.get("url"): record
            for record in reuse_evidence.get("assets", [])
            if isinstance(record, dict) and record.get("url")
        }
        for url, record in self.reuse_assets.items():
            priority = content_type_priority(str(record.get("content_type", "")))
            self.asset_priority_hints.setdefault(url, priority)

    def collector_assets(self, collector: LinkCollector, final_url: str) -> list[str]:
        assets: list[str] = []
        for raw, priority in collector.prioritized_asset_candidates():
            value = normalize_url(final_url, raw)
            if value is None:
                continue
            assets.append(value)
            self.asset_priority_hints[value] = min(
                priority, self.asset_priority_hints.get(value, priority)
            )
        return assets

    def prioritized_asset_queue(self, values: deque[str]) -> deque[str]:
        return deque(
            url
            for _, url in sorted(
                enumerate(values),
                key=lambda item: (
                    self.asset_priority_hints.get(
                        item[1], url_priority(item[1])
                    ),
                    item[0],
                ),
            )
        )

    def reuse_body(
        self,
        url: str,
        records: dict[str, dict[str, Any]],
        temp_root: Path,
        *,
        page: bool,
    ) -> tuple[bytes, dict[str, Any]] | None:
        record = records.get(url)
        if record is None or not self.output_dir.is_dir():
            return None
        final_url = record.get("final_url")
        if not isinstance(final_url, str):
            return None
        if page and not (
            self.spec.page_allowed(final_url)
            or self.spec.page_redirect_allowed(url, final_url)
        ):
            return None
        if not page and not self.spec.asset_allowed(final_url):
            return None
        local_path = record.get("local_path")
        if not isinstance(local_path, str):
            return None
        source = (self.output_dir / local_path).resolve()
        output_root = self.output_dir.resolve()
        if output_root not in source.parents or not source.is_file():
            return None
        body = source.read_bytes()
        if sha256_bytes(body) != record.get("sha256"):
            return None
        destination = temp_root / local_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        reused = dict(record)
        reused["reused"] = True
        return body, reused

    def get(
        self,
        url: str,
        *,
        timeout: tuple[float, float] = PAGE_REQUEST_TIMEOUT,
        allow_robots_canonical_redirects: bool = False,
    ) -> requests.Response:
        current = url
        allowed_hosts = self.spec.page_hosts | self.spec.asset_hosts
        canonical_redirect_hosts: set[str] = set()
        for _ in range(6):
            host = urlsplit(current).hostname or ""
            if host not in allowed_hosts and host not in canonical_redirect_hosts:
                raise CaptureError(f"request host is outside declared scope: {host}")
            assert_public_host(host)
            remaining = self.spec.delay_seconds - (time.monotonic() - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
            try:
                response = self.session.get(current, timeout=timeout, allow_redirects=False)
            except requests.RequestException as exc:
                raise CaptureError(str(exc)) from exc
            self.last_request_at = time.monotonic()
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            candidate = normalize_url(current, location or "")
            candidate_host = urlsplit(candidate).hostname or "" if candidate else ""
            if candidate is None or (
                candidate_host not in allowed_hosts
                and not (
                    allow_robots_canonical_redirects
                    and robots_redirect_allowed(url, candidate)
                )
            ):
                raise CaptureError(f"redirect left declared scope: {current} -> {location!r}")
            if candidate_host not in allowed_hosts:
                canonical_redirect_hosts.add(candidate_host)
            current = candidate
        raise CaptureError(f"too many redirects while requesting {url}")

    def load_robots(self) -> None:
        hosts = sorted(self.spec.page_hosts | self.spec.asset_hosts)
        upstream_scheme = urlsplit(self.spec.upstream_url).scheme
        for host in hosts:
            scheme = upstream_scheme if host in self.spec.page_hosts else "https"
            robots_url = f"{scheme}://{host}/robots.txt"
            try:
                response = self.get(
                    robots_url,
                    allow_robots_canonical_redirects=True,
                )
                body = response_body(response, 1_000_000)
                challenge = response_challenge_reason(response)
                if challenge:
                    raise CaptureError(f"robots response is blocked: {challenge}")
                robots_unavailable = response.status_code in {404, 410}
                if not successful_http_status(response.status_code) and not robots_unavailable:
                    raise CaptureError(f"robots request failed: {robots_url} -> {response.status_code}")
                parser = RobotsPolicy(robots_url)
                parser.parse([] if robots_unavailable else response.text.splitlines())
                self.robots[host] = parser
                self.robots_records.append({
                    "url": robots_url,
                    "final_url": normalize_url(
                        robots_url, getattr(response, "url", robots_url)
                    ) or robots_url,
                    "status": response.status_code,
                    "bytes": len(body),
                    "sha256": sha256_bytes(body),
                    "policy": (
                        "unavailable_allow_all_rfc9309" if robots_unavailable else "parsed"
                    ),
                })
            except CaptureError as exc:
                if host in self.core_entry_hosts:
                    raise
                self.unavailable_optional_hosts.add(host)
                if host in self.optional_query_hosts:
                    target = self.optional_query_failures
                elif host in self.spec.page_hosts:
                    target = self.optional_page_failures
                else:
                    target = self.asset_failures
                target.append({
                    "kind": "robots",
                    "url": robots_url,
                    "error": str(exc),
                })

    def allowed_by_robots(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        parser = self.robots.get(host)
        return bool(parser and parser.can_fetch(self.spec.user_agent, url))

    def capture_page(self, url: str, temp_root: Path) -> tuple[list[str], list[str]]:
        if not self.allowed_by_robots(url):
            raise CaptureError(f"robots.txt disallows {url}")
        cached = self.reuse_body(url, self.reuse_pages, temp_root, page=True)
        if cached is not None:
            body, record = cached
            self.pages.append(record)
            collector = LinkCollector()
            collector.feed(body.decode("utf-8", errors="replace"))
            final_url = record["final_url"]
            pages = [value for raw in collector.page_links if (value := normalize_url(final_url, raw))]
            assets = self.collector_assets(collector, final_url)
            return pages, assets
        response = self.get(url)
        challenge = response_challenge_reason(response)
        if challenge:
            raise CaptureError(f"page is an access challenge, not source content: {challenge}")
        final_url = normalize_url(url, response.url)
        if final_url is None or not (
            self.spec.page_allowed(final_url)
            or self.spec.page_redirect_allowed(url, final_url)
        ):
            raise CaptureError(f"page redirected out of scope: {url} -> {response.url}")
        if response.status_code != 200:
            raise CaptureError(f"page returned HTTP {response.status_code}")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise CaptureError(f"page returned unsupported content type {media_type!r}")
        body = response_body(response, self.spec.max_page_bytes)
        marker = block_page_reason(body)
        if marker:
            raise CaptureError(f"page is an access challenge, not source content: {marker}")
        local_path = Path("pages") / f"{hashlib.sha256(url.encode()).hexdigest()[:20]}.html"
        destination = temp_root / local_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        collector = LinkCollector()
        collector.feed(response.text)
        self.pages.append({
            "url": url, "final_url": final_url, "status": response.status_code,
            "content_type": media_type, "bytes": len(body), "sha256": sha256_bytes(body),
            "local_path": local_path.as_posix(),
            "etag": response.headers.get("etag"), "last_modified": response.headers.get("last-modified"),
        })
        pages = [value for raw in collector.page_links if (value := normalize_url(final_url, raw))]
        assets = self.collector_assets(collector, final_url)
        return pages, assets

    def capture_asset(self, url: str, temp_root: Path) -> list[str]:
        if not self.allowed_by_robots(url):
            raise CaptureError(f"robots.txt disallows asset {url}")
        cached = self.reuse_body(url, self.reuse_assets, temp_root, page=False)
        if cached is not None:
            body, record = cached
            self.assets.append(record)
            if record.get("content_type") == "text/css":
                text = body.decode("utf-8", errors="replace")
                raw_urls = [raw for _, raw in CSS_URL_RE.findall(text)]
                raw_urls.extend(raw for _, raw in CSS_IMPORT_RE.findall(text))
                return [
                    value for raw in raw_urls
                    if (value := normalize_url(record["final_url"], raw))
                ]
            return []
        response = self.get(url, timeout=ASSET_REQUEST_TIMEOUT)
        challenge = response_challenge_reason(response)
        if challenge:
            raise CaptureError(f"asset is an access challenge, not source content: {challenge}")
        final_url = normalize_url(url, response.url)
        if final_url is None or not self.spec.asset_allowed(final_url):
            raise CaptureError(f"asset redirected out of scope: {url} -> {response.url}")
        if response.status_code != 200:
            raise CaptureError(f"asset returned HTTP {response.status_code}")
        body = response_body(response, self.spec.max_asset_bytes)
        media_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
        if media_type in {"text/html", "application/xhtml+xml"}:
            marker = block_page_reason(body)
            if marker:
                raise CaptureError(f"asset is an access challenge, not source content: {marker}")
        local_path = safe_asset_path(final_url, media_type)
        destination = temp_root / local_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        self.assets.append({
            "url": url, "final_url": final_url, "status": response.status_code,
            "content_type": media_type, "bytes": len(body), "sha256": sha256_bytes(body),
            "local_path": local_path.as_posix(),
        })
        if media_type == "text/css":
            raw_urls = [raw for _, raw in CSS_URL_RE.findall(response.text)]
            raw_urls.extend(raw for _, raw in CSS_IMPORT_RE.findall(response.text))
            return [
                value for raw in raw_urls
                if (value := normalize_url(final_url, raw))
            ]
        return []

    def run(self) -> dict[str, Any]:
        self.load_robots()
        temp_root = Path(tempfile.mkdtemp(prefix=f"webharbor-capture-{self.spec.site}-", dir=self.output_dir.parent))
        page_queue = deque(
            (url, 0, index == 0)
            for index, url in enumerate(self.spec.start_urls)
            if (urlsplit(url).hostname or "") not in self.unavailable_optional_hosts
        )
        asset_queue: deque[str] = deque(
            [record["url"] for record in self.spec.prerender_resources]
            + list(self.reuse_assets)
        )
        seen_pages: set[str] = set()
        seen_assets: set[str] = set()
        try:
            while page_queue and len(self.pages) < self.spec.max_pages:
                url, depth, required = page_queue.popleft()
                if url in seen_pages:
                    continue
                seen_pages.add(url)
                if not self.spec.page_allowed(url):
                    if required:
                        raise CaptureError(f"start URL is outside page scope: {url}")
                    continue
                try:
                    discovered_pages, discovered_assets = self.capture_page(url, temp_root)
                except CaptureError as exc:
                    if required:
                        self.failures.append({"kind": "page", "url": url, "error": str(exc)})
                        raise CaptureError(f"{url}: {exc}") from exc
                    target = (
                        self.optional_query_failures
                        if url in self.generated_query_urls
                        else self.optional_page_failures
                    )
                    target.append({"kind": "page", "url": url, "error": str(exc)})
                    continue
                asset_queue.extend(discovered_assets)
                if depth < self.spec.max_depth:
                    for candidate in discovered_pages:
                        if (
                            self.spec.page_allowed(candidate)
                            and candidate not in seen_pages
                            and (urlsplit(candidate).hostname or "")
                            not in self.unavailable_optional_hosts
                        ):
                            page_queue.append((candidate, depth + 1, False))
            asset_attempts = 0
            max_asset_attempts = self.spec.max_assets + min(64, self.spec.max_assets)
            asset_queue = self.prioritized_asset_queue(asset_queue)
            while (
                asset_queue
                and len(self.assets) < self.spec.max_assets
                and asset_attempts < max_asset_attempts
            ):
                url = asset_queue.popleft()
                if (
                    url in seen_assets
                    or not self.spec.asset_allowed(url)
                    or (urlsplit(url).hostname or "") in self.unavailable_optional_hosts
                ):
                    continue
                seen_assets.add(url)
                asset_attempts += 1
                try:
                    discovered = self.capture_asset(url, temp_root)
                    if self.reuse_assets:
                        # An incremental pass must not evict hash-verified baseline
                        # assets. The browser audit promotes newly exposed CSS
                        # dependencies into an explicitly budgeted next pass.
                        asset_queue.extend(discovered)
                    else:
                        for dependency in discovered:
                            self.asset_priority_hints.setdefault(
                                dependency, url_priority(dependency)
                            )
                        asset_queue.extend(discovered)
                        asset_queue = self.prioritized_asset_queue(asset_queue)
                except CaptureError as exc:
                    # Preserve the missing resource as a fidelity gap. Page capture,
                    # screenshots, and landmark checks decide whether it is material.
                    self.asset_failures.append({
                        "kind": "asset", "url": url, "error": str(exc)
                    })
            asset_queue_remaining = len({
                url for url in asset_queue
                if url not in seen_assets
                and self.spec.asset_allowed(url)
                and (urlsplit(url).hostname or "") not in self.unavailable_optional_hosts
            })
            if self.output_dir.exists():
                shutil.rmtree(self.output_dir)
            os.replace(temp_root, self.output_dir)
            stale_prefix = f"webharbor-capture-{self.spec.site}-"
            for candidate in self.output_dir.parent.iterdir():
                if (
                    candidate.name.startswith(stale_prefix)
                    and candidate.is_dir()
                    and not candidate.is_symlink()
                ):
                    shutil.rmtree(candidate)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        return {
            "schema_version": 1,
            "tool": TOOL_VERSION,
            "phase": "upstream_capture",
            "site": self.spec.site,
            "generated_at": utc_now(),
            "status": "passed",
            "authorization": {
                "basis": self.spec.authorization_basis,
                "notes": self.spec.authorization_notes,
            },
            "scope": {
                "upstream_url": self.spec.upstream_url,
                "start_urls": list(self.spec.start_urls),
                "page_hosts": sorted(self.spec.page_hosts),
                "asset_hosts": sorted(self.spec.asset_hosts),
                "page_allow_patterns": [pattern.pattern for pattern in self.spec.page_patterns],
                "asset_allow_patterns": [pattern.pattern for pattern in self.spec.asset_patterns],
                "max_pages": self.spec.max_pages,
                "max_assets": self.spec.max_assets,
                "max_depth": self.spec.max_depth,
                "delay_seconds": self.spec.delay_seconds,
                "method": "bounded public GET only",
                "generated_get_queries": list(self.spec.generated_get_queries),
                "prerender_resources": list(self.spec.prerender_resources),
            },
            "robots": self.robots_records,
            "pages": sorted(self.pages, key=lambda item: item["url"]),
            "assets": sorted(self.assets, key=lambda item: item["url"]),
            "asset_attempts": asset_attempts,
            "max_asset_attempts": max_asset_attempts,
            "asset_budget_exhausted": asset_queue_remaining > 0,
            "asset_queue_remaining": asset_queue_remaining,
            "failures": self.failures,
            "asset_failures": self.asset_failures,
            "optional_page_failures": self.optional_page_failures,
            "optional_query_failures": self.optional_query_failures,
            "page_redirects": [
                {"url": record["url"], "final_url": record["final_url"]}
                for record in sorted(self.pages, key=lambda item: item["url"])
                if record["url"] != record["final_url"]
            ],
        }


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def path_in_root(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise CaptureError(f"path escapes repository: {value}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    root = repository_root()
    spec_path = args.spec.resolve()
    spec = CaptureSpec.load(spec_path)
    output_dir = path_in_root(root, spec.output_dir)
    required_prefix = (root / "sites" / spec.site / "static" / "external_cache").resolve()
    if output_dir != required_prefix and required_prefix not in output_dir.parents:
        raise CaptureError(f"output_dir must stay under {required_prefix}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    evidence_path = args.evidence or root / "mirror_factory" / "evidence" / spec.site / "upstream.json"
    evidence_path = evidence_path.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    reuse_evidence = None
    if evidence_path.is_file() and output_dir.is_dir():
        try:
            candidate = json.loads(evidence_path.read_text(encoding="utf-8"))
            if candidate.get("site") == spec.site:
                reuse_evidence = candidate
        except (json.JSONDecodeError, OSError):
            pass
    runner = CaptureRunner(spec, output_dir, reuse_evidence=reuse_evidence)
    evidence = runner.run()
    evidence["spec"] = {
        "path": spec_path.relative_to(root).as_posix(),
        "sha256": sha256_file(spec_path),
    }
    manifest = root / "mirror_factory" / "targets.jsonl"
    evidence["manifest_sha256"] = sha256_file(manifest) if manifest.is_file() else None
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": evidence["status"], "site": spec.site,
        "pages": len(evidence["pages"]), "assets": len(evidence["assets"]),
        "failures": len(evidence["failures"]), "evidence": str(evidence_path),
    }, sort_keys=True))
    return 0 if evidence["status"] == "passed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CaptureError as exc:
        print(f"capture error: {exc}", file=sys.stderr)
        raise SystemExit(1)
