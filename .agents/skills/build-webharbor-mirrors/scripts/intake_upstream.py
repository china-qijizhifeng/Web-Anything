#!/usr/bin/env python3
"""Turn one public URL into a bounded, robots-aware upstream capture spec."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

import requests

from robots_policy import RobotsPolicy
from page_content_policy import content_error_reason


TOOL_VERSION = "webharbor-upstream-intake-v14"
USER_AGENT = "WebHarborCapture/1.0 (deterministic offline benchmark)"
MAX_PAGE_BYTES = 5_000_000
MAX_EXTERNAL_PRESENTATION_ASSET_HOSTS = 6
PRIVATE_RESEARCH_AUTHORIZATION = "user_directed_bounded_private_research_snapshot"
PRIVATE_RESEARCH_NOTES = (
    "The user explicitly requested a bounded local benchmark snapshot of this public, "
    "unauthenticated site. Capture remains read-only and robots-aware, excludes personal "
    "and authenticated data, is not automatically published, and does not assert a "
    "third-party license."
)
SITE_ID_RE = re.compile(r"[^a-z0-9]+")
BLOCKED_STATUS = {401, 403, 406, 407, 409, 423, 429, 451}
BLOCKED_PATH_PARTS = {
    "account", "admin", "apply", "auth", "basket", "cart", "checkout",
    "dashboard", "feedback", "login", "logout", "oauth", "password",
    "payment", "profile", "register", "session", "signin", "signup",
    "submit", "upload", "user",
}
DOCUMENT_EXTENSIONS = {
    ".7z", ".csv", ".doc", ".docx", ".gz", ".json", ".pdf", ".ppt",
    ".pptx", ".rar", ".rss", ".tar", ".txt", ".xls", ".xlsx", ".xml",
    ".zip",
}
ASSET_TAG_ATTRS = {
    "img": ("src", "srcset"),
    "script": ("src",),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
}
CHALLENGE_MARKERS = (
    "attention required! | cloudflare",
    "cf-chl-",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "perimeterx",
    "press and hold to confirm you are a human",
    "request rate threshold exceeded",
    "verify you are human",
    "/_sec/verify?provider=interstitial",
    "/interstitial/ic.html",
    "bm-verify=",
)
LICENSE_MARKERS = (
    "creative commons", "cc by", "open government licence", "public domain",
    "apache license", "mit license", "gnu general public license",
)
SENSITIVE_FORM_MARKERS = {
    "account", "address", "auth", "card", "checkout", "credential", "csrf",
    "email", "key", "login", "mail", "name", "oauth", "password", "payment",
    "phone", "profile", "secret", "session", "signin", "signup", "ssn", "token",
    "upload", "user",
}
SENSITIVE_INPUT_TYPES = {"email", "file", "password", "tel"}
SEARCH_FIELD_NAMES = {"q", "query", "search", "searchterm", "search_term", "term", "keyword", "keywords"}
GENERIC_QUERY_TERMS = {
    "about", "contact", "home", "learn more", "menu", "more", "next", "privacy",
    "read more", "search", "services", "sign in", "skip to content", "terms",
}


class IntakeError(RuntimeError):
    """A URL cannot safely advance to bounded capture."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def successful_http_status(status: int) -> bool:
    return 200 <= status < 300


def normalize_entry_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise IntakeError("upstream URL is empty")
    if "://" not in value:
        value = "https://" + value
    value, _ = urldefrag(value)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IntakeError("upstream URL must be HTTP(S) with a hostname")
    host = parsed.hostname.lower().rstrip(".")
    if parsed.username or parsed.password:
        raise IntakeError("credentials are not allowed in an upstream URL")
    if parsed.scheme != "https" and host not in {"127.0.0.1", "localhost"}:
        raise IntakeError("public upstream URLs must use HTTPS")
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def derive_site_id(url: str) -> str:
    parsed = urlsplit(normalize_entry_url(url))
    host = (parsed.hostname or "site").lower()
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    stem = "_".join(labels[:-1] or labels)
    stem = SITE_ID_RE.sub("_", stem).strip("_")
    return stem[:64] or "site"


def normalize_discovered_url(base: str, value: str) -> str | None:
    value = value.strip()
    if not value or value.startswith(("#", "data:", "javascript:", "mailto:", "tel:")):
        return None
    absolute, _ = urldefrag(urljoin(base, value))
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    netloc = host
    if parsed.port and not ((parsed.scheme == "https" and parsed.port == 443) or (parsed.scheme == "http" and parsed.port == 80)):
        netloc = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


@dataclass
class Link:
    href: str
    text: str
    region: str


class DiscoveryParser(HTMLParser):
    """Collect navigational candidates without executing page code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.links: list[Link] = []
        self.asset_urls: list[str] = []
        self.presentation_asset_urls: list[str] = []
        self.forms: list[dict[str, Any]] = []
        self.headings: list[str] = []
        self._regions: list[str] = []
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._heading_depth = 0
        self._heading_parts: list[str] = []
        self._anchor: dict[str, Any] | None = None
        self._form: dict[str, Any] | None = None
        self._select: dict[str, Any] | None = None
        self._option: dict[str, Any] | None = None
        self._label: dict[str, Any] | None = None

    @property
    def region(self) -> str:
        return self._regions[-1] if self._regions else "body"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        attribute_names = {key.lower() for key, _ in attrs}
        if tag in {"header", "nav", "main", "footer"}:
            self._regions.append(tag)
        if tag == "title" and not self.title and not self._title_depth:
            self._title_parts = []
            self._title_depth += 1
        if tag in {"h1", "h2", "h3"}:
            if not self._heading_depth:
                self._heading_parts = []
            self._heading_depth += 1
        if tag == "meta" and not self.description:
            marker = (values.get("name") or values.get("property") or "").lower()
            if marker in {"description", "og:description"}:
                self.description = values.get("content", "").strip()
        if tag == "a" and values.get("href"):
            self._anchor = {"href": values["href"], "parts": [], "region": self.region}
        if tag == "link" and values.get("href"):
            rel = {part.lower() for part in values.get("rel", "").split()}
            if rel & {"stylesheet", "icon", "preload", "apple-touch-icon"}:
                self.asset_urls.append(values["href"])
                if (
                    rel & {"stylesheet", "icon", "apple-touch-icon"}
                    or values.get("as", "").casefold() in {"font", "image", "style"}
                ):
                    self.presentation_asset_urls.append(values["href"])
        for attr in ASSET_TAG_ATTRS.get(tag, ()):
            if not values.get(attr):
                continue
            if attr == "srcset":
                discovered = [
                    entry.strip().split()[0]
                    for entry in values[attr].split(",")
                    if entry.strip()
                ]
                self.asset_urls.extend(discovered)
                if tag in {"img", "source", "video"}:
                    self.presentation_asset_urls.extend(discovered)
            else:
                self.asset_urls.append(values[attr])
                if tag in {"img", "source", "video"}:
                    self.presentation_asset_urls.append(values[attr])
        if tag == "form":
            self._form = {
                "action": values.get("action", ""),
                "method": values.get("method", "get").lower(),
                "region": self.region,
                "role": values.get("role", "").lower(),
                "fields": [],
            }
        elif tag == "input" and self._form is not None:
            field_type = values.get("type", "text").lower()
            value = values.get("value", "")
            if field_type in {"checkbox", "radio"} and "value" not in attribute_names:
                value = "on"
            field = {
                "name": values.get("name", ""),
                "type": field_type,
                "value": value,
                "placeholder": values.get("placeholder", ""),
                "aria_label": values.get("aria-label", ""),
                "autocomplete": values.get("autocomplete", "").lower(),
                "id": values.get("id", ""),
                "label": "",
                "checked": "checked" in attribute_names,
                "disabled": "disabled" in attribute_names,
            }
            self._form["fields"].append(field)
            if self._label is not None and not self._label["for"]:
                self._label["field"] = field
        elif tag == "select" and self._form is not None:
            field = {
                "name": values.get("name", ""),
                "type": "select",
                "value": "",
                "placeholder": "",
                "aria_label": values.get("aria-label", ""),
                "autocomplete": values.get("autocomplete", "").lower(),
                "options": [],
                "disabled": "disabled" in attribute_names,
            }
            self._form["fields"].append(field)
            self._select = field
        elif tag == "option" and self._select is not None:
            self._option = {
                "value": values.get("value", ""),
                "has_value": "value" in values,
                "selected": "selected" in values,
                "disabled": "disabled" in attribute_names,
                "parts": [],
            }
        elif tag == "label" and self._form is not None:
            self._label = {"for": values.get("for", ""), "parts": [], "field": None}

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._anchor is not None:
            self.links.append(Link(
                href=self._anchor["href"],
                text=" ".join("".join(self._anchor["parts"]).split()),
                region=self._anchor["region"],
            ))
            self._anchor = None
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
            self._select = None
            self._option = None
            self._label = None
        if tag == "label" and self._label is not None and self._form is not None:
            label = " ".join("".join(self._label["parts"]).split())
            field = self._label.get("field")
            if field is None and self._label["for"]:
                field = next(
                    (
                        candidate for candidate in self._form["fields"]
                        if candidate.get("id") == self._label["for"]
                    ),
                    None,
                )
            if field is not None:
                field["label"] = label
            self._label = None
        if tag == "option" and self._option is not None and self._select is not None:
            label = " ".join("".join(self._option["parts"]).split())
            value = self._option["value"] if self._option["has_value"] else label
            option = {
                "value": value,
                "label": label or value,
                "selected": self._option["selected"],
            }
            if self._option["disabled"]:
                option["disabled"] = True
            self._select["options"].append(option)
            if option["selected"]:
                self._select["value"] = value
            self._option = None
        if tag == "select" and self._select is not None:
            if not self._select["value"] and self._select["options"]:
                self._select["value"] = self._select["options"][0]["value"]
            self._select = None
            self._option = None
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
            if not self._title_depth:
                self.title = " ".join("".join(self._title_parts).split())
        if tag in {"h1", "h2", "h3"} and self._heading_depth:
            self._heading_depth -= 1
            if not self._heading_depth:
                heading = " ".join("".join(self._heading_parts).split())
                if heading:
                    self.headings.append(heading)
        if tag in {"header", "nav", "main", "footer"} and self._regions:
            for index in range(len(self._regions) - 1, -1, -1):
                if self._regions[index] == tag:
                    del self._regions[index:]
                    break

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._heading_depth:
            self._heading_parts.append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)
        if self._option is not None:
            self._option["parts"].append(data)
        if self._label is not None:
            self._label["parts"].append(data)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def same_site_host(first: str, second: str) -> bool:
    left = (urlsplit(first).hostname or "").casefold().removeprefix("www.")
    right = (urlsplit(second).hostname or "").casefold().removeprefix("www.")
    return bool(
        left
        and right
        and (
            left == right
            or left.endswith("." + right)
            or right.endswith("." + left)
        )
    )


def trusted_get_action_host(base: str, action: str) -> bool:
    if same_site_host(base, action):
        return True
    base_host = (urlsplit(base).hostname or "").casefold().removeprefix("www.")
    action_host = (urlsplit(action).hostname or "").casefold().removeprefix("www.")
    if not base_host.endswith((".gov", ".mil")):
        return False
    return bool(action_host and (action_host == base_host or action_host.endswith("." + base_host)))


def assert_public_destination(url: str) -> None:
    host = urlsplit(url).hostname or ""
    if host in {"localhost", "127.0.0.1"}:
        raise IntakeError("loopback URLs are reserved for injected test sessions")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise IntakeError(f"upstream hostname did not resolve safely: {host}") from exc
    blocked = sorted(str(address) for address in addresses if not address.is_global)
    if blocked:
        raise IntakeError(f"upstream resolves to non-public address(es): {', '.join(blocked)}")


def safe_get(
    client: requests.Session,
    url: str,
    *,
    injected_session: bool,
    max_redirects: int = 5,
) -> requests.Response:
    current = url
    for _ in range(max_redirects + 1):
        if not injected_session:
            assert_public_destination(current)
        response = client.get(current, timeout=(10, 45), allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            raise IntakeError(f"redirect response omitted Location: {current}")
        candidate = normalize_discovered_url(current, location)
        if candidate is None or not same_site_host(url, candidate):
            raise IntakeError(f"redirect left the requested site: {current} -> {location}")
        current = candidate
    raise IntakeError(f"too many redirects while requesting {url}")


def page_candidate_allowed(base: str, candidate: str) -> bool:
    if not same_site_host(base, candidate):
        return False
    parsed = urlsplit(candidate)
    if parsed.query:
        return False
    suffix = Path(parsed.path).suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        return False
    segments = {segment.casefold() for segment in parsed.path.split("/") if segment}
    return not bool(segments & BLOCKED_PATH_PARTS)


def select_start_urls(base: str, links: list[Link], max_pages: int) -> list[str]:
    candidates: dict[str, tuple[int, int]] = {}
    for order, link in enumerate(links):
        candidate = normalize_discovered_url(base, link.href)
        if candidate is None or not page_candidate_allowed(base, candidate):
            continue
        parsed = urlsplit(candidate)
        depth = len([part for part in parsed.path.split("/") if part])
        score = {"nav": 300, "header": 220, "main": 120, "footer": 20}.get(link.region, 60)
        score += max(0, 40 - depth * 8)
        if 2 <= len(link.text) <= 80:
            score += 20
        current = candidates.get(candidate)
        candidate_score = (score, -order)
        if current is None or candidate_score > current:
            candidates[candidate] = candidate_score
    ordered = sorted(candidates, key=lambda value: candidates[value], reverse=True)
    result = [base]
    result.extend(value for value in ordered if value != base)
    return result[:max_pages]


def sensitive_form_field(field: dict[str, str]) -> bool:
    field_type = field.get("type", "text").casefold()
    if field_type in SENSITIVE_INPUT_TYPES:
        return True
    markers = re.split(r"[^a-z0-9]+", field.get("name", "").casefold())
    return any(marker in SENSITIVE_FORM_MARKERS for marker in markers if marker)


def build_get_submission_fields(
    form: dict[str, Any],
    existing: list[tuple[str, str]],
    *,
    variable_field: dict[str, Any],
    variable_record: dict[str, str],
) -> list[dict[str, str]] | None:
    """Model the successful controls a browser will include in a GET submission."""
    records: list[dict[str, str]] = []
    for name, value in existing:
        records.append({
            "name": name,
            "value": value,
            "source": "form_action_default",
            "role": "fixed",
        })
    for field in form.get("fields", []):
        if field is variable_field:
            records.append(variable_record)
            continue
        field_type = field.get("type", "text")
        if field.get("disabled"):
            continue
        name = str(field.get("name", ""))
        if not name:
            continue
        if field_type in {"checkbox", "radio"}:
            if not field.get("checked"):
                continue
            if field_type == "radio" and name == variable_field.get("name"):
                continue
        elif field_type not in {"hidden", "search", "text", "select"}:
            continue
        value = " ".join(str(field.get("value", "")).split())
        if (
            sensitive_form_field(field)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name)
            or len(value) > 80
            or "@" in value
            or "://" in value
        ):
            return None
        source = {
            "hidden": "form_hidden_default",
            "select": "form_select_default",
            "checkbox": "form_checked_default",
            "radio": "form_checked_default",
        }.get(field_type, "form_default")
        records.append({
            "name": name,
            "value": value,
            "source": source,
            "role": "fixed",
        })
    return records if 1 <= len(records) <= 5 else None


def grounded_query_terms(parser: DiscoveryParser) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    ordered_links = sorted(
        enumerate(parser.links),
        key=lambda item: (
            {"main": 3, "nav": 2, "header": 1}.get(item[1].region, 0),
            -item[0],
        ),
        reverse=True,
    )
    candidates.extend((link.text, f"visible_{link.region}_link") for _, link in ordered_links)
    for separator in (" | ", " - ", " — ", ": "):
        if separator in parser.title:
            candidates.extend((part, "page_title") for part in parser.title.split(separator))
            break
    else:
        candidates.append((parser.title, "page_title"))

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw, source in candidates:
        value = " ".join(raw.split()).strip(" -–—|:,.\t\r\n")
        folded = value.casefold()
        if (
            folded in seen
            or folded in GENERIC_QUERY_TERMS
            or not 3 <= len(value) <= 80
            or len(value.split()) > 8
            or "@" in value
            or "://" in value
            or not re.search(r"[A-Za-z]", value)
        ):
            continue
        seen.add(folded)
        result.append((value, source))
    return result


def build_get_query_candidates(
    base: str,
    parser: DiscoveryParser,
    *,
    max_queries: int = 2,
) -> list[dict[str, Any]]:
    terms = grounded_query_terms(parser)
    if max_queries <= 0 or not terms:
        return []
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    term_index = 0
    for form_index, form in enumerate(parser.forms):
        if len(candidates) >= max_queries or form.get("method", "get").casefold() != "get":
            continue
        fields = form.get("fields", [])
        if any(sensitive_form_field(field) for field in fields):
            continue
        search_fields = [
            field for field in fields
            if field.get("name")
            and field.get("type", "text") in {"search", "text"}
            and (
                field["name"].casefold() in SEARCH_FIELD_NAMES
                or form.get("role") == "search"
                or "search" in field.get("placeholder", "").casefold()
                or "search" in field.get("aria_label", "").casefold()
            )
        ]
        if len(search_fields) != 1:
            continue
        action = normalize_discovered_url(base, form.get("action") or base)
        if action is None or not trusted_get_action_host(base, action):
            continue
        action_parts = urlsplit(action)
        if Path(action_parts.path).suffix.lower() in DOCUMENT_EXTENSIONS:
            continue
        segments = {segment.casefold() for segment in action_parts.path.split("/") if segment}
        if segments & BLOCKED_PATH_PARTS:
            continue
        existing = parse_qsl(action_parts.query, keep_blank_values=True)
        if any(
            any(marker in SENSITIVE_FORM_MARKERS for marker in re.split(r"[^a-z0-9]+", key.casefold()) if marker)
            for key, _ in existing
        ):
            continue
        field = search_fields[0]
        default = " ".join(field.get("value", "").split())
        if default and any(value == default for value, _ in terms):
            term, source = default, "form_default"
        else:
            term, source = terms[term_index % len(terms)]
            term_index += 1
        variable = {
            "name": field["name"],
            "value": term,
            "source": source,
            "role": "query",
        }
        submission_fields = build_get_submission_fields(
            form,
            existing,
            variable_field=field,
            variable_record=variable,
        )
        if submission_fields is None:
            continue
        pairs = [(item["name"], item["value"]) for item in submission_fields]
        query_url = urlunsplit((
            action_parts.scheme,
            action_parts.netloc,
            action_parts.path or "/",
            urlencode(pairs, doseq=True),
            "",
        ))
        if query_url in seen_urls:
            continue
        seen_urls.add(query_url)
        candidates.append({
            "url": query_url,
            "source_page": base,
            "form_index": form_index,
            "action": urlunsplit((action_parts.scheme, action_parts.netloc, action_parts.path or "/", "", "")),
            "method": "get",
            "kind": "search",
            "fields": submission_fields,
        })
    return candidates


def build_get_filter_candidates(
    base: str,
    parser: DiscoveryParser,
    *,
    max_filters: int = 1,
) -> list[dict[str, Any]]:
    """Build one bounded GET variant from a visible select/checkbox/radio option."""
    if max_filters <= 0:
        return []
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for form_index, form in enumerate(parser.forms):
        if len(candidates) >= max_filters or form.get("method", "get").casefold() != "get":
            continue
        fields = form.get("fields", [])
        if any(sensitive_form_field(field) for field in fields):
            continue
        action = normalize_discovered_url(base, form.get("action") or base)
        if action is None or not trusted_get_action_host(base, action):
            continue
        action_parts = urlsplit(action)
        if Path(action_parts.path).suffix.lower() in DOCUMENT_EXTENSIONS:
            continue
        segments = {segment.casefold() for segment in action_parts.path.split("/") if segment}
        if segments & BLOCKED_PATH_PARTS:
            continue
        existing = parse_qsl(action_parts.query, keep_blank_values=True)
        if any(
            any(marker in SENSITIVE_FORM_MARKERS for marker in re.split(r"[^a-z0-9]+", key.casefold()) if marker)
            for key, _ in existing
        ):
            continue

        chosen_field: dict[str, Any] | None = None
        chosen_option: dict[str, Any] | None = None
        for field in fields:
            name = field.get("name", "")
            if (
                not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name)
                or sensitive_form_field(field)
                or field.get("disabled")
            ):
                continue
            field_type = field.get("type")
            options: list[dict[str, Any]]
            if field_type == "select":
                default = str(field.get("value", ""))
                options = [
                    option for option in field.get("options", [])
                    if not option.get("disabled") and option.get("value") != default
                ]
                source = "visible_select_option"
            elif field_type in {"checkbox", "radio"} and not field.get("checked"):
                options = [{"value": field.get("value", ""), "label": field.get("label", "")}]
                source = f"visible_{field_type}_option"
            else:
                continue
            for option in options:
                value = " ".join(str(option.get("value", "")).split())
                label = " ".join(str(option.get("label", "")).split())
                if (
                    not value
                    or not 1 <= len(value) <= 80
                    or not 1 <= len(label) <= 100
                    or "@" in value
                    or "://" in value
                ):
                    continue
                chosen_field = field
                chosen_option = {"value": value, "label": label, "source": source}
                break
            if chosen_option is not None:
                break
        if chosen_field is None or chosen_option is None:
            continue

        variable = {
            "name": chosen_field["name"],
            "value": chosen_option["value"],
            "label": chosen_option["label"],
            "source": chosen_option["source"],
            "role": "filter",
        }
        submission_fields = build_get_submission_fields(
            form,
            existing,
            variable_field=chosen_field,
            variable_record=variable,
        )
        if submission_fields is None:
            continue
        pairs = [(item["name"], item["value"]) for item in submission_fields]
        query_url = urlunsplit((
            action_parts.scheme,
            action_parts.netloc,
            action_parts.path or "/",
            urlencode(pairs, doseq=True),
            "",
        ))
        if query_url in seen_urls:
            continue
        seen_urls.add(query_url)
        candidates.append({
            "url": query_url,
            "source_page": base,
            "form_index": form_index,
            "action": urlunsplit((action_parts.scheme, action_parts.netloc, action_parts.path or "/", "", "")),
            "method": "get",
            "kind": "filter",
            "fields": submission_fields,
        })
    return candidates


def build_get_form_candidates(
    base: str,
    parser: DiscoveryParser,
    *,
    max_candidates: int = 2,
    preferred_kind: str | None = None,
) -> list[dict[str, Any]]:
    if max_candidates <= 0:
        return []
    searches = build_get_query_candidates(base, parser, max_queries=max_candidates)
    filters = build_get_filter_candidates(base, parser, max_filters=1)
    selected: list[dict[str, Any]] = []
    known: set[str] = set()

    # Preserve workflow diversity inside the global two-variant capture budget.
    # Prefer one grounded search and one filter, then fill unused space with
    # additional searches only when no distinct filter is available.
    groups = (
        (filters, searches[:1], searches[1:])
        if preferred_kind == "filter"
        else (searches[:1], filters, searches[1:])
    )
    for group in groups:
        for record in group:
            if record["url"] in known:
                continue
            selected.append(record)
            known.add(record["url"])
            if len(selected) >= max_candidates:
                return selected
    return selected


def authorization_for(
    host: str,
    html: str,
    *,
    private_research_snapshot: bool = False,
) -> tuple[str | None, str]:
    normalized_host = host.lower().removeprefix("www.")
    body = html.casefold()
    if normalized_host in {"localhost", "127.0.0.1"}:
        return "local_test_fixture", "Local fixture used to exercise the capture pipeline."
    if normalized_host.endswith(".gov") or normalized_host.endswith(".mil"):
        return (
            "public_government_information_bounded_research_snapshot",
            "Public, unauthenticated government pages selected for a bounded, read-only, "
            "noncommercial offline research snapshot. This does not assert an open license.",
        )
    if private_research_snapshot:
        return PRIVATE_RESEARCH_AUTHORIZATION, PRIVATE_RESEARCH_NOTES
    detected = [marker for marker in LICENSE_MARKERS if marker in body]
    if detected:
        return (
            "explicit_license_marker_detected",
            "The public entry page contains explicit license markers: " + ", ".join(detected) + ".",
        )
    return None, (
        "No government-domain or explicit open-license marker was found on the public entry page. "
        "The pipeline refuses capture until a site-specific rights basis is recorded."
    )


def challenge_reason(
    status: int,
    title: str,
    html: str,
    headers: dict[str, str] | None = None,
    headings: list[str] | None = None,
) -> str | None:
    waf_action = (headers or {}).get("x-amzn-waf-action", "").casefold()
    if waf_action:
        return f"access challenge header x-amzn-waf-action={waf_action}"
    if status in BLOCKED_STATUS:
        return f"access-control HTTP status {status}"
    sample = (title + "\n" + html[:200_000]).casefold()
    for marker in CHALLENGE_MARKERS:
        if marker in sample:
            return f"challenge marker detected: {marker}"
    error_marker = content_error_reason("\n".join([title, *(headings or [])]))
    if error_marker:
        return f"content error marker detected: {error_marker}"
    return None


def response_body(response: requests.Response, limit: int = MAX_PAGE_BYTES) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise IntakeError(f"response exceeds byte limit: {declared} > {limit}")
        except ValueError:
            pass
    body = response.content
    if len(body) > limit:
        raise IntakeError(f"response exceeds byte limit: {len(body)} > {limit}")
    return body


def build_capture_spec(
    *, site: str, upstream_url: str, final_url: str, parser: DiscoveryParser,
    authorization_basis: str, authorization_notes: str, delay_seconds: float,
    max_pages: int, max_assets: int,
) -> dict[str, Any]:
    query_candidates = build_get_form_candidates(
        final_url, parser, max_candidates=min(2, max(0, max_pages - 1))
    )
    navigation_pages = select_start_urls(
        final_url, parser.links, max(1, max_pages - len(query_candidates))
    )
    start_urls = navigation_pages + [
        candidate["url"] for candidate in query_candidates
        if candidate["url"] not in navigation_pages
    ]
    page_hosts = sorted({
        host
        for value in [upstream_url, *start_urls]
        if (host := urlsplit(value).hostname)
    })
    asset_hosts = set(page_hosts)
    for raw in parser.asset_urls:
        candidate = normalize_discovered_url(final_url, raw)
        if candidate and same_site_host(final_url, candidate):
            asset_hosts.add(urlsplit(candidate).hostname or "")
    external_presentation_hosts: list[str] = []
    for raw in parser.presentation_asset_urls:
        candidate = normalize_discovered_url(final_url, raw)
        if candidate is None or same_site_host(final_url, candidate):
            continue
        host = urlsplit(candidate).hostname or ""
        if not host or host in external_presentation_hosts:
            continue
        if len(external_presentation_hosts) >= MAX_EXTERNAL_PRESENTATION_ASSET_HOSTS:
            break
        external_presentation_hosts.append(host)
        asset_hosts.add(host)
    exact_page_patterns = ["^" + re.escape(value) + "$" for value in start_urls]
    host_patterns = [
        rf"^https?://{re.escape(host)}/"
        for host in sorted(asset_hosts)
        if host
    ]
    return {
        "site": site,
        "upstream_url": upstream_url,
        "start_urls": start_urls,
        "generated_get_queries": query_candidates,
        "page_hosts": page_hosts,
        "asset_hosts": sorted(host for host in asset_hosts if host),
        "page_allow_patterns": exact_page_patterns,
        "asset_allow_patterns": host_patterns,
        "max_pages": len(start_urls),
        "discovery_max_pages": max_pages,
        "max_assets": max_assets,
        "max_depth": 0,
        "max_page_bytes": MAX_PAGE_BYTES,
        "max_asset_bytes": 8_000_000,
        "delay_seconds": delay_seconds,
        "user_agent": USER_AGENT,
        "authorization_basis": authorization_basis,
        "authorization_notes": authorization_notes,
        "output_dir": f"sites/{site}/static/external_cache/upstream",
    }


def run_intake(
    url: str,
    *,
    site: str | None = None,
    max_pages: int = 12,
    max_assets: int = 160,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    private_research_snapshot: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    upstream_url = normalize_entry_url(url)
    site = site or derive_site_id(upstream_url)
    if not re.fullmatch(r"[a-z0-9_]+", site):
        raise IntakeError("site must contain lowercase letters, digits, or underscores")
    injected_session = session is not None
    client = session or requests.Session()
    client.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    parsed = urlsplit(upstream_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    report: dict[str, Any] = {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "phase": "upstream_intake",
        "site": site,
        "upstream_url": upstream_url,
        "generated_at": utc_now(),
        "status": "rejected",
        "failures": [],
    }
    try:
        robots_response = safe_get(client, robots_url, injected_session=injected_session)
        robots_body = response_body(robots_response, 1_000_000)
        report["robots"] = {
            "url": robots_url,
            "final_url": robots_response.url,
            "status": robots_response.status_code,
            "bytes": len(robots_body),
            "sha256": sha256_bytes(robots_body),
        }
        robots_challenge = challenge_reason(
            robots_response.status_code,
            "",
            robots_body.decode(robots_response.encoding or "utf-8", errors="replace"),
            robots_response.headers,
        )
        if robots_challenge:
            raise IntakeError(f"robots response is blocked: {robots_challenge}")
        robots_unavailable = robots_response.status_code in {404, 410}
        if not successful_http_status(robots_response.status_code) and not robots_unavailable:
            raise IntakeError(f"robots request failed with HTTP {robots_response.status_code}")
        robots = RobotsPolicy(robots_url)
        robots.parse([] if robots_unavailable else robots_response.text.splitlines())
        report["robots"]["policy"] = (
            "unavailable_allow_all_rfc9309" if robots_unavailable else "parsed"
        )
        if not robots.can_fetch(USER_AGENT, upstream_url):
            raise IntakeError("robots.txt disallows the requested entry URL")
        crawl_delay = robots.crawl_delay(USER_AGENT) or robots.crawl_delay("*") or 1
        delay_seconds = max(1.0, min(float(crawl_delay), 60.0))
        sleep(delay_seconds)
        response = safe_get(client, upstream_url, injected_session=injected_session)
        body = response_body(response)
        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        final_url = normalize_entry_url(response.url)
        if not same_site_host(upstream_url, final_url):
            raise IntakeError(f"entry URL redirected to another site: {response.url}")
        if response.status_code != 200:
            raise IntakeError(f"entry page returned HTTP {response.status_code}")
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise IntakeError(f"entry page returned unsupported content type {media_type!r}")
        html = body.decode(response.encoding or "utf-8", errors="replace")
        discovery = DiscoveryParser()
        discovery.feed(html)
        challenge = challenge_reason(
            response.status_code,
            discovery.title,
            html,
            response.headers,
            discovery.headings,
        )
        if challenge:
            raise IntakeError(challenge)
        basis, notes = authorization_for(
            urlsplit(final_url).hostname or "",
            html,
            private_research_snapshot=private_research_snapshot,
        )
        report["entry_page"] = {
            "url": upstream_url,
            "final_url": final_url,
            "status": response.status_code,
            "content_type": media_type,
            "bytes": len(body),
            "sha256": sha256_bytes(body),
            "title": discovery.title,
            "description": discovery.description,
            "links_discovered": len(discovery.links),
            "forms_discovered": len(discovery.forms),
        }
        report["authorization"] = {"basis": basis, "notes": notes}
        if basis is None:
            raise IntakeError("authorization basis is ambiguous")
        spec = build_capture_spec(
            site=site,
            upstream_url=upstream_url,
            final_url=final_url,
            parser=discovery,
            authorization_basis=basis,
            authorization_notes=notes,
            delay_seconds=delay_seconds,
            max_pages=max_pages,
            max_assets=max_assets,
        )
        report["entry_page"]["safe_get_queries_generated"] = len(spec["generated_get_queries"])
        report["status"] = "passed"
        report["capture_scope"] = {
            "start_urls": spec["start_urls"],
            "generated_get_queries": spec["generated_get_queries"],
            "page_hosts": spec["page_hosts"],
            "asset_hosts": spec["asset_hosts"],
            "max_pages": spec["max_pages"],
            "max_assets": spec["max_assets"],
            "max_depth": spec["max_depth"],
            "delay_seconds": spec["delay_seconds"],
            "excluded": [
                "authentication and account paths",
                "unbounded query and facet crawling",
                "POST and state-changing form submissions",
                "cross-site pages and undeclared runtime assets",
                "personal or non-public data",
            ],
        }
        return report, spec
    except requests.RequestException as exc:
        report["status"] = "failed"
        report["failure_kind"] = "upstream_network_failure"
        report["resumable"] = True
        report["failures"].append(str(exc))
        return report, None
    except (IntakeError, ValueError) as exc:
        folded = str(exc).casefold()
        resumable = any(marker in folded for marker in (
            "response exceeds byte limit",
            "did not resolve safely",
            "request failed with http",
            "too many redirects",
            "redirect response omitted location",
        ))
        report["status"] = "failed" if resumable else "rejected"
        report["failure_kind"] = (
            "upstream_scope_or_network_failure"
            if resumable
            else "upstream_policy_rejection"
        )
        report["resumable"] = resumable
        report["failures"].append(str(exc))
        return report, None


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--site")
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--max-assets", type=int, default=160)
    parser.add_argument("--private-research-snapshot", action="store_true")
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.max_pages < 1 or args.max_assets < 0:
        parser.error("capture limits must be non-negative and max-pages must be positive")
    root = repository_root()
    site = args.site or derive_site_id(args.url)
    evidence_path = args.evidence or root / "mirror_factory" / "evidence" / site / "intake.json"
    spec_path = args.spec or root / "mirror_factory" / "upstream_specs" / f"{site}.json"
    report, spec = run_intake(
        args.url,
        site=site,
        max_pages=args.max_pages,
        max_assets=args.max_assets,
        private_research_snapshot=args.private_research_snapshot,
    )
    write_json(evidence_path.resolve(), report)
    if spec is not None:
        write_json(spec_path.resolve(), spec)
    print(json.dumps({
        "site": site,
        "status": report["status"],
        "spec": str(spec_path) if spec else None,
        "evidence": str(evidence_path),
        "failures": report["failures"],
    }, sort_keys=True))
    return 0 if spec is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
