"""Authenticated, privacy-preserving visual references for synthetic benchmarks."""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from .release import atomic_json, sha256_file


REFERENCE_SCHEMA = "webanything-synthetic-visual-reference-v1"
CAPTURE_SPEC_SCHEMA = "webanything-synthetic-visual-capture-spec-v1"
SANITIZER_VERSION = 4
CONTENT_STRATEGY = "source_visible"
TEXT_COMPARISON_COLOR = (126, 132, 142)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)+$")
ACCEPT_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9*;,=. -]+$")
MEDIA_SKY = (101, 149, 190)
MEDIA_GROUND = (49, 101, 103)
MEDIA_ACCENT = (238, 178, 77)
IDENTITY_BACKGROUND = (42, 126, 132)
IDENTITY_FOREGROUND = (224, 242, 238)
DEFAULT_THRESHOLDS = {
    "composite": 0.78,
    "masked_region": 0.70,
    "geometry": 0.74,
    "density": 0.72,
    "columns": 0.72,
    "spacing": 0.68,
}
DEFAULT_WEIGHTS = {
    "masked_region": 0.30,
    "geometry": 0.30,
    "density": 0.15,
    "columns": 0.15,
    "spacing": 0.10,
}


class SyntheticVisualError(RuntimeError):
    """A visual reference or comparison is invalid."""


class AuthExpiredError(SyntheticVisualError):
    """The authorized browser state no longer reaches the expected surface."""


class SourceBlockedError(SyntheticVisualError):
    """The source refused policy-compliant capture."""


class CaptureFailedError(SyntheticVisualError):
    """The source could not be captured."""


class SanitizeFailedError(SyntheticVisualError):
    """Private visual material could not be safely reduced."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SyntheticVisualError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyntheticVisualError(f"{label} must be a JSON object: {path}")
    return value


def _safe_artifact(base: Path, raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise SyntheticVisualError(f"{label} must be a nonempty relative path")
    relative = Path(raw)
    if ".." in relative.parts:
        raise SyntheticVisualError(f"{label} escapes the visual reference bundle")
    path = (base / relative).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as exc:
        raise SyntheticVisualError(f"{label} escapes the visual reference bundle") from exc
    return path


def _origin(raw: str) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SyntheticVisualError("capture URLs must be credential-free absolute HTTP(S) URLs")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.casefold()}{port}"


def _is_private_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            ]
        except OSError as exc:
            raise SourceBlockedError(f"capture source hostname could not be resolved: {hostname}: {exc}") from exc
    return any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    )


def _normalized_origins(values: Iterable[str]) -> list[str]:
    return sorted(set(_origin(value) for value in values))


def _load_key(path: Path) -> bytes:
    path = path.resolve()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        raw = path.read_bytes().strip()
    except OSError as exc:
        raise SyntheticVisualError(f"authentication key is unavailable: {path}: {exc}") from exc
    if mode & 0o077:
        raise SyntheticVisualError("authentication key file must not be readable or writable by group/other")
    candidates: list[bytes] = [raw]
    try:
        candidates.append(bytes.fromhex(raw.decode("ascii")))
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        candidates.append(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        pass
    key = next((item for item in candidates if len(item) == 32), None)
    if key is None:
        raise SyntheticVisualError("authentication key must decode to exactly 32 bytes")
    return key


def _domain_allowed(domain: str, origins: list[str]) -> bool:
    normalized = domain.lstrip(".").casefold()
    for origin in origins:
        host = urlsplit(origin).hostname or ""
        if host == normalized or host.endswith("." + normalized):
            return True
    return False


def _validate_storage_state(value: dict[str, Any], allowed_origins: list[str]) -> None:
    cookies = value.get("cookies", [])
    origins = value.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise SyntheticVisualError("Playwright storage state must contain cookie and origin arrays")
    for index, cookie in enumerate(cookies):
        if not isinstance(cookie, dict) or not _domain_allowed(str(cookie.get("domain") or ""), allowed_origins):
            raise SyntheticVisualError(f"storage state cookie[{index}] is outside allowed origins")
    for index, item in enumerate(origins):
        if not isinstance(item, dict) or _origin(str(item.get("origin") or "")) not in allowed_origins:
            raise SyntheticVisualError(f"storage state origin[{index}] is outside allowed origins")


def seal_auth_state(
    *,
    storage_state_path: Path,
    output_path: Path,
    key_path: Path,
    site: str,
    account: str,
    allowed_origins: list[str],
    delete_plaintext: bool = False,
) -> dict[str, Any]:
    """Encrypt an authorized Playwright state; plaintext is never copied into a run."""
    if not SITE_RE.fullmatch(site):
        raise SyntheticVisualError("auth profile site id is invalid")
    if not account.strip():
        raise SyntheticVisualError("auth profile account label is required")
    origins = _normalized_origins(allowed_origins)
    state = _read_json(storage_state_path.resolve(), "Playwright storage state")
    _validate_storage_state(state, origins)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise SyntheticVisualError("cryptography is required to seal authentication state") from exc
    account_sha256 = hashlib.sha256(account.encode("utf-8")).hexdigest()
    metadata = {
        "schema": "webanything-encrypted-auth-profile-v1",
        "site": site,
        "account_sha256": account_sha256,
        "allowed_origins": origins,
        "created_at": utc_now(),
        "cipher": "AES-256-GCM",
    }
    aad = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    plaintext = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(_load_key(key_path)).encrypt(nonce, plaintext, aad)
    envelope = {
        **metadata,
        "profile_id": canonical_sha256({"site": site, "account_sha256": account_sha256, "allowed_origins": origins}),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }
    atomic_json(output_path.resolve(), envelope)
    os.chmod(output_path.resolve(), 0o600)
    if delete_plaintext:
        storage_state_path.resolve().unlink()
    return {key: envelope[key] for key in ("schema", "site", "profile_id", "allowed_origins", "created_at")}


def load_auth_state(*, profile_path: Path, key_path: Path, site: str) -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = _read_json(profile_path.resolve(), "encrypted auth profile")
    required = {"schema", "site", "account_sha256", "allowed_origins", "created_at", "cipher", "profile_id", "nonce", "ciphertext"}
    if set(envelope) != required or envelope.get("schema") != "webanything-encrypted-auth-profile-v1":
        raise SyntheticVisualError("encrypted auth profile has an unsupported shape")
    if envelope.get("site") != site or envelope.get("cipher") != "AES-256-GCM":
        raise SyntheticVisualError("encrypted auth profile is bound to another site or cipher")
    origins = _normalized_origins(envelope.get("allowed_origins") or [])
    identity = {"site": site, "account_sha256": envelope["account_sha256"], "allowed_origins": origins}
    if envelope.get("profile_id") != canonical_sha256(identity):
        raise SyntheticVisualError("encrypted auth profile identity is invalid")
    metadata = {key: envelope[key] for key in ("schema", "site", "account_sha256", "allowed_origins", "created_at", "cipher")}
    aad = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = base64.urlsafe_b64decode(envelope["nonce"])
        ciphertext = base64.urlsafe_b64decode(envelope["ciphertext"])
        plaintext = AESGCM(_load_key(key_path)).decrypt(nonce, ciphertext, aad)
        state = json.loads(plaintext)
    except Exception as exc:
        raise SyntheticVisualError("encrypted auth profile could not be authenticated") from exc
    if not isinstance(state, dict):
        raise SyntheticVisualError("decrypted auth profile is not a Playwright storage state")
    _validate_storage_state(state, origins)
    return state, {"profile_id": envelope["profile_id"], "allowed_origins": origins}


GEOMETRY_SCRIPT = r"""() => {
  const viewport = {width: window.innerWidth, height: window.innerHeight};
  const identityPattern = /(^|[\s_-])(logo|brand|avatar|profile|account|username)([\s_-]|$)/i;
  const mediaPattern = /(^|[\s_-])(media|image|img|player|poster|thumbnail|thumb|photo)([\s_-]|$)/i;
  const customMediaPattern = /-(thumbnail|player|poster|image|img)$/i;
  const hasExternalPixels = value => /(?:url|image-set)\(/i.test(value || '');
  const visible = (node, style, rect) => style.display !== 'none'
    && style.visibility !== 'hidden'
    && Number.parseFloat(style.opacity || '1') > 0.01
    && rect.width > 1 && rect.height > 1
    && rect.bottom > 0 && rect.right > 0
    && rect.top < viewport.height && rect.left < viewport.width;
  const nodes = [...document.querySelectorAll('body *')];
  for (let index = 0; index < nodes.length && nodes.length < 2400; index += 1) {
    const root = nodes[index].shadowRoot;
    if (root) nodes.push(...root.querySelectorAll('*'));
  }
  const values = [];
  for (const node of nodes) {
    if (values.length >= 1600) break;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    if (!visible(node, style, rect)) continue;
    const token = `${node.id || ''} ${node.className || ''}`;
    const tag = node.tagName.toLowerCase();
    const directText = [...node.childNodes].some(child => child.nodeType === Node.TEXT_NODE && child.textContent.trim());
    const beforeStyle = getComputedStyle(node, '::before');
    const afterStyle = getComputedStyle(node, '::after');
    const pseudoContent = [beforeStyle.content, afterStyle.content]
      .some(value => value && !['none', 'normal', '""'].includes(value));
    const pseudoImage = [beforeStyle.backgroundImage, afterStyle.backgroundImage].some(hasExternalPixels);
    const role = node.getAttribute('role') || '';
    const borderWidth = ['borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth']
      .reduce((total, name) => total + (Number.parseFloat(style[name] || '0') || 0), 0);
    const transparent = ['transparent', 'rgba(0, 0, 0, 0)', 'rgba(0,0,0,0)'].includes(style.backgroundColor);
    const viewportArea = Math.max(viewport.width * viewport.height, 1);
    const boundedIdentity = identityPattern.test(token)
      && rect.width * rect.height / viewportArea <= 0.20
      && rect.width <= viewport.width * 0.65
      && rect.height <= viewport.height * 0.35;
    const boundedMediaToken = mediaPattern.test(token)
      && rect.width * rect.height / viewportArea <= 0.50;
    let category = 'container';
    if (tag === 'picture') continue;
    if (boundedIdentity) category = 'identity';
    else if (['img','video','canvas','svg','iframe','object','embed'].includes(tag)
        || role === 'img' || customMediaPattern.test(tag) || boundedMediaToken
        || hasExternalPixels(style.backgroundImage) || pseudoImage) category = 'media';
    else if (['button','input','select','textarea'].includes(tag) || ['button','textbox','checkbox','radio','combobox'].includes(role)) category = 'control';
    else if (directText || pseudoContent
        || (node.children.length === 0 && ['h1','h2','h3','h4','h5','h6','p','span','label','a','li','time'].includes(tag))) category = 'text';
    const clipped = {
      x: Math.max(0, Math.min(viewport.width, rect.x)),
      y: Math.max(0, Math.min(viewport.height, rect.y)),
      width: Math.max(0, Math.min(viewport.width, rect.right) - Math.max(0, rect.x)),
      height: Math.max(0, Math.min(viewport.height, rect.bottom) - Math.max(0, rect.y)),
    };
    if (clipped.width <= 1 || clipped.height <= 1) continue;
    values.push({
      category,
      x: Math.round(clipped.x), y: Math.round(clipped.y),
      width: Math.round(clipped.width), height: Math.round(clipped.height),
      position: ['fixed','sticky','absolute'].includes(style.position) ? style.position : 'flow',
      radius: Math.round(Number.parseFloat(style.borderRadius || '0')),
      surface: !transparent || borderWidth > 0 || style.boxShadow !== 'none',
    });
  }
  const landmarks = {};
  const landmarkSelectors = {
    header: 'header,[role="banner"],#masthead,ytd-masthead',
    nav: 'nav,[role="navigation"],#guide,ytd-guide-renderer',
    main: 'main,[role="main"],#content,ytd-page-manager',
    aside: 'aside,[role="complementary"],#secondary',
    footer: 'footer,[role="contentinfo"]',
  };
  for (const [name, selector] of Object.entries(landmarkSelectors)) {
    const candidates = [...document.querySelectorAll(selector)].map(node => ({node, rect: node.getBoundingClientRect(), style: getComputedStyle(node)}))
      .filter(item => visible(item.node, item.style, item.rect))
      .filter(item => {
        if (name === 'header') return item.rect.width >= viewport.width * 0.5 && item.rect.height <= viewport.height * 0.35;
        if (name === 'nav') return item.rect.width <= viewport.width * 0.5 || item.rect.height <= viewport.height * 0.35;
        if (name === 'aside') return item.rect.width <= viewport.width * 0.5;
        if (name === 'main') return item.rect.width * item.rect.height >= viewport.width * viewport.height * 0.08;
        return item.rect.height <= viewport.height * 0.5;
      })
      .sort((left, right) => right.rect.width * right.rect.height - left.rect.width * left.rect.height);
    if (!candidates.length) continue;
    const rect = candidates[0].rect;
    landmarks[name] = {
      x: Math.round(Math.max(0, rect.x)), y: Math.round(Math.max(0, rect.y)),
      width: Math.round(Math.min(viewport.width, rect.right) - Math.max(0, rect.x)),
      height: Math.round(Math.min(viewport.height, rect.bottom) - Math.max(0, rect.y)),
    };
  }
  return {
    schema: 'webanything-sanitized-dom-geometry-v1', viewport, elements: values,
    landmarks, truncated: values.length >= 1600 && nodes.length > values.length,
  };
}"""


GEOMETRY_CATEGORIES = ("container", "text", "identity", "media", "control")


def _rect_area(item: dict[str, Any]) -> int:
    return max(int(item["width"]), 0) * max(int(item["height"]), 0)


def _intersection_area(left: dict[str, Any], right: dict[str, Any]) -> int:
    width = max(0, min(left["x"] + left["width"], right["x"] + right["width"]) - max(left["x"], right["x"]))
    height = max(0, min(left["y"] + left["height"], right["y"] + right["height"]) - max(left["y"], right["y"]))
    return width * height


def _contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    return (
        outer["x"] <= inner["x"]
        and outer["y"] <= inner["y"]
        and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
        and outer["y"] + outer["height"] >= inner["y"] + inner["height"]
    )


def _normalize_elements(elements: list[dict[str, Any]], viewport: dict[str, int]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    viewport_area = max(viewport["width"] * viewport["height"], 1)
    ordered = sorted(
        elements,
        key=lambda item: (_rect_area(item), 0 if item.get("surface") else 1, item["y"], item["x"], item["category"]),
    )
    kept: list[dict[str, Any]] = []
    page_ancestors = 0
    overlaps = 0
    transparent_ancestors = 0
    for item in ordered:
        area = _rect_area(item)
        if item["category"] == "container" and area / viewport_area >= 0.86:
            page_ancestors += 1
            continue
        duplicate = False
        for existing in kept:
            if existing["category"] != item["category"]:
                continue
            smaller = min(area, _rect_area(existing))
            larger = max(area, _rect_area(existing))
            if smaller and larger / smaller <= 1.35 and _intersection_area(item, existing) / smaller >= 0.96:
                duplicate = True
                break
        if duplicate:
            overlaps += 1
            continue
        if item["category"] == "container" and item.get("surface") is False and area:
            if any(_contains(item, existing) and _rect_area(existing) / area >= 0.12 for existing in kept):
                transparent_ancestors += 1
                continue
        kept.append(item)
    without_nested_media: list[dict[str, Any]] = []
    nested_media = 0
    for item in kept:
        area = _rect_area(item)
        if item["category"] == "media" and any(
            other is not item
            and other["category"] == "media"
            and _rect_area(other) >= area * 1.5
            and _contains(other, item)
            for other in kept
        ):
            nested_media += 1
            continue
        without_nested_media.append(item)
    kept = without_nested_media
    kept.sort(key=lambda item: (-_rect_area(item), item["y"], item["x"], item["category"]))
    return kept, {
        "input_count": len(elements),
        "output_count": len(kept),
        "dropped_page_ancestors": page_ancestors,
        "dropped_overlaps": overlaps,
        "dropped_transparent_ancestors": transparent_ancestors,
        "dropped_nested_media": nested_media,
    }


def validate_geometry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != "webanything-sanitized-dom-geometry-v1":
        raise SanitizeFailedError("sanitized DOM geometry has an unsupported schema")
    encoded = json.dumps(value, ensure_ascii=False).casefold()
    for forbidden in ("textcontent", "innerhtml", "outerhtml", "aria-label", "cookie", "localstorage"):
        if forbidden in encoded:
            raise SanitizeFailedError(f"sanitized DOM geometry contains forbidden field: {forbidden}")
    viewport = value.get("viewport") or {}
    if (
        set(viewport) != {"width", "height"}
        or not isinstance(viewport.get("width"), int)
        or not isinstance(viewport.get("height"), int)
    ):
        raise SanitizeFailedError("sanitized DOM geometry lacks viewport dimensions")
    elements = value.get("elements")
    if not isinstance(elements, list) or not elements:
        raise SanitizeFailedError("sanitized DOM geometry must contain visible elements")
    for item in elements:
        if (
            not isinstance(item, dict)
            or set(item) != {"category", "x", "y", "width", "height", "position", "radius", "surface"}
            or item.get("category") not in GEOMETRY_CATEGORIES
        ):
            raise SanitizeFailedError("sanitized DOM geometry contains an invalid element")
        if any(not isinstance(item.get(key), int) for key in ("x", "y", "width", "height")):
            raise SanitizeFailedError("sanitized DOM geometry contains an invalid rectangle")
        if item.get("position") not in {"flow", "fixed", "sticky", "absolute"} or not isinstance(item.get("radius"), int) or not isinstance(item.get("surface"), bool):
            raise SanitizeFailedError("sanitized DOM geometry contains invalid style metadata")
    landmarks = value.get("landmarks")
    if not isinstance(landmarks, dict) or any(name not in {"header", "nav", "main", "aside", "footer"} for name in landmarks):
        raise SanitizeFailedError("sanitized DOM geometry contains invalid landmarks")
    for rectangle in landmarks.values():
        if not isinstance(rectangle, dict) or set(rectangle) != {"x", "y", "width", "height"} or any(
            not isinstance(rectangle.get(key), int) for key in ("x", "y", "width", "height")
        ):
            raise SanitizeFailedError("sanitized DOM geometry contains an invalid landmark rectangle")
    if not isinstance(value.get("truncated"), bool):
        raise SanitizeFailedError("sanitized DOM geometry lacks its truncation status")
    normalized, stats = _normalize_elements(elements, viewport)
    existing_stats = value.get("normalization")
    if existing_stats is not None:
        if not isinstance(existing_stats, dict) or normalized != elements:
            raise SanitizeFailedError("sanitized DOM geometry normalization is stale")
        stats = existing_stats
    return {**value, "elements": normalized, "normalization": stats}


def _synthetic_media(width: int, height: int, radius: int) -> tuple[Any, Any]:
    from PIL import Image, ImageDraw

    region = Image.new("RGB", (width, height), MEDIA_SKY)
    draw = ImageDraw.Draw(region)
    bands = max(8, min(32, height))
    for index in range(bands):
        ratio = index / max(bands - 1, 1)
        color = tuple(round(MEDIA_SKY[channel] * (1 - ratio) + (218, 230, 223)[channel] * ratio) for channel in range(3))
        top = round(index / bands * height)
        bottom = round((index + 1) / bands * height)
        draw.rectangle((0, top, width, bottom), fill=color)
    sun = max(8, min(width, height) // 8)
    draw.ellipse((width - sun * 3, sun, width - sun, sun * 3), fill=MEDIA_ACCENT)
    horizon = round(height * 0.68)
    draw.polygon(((0, horizon), (round(width * 0.28), round(height * 0.38)), (round(width * 0.55), horizon),
                  (round(width * 0.76), round(height * 0.5)), (width, horizon), (width, height), (0, height)), fill=MEDIA_GROUND)
    draw.polygon(((0, round(height * 0.82)), (round(width * 0.42), round(height * 0.6)),
                  (round(width * 0.72), round(height * 0.8)), (width, round(height * 0.7)), (width, height), (0, height)),
                 fill=(66, 122, 106))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width, height), radius=max(0, min(radius, min(width, height) // 2)), fill=255)
    return region, mask


def _synthetic_identity(width: int, height: int, radius: int) -> tuple[Any, Any]:
    from PIL import Image, ImageDraw

    region = Image.new("RGB", (width, height), IDENTITY_BACKGROUND)
    draw = ImageDraw.Draw(region)
    size = max(2, min(width, height))
    inset = max(1, size // 5)
    draw.ellipse((inset, inset, min(width - inset, size - inset), min(height - inset, size - inset)), fill=IDENTITY_FOREGROUND)
    draw.ellipse((inset * 2, inset * 2, min(width - inset * 2, size - inset * 2), min(height - inset * 2, size - inset * 2)), fill=(89, 163, 158))
    if width > height * 1.8:
        bar_height = max(2, height // 5)
        draw.rounded_rectangle((height, height // 3, width - max(2, height // 4), height // 3 + bar_height), radius=bar_height // 2, fill=IDENTITY_FOREGROUND)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width, height), radius=max(0, min(radius, min(width, height) // 2)), fill=255)
    return region, mask


def sanitize_screenshot(raw_png: bytes, geometry: dict[str, Any]) -> bytes:
    """Create an internal content-neutral comparison image.

    The source-visible screenshot is stored separately and is never modified by
    this function.
    """
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(raw_png)).convert("RGB")
        viewport = geometry["viewport"]
        if image.size != (viewport["width"], viewport["height"]):
            raise SanitizeFailedError("screenshot dimensions differ from sanitized geometry")
        for item in geometry["elements"]:
            if item["category"] not in {"media", "identity", "text"}:
                continue
            x0, y0 = item["x"], item["y"]
            width, height = item["width"], item["height"]
            if width <= 0 or height <= 0:
                continue
            if item["category"] == "media":
                region, mask = _synthetic_media(width, height, item["radius"])
            elif item["category"] == "identity":
                region, mask = _synthetic_identity(width, height, item["radius"])
            else:
                region = Image.new("RGB", (width, height), TEXT_COMPARISON_COLOR)
                mask = Image.new("L", (width, height), 255)
            image.paste(region, (x0, y0), mask)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=False, compress_level=9)
        return output.getvalue()
    except SanitizeFailedError:
        raise
    except Exception as exc:
        raise SanitizeFailedError(f"screenshot sanitization failed: {exc}") from exc


def _merged_length(intervals: list[tuple[int, int]]) -> int:
    total = 0
    end = -1
    for start, stop in sorted(intervals):
        if stop <= start:
            continue
        if start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


def _union_area(rectangles: list[dict[str, Any]]) -> int:
    boundaries = sorted({value for item in rectangles for value in (item["x"], item["x"] + item["width"])})
    total = 0
    for left, right in zip(boundaries, boundaries[1:]):
        if right <= left:
            continue
        intervals = [
            (item["y"], item["y"] + item["height"])
            for item in rectangles
            if item["x"] < right and item["x"] + item["width"] > left
        ]
        total += (right - left) * _merged_length(intervals)
    return total


def geometry_metrics(geometry: dict[str, Any]) -> dict[str, Any]:
    width = geometry["viewport"]["width"]
    height = geometry["viewport"]["height"]
    area = max(width * height, 1)
    counts = {name: 0 for name in GEOMETRY_CATEGORIES}
    by_category = {name: [] for name in GEOMETRY_CATEGORIES}
    columns: list[float] = []
    significant: list[dict[str, Any]] = []
    for item in geometry["elements"]:
        category = item["category"]
        counts[category] += 1
        by_category[category].append(item)
        item_area = max(item["width"], 0) * max(item["height"], 0)
        if item_area / area >= 0.005:
            significant.append(item)
    coverage = {name: _union_area(items) / area for name, items in by_category.items()}
    for index in range(24):
        left = index * width / 24
        right = (index + 1) * width / 24
        intervals = [
            (item["y"], item["y"] + item["height"])
            for item in geometry["elements"]
            if item["x"] < right and item["x"] + item["width"] > left
        ]
        columns.append(_merged_length(intervals) / max(height, 1))
    ordered = sorted(significant, key=lambda item: (item["y"], item["x"]))
    gaps = []
    for left, right in zip(ordered, ordered[1:]):
        gap = max(0, right["y"] - (left["y"] + left["height"]))
        gaps.append(round(gap / max(height, 1), 4))
    return {
        "counts": counts,
        "coverage": {key: round(min(value, 1.0), 6) for key, value in coverage.items()},
        "overlap": {
            key: round(max(0.0, sum(_rect_area(item) for item in items) / max(_union_area(items), 1) - 1.0), 6)
            for key, items in by_category.items()
        },
        "columns": [round(min(value, 1.0), 6) for value in columns],
        "spacing": sorted(gaps)[:128],
        "landmarks": geometry.get("landmarks") or {},
        "max_rectangle_coverage": round(max((_rect_area(item) / area for item in geometry["elements"]), default=0.0), 6),
    }


def geometry_quality(geometry: dict[str, Any]) -> dict[str, Any]:
    metrics = geometry_metrics(geometry)
    failures: list[dict[str, Any]] = []
    saturated = sum(value >= 0.98 for value in metrics["columns"])
    maximum_overlap = max(metrics["overlap"].values(), default=0.0)
    if geometry.get("truncated") is True:
        failures.append({"code": "geometry_truncated", "message": "visible element extraction reached its safety limit"})
    if metrics["max_rectangle_coverage"] >= 0.88:
        failures.append({"code": "page_covering_rectangle", "message": "one retained rectangle covers almost the entire viewport"})
    if saturated >= 23:
        failures.append({"code": "column_saturation", "message": "layout occupancy saturates almost every horizontal column", "columns": saturated})
    if not metrics["landmarks"]:
        failures.append({"code": "landmarks_missing", "message": "no effective page landmark could be extracted"})
    if maximum_overlap > 4.0:
        failures.append({"code": "rectangle_overlap", "message": "same-category rectangles overlap excessively", "ratio": maximum_overlap})
    if len(geometry["elements"]) < 4:
        failures.append({"code": "geometry_too_sparse", "message": "too few visible regions remain after sanitization"})
    return {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "metrics": {
            "saturated_columns": saturated,
            "landmark_count": len(metrics["landmarks"]),
            "max_rectangle_coverage": metrics["max_rectangle_coverage"],
            "max_overlap_ratio": maximum_overlap,
            "normalization": geometry.get("normalization") or {},
        },
    }


def _read_robots(parser: RobotFileParser, robots_url: str, user_agent: str) -> None:
    """Fetch robots with the declared capture identity and fail closed."""
    request = Request(
        robots_url,
        headers={"User-Agent": user_agent, "Accept": "text/plain,*/*;q=0.1"},
    )
    response = None
    try:
        response = urlopen(request, timeout=15)
        status_value = getattr(response, "status", None)
        status = int(status_value if status_value is not None else response.getcode())
        final_url = response.geturl()
        final = urlsplit(final_url)
        if final.scheme not in {"http", "https"} or not final.hostname or _is_private_host(final.hostname):
            raise SourceBlockedError("robots.txt redirected to an unsafe destination")
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise SourceBlockedError("robots.txt exceeds the capture safety limit")
    except HTTPError as exc:
        status = int(exc.code)
        body = b""
    except (URLError, OSError) as exc:
        raise SourceBlockedError(f"robots.txt could not be checked: {exc}") from exc
    finally:
        if response is not None:
            response.close()
    if status in {401, 403}:
        parser.disallow_all = True
    elif 400 <= status < 500:
        # RFC-compatible missing robots file: there are no declared rules.
        parser.allow_all = True
    elif status < 200 or status >= 300:
        raise SourceBlockedError(f"robots.txt returned HTTP status {status}")
    else:
        parser.parse(body.decode("utf-8", errors="replace").splitlines())


def _check_robots(
    url: str,
    user_agent: str,
    policy: str,
    *,
    allow_private_network: bool,
    cache: dict[str, RobotFileParser] | None = None,
) -> None:
    parsed = urlsplit(url)
    if _is_private_host(parsed.hostname or ""):
        if allow_private_network and policy == "not_applicable_local_test":
            return
        raise SourceBlockedError("private or loopback capture requires the explicit local-test policy")
    if policy != "respect":
        raise SourceBlockedError("production source capture must respect robots.txt")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    parser = cache.get(origin) if cache is not None else None
    if parser is None:
        parser = RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        parser.set_url(robots_url)
        _read_robots(parser, robots_url, user_agent)
        if cache is not None:
            cache[origin] = parser
    if not parser.can_fetch(user_agent, url):
        raise SourceBlockedError("robots.txt does not allow this capture route")


def load_capture_spec(path: Path, *, allow_private_network: bool = False) -> dict[str, Any]:
    spec = _read_json(path.resolve(), "synthetic visual capture spec")
    if spec.get("schema") != CAPTURE_SPEC_SCHEMA or not SITE_RE.fullmatch(str(spec.get("site") or "")):
        raise SyntheticVisualError("unsupported synthetic visual capture spec")
    viewport = spec.get("viewport") or {}
    if any(
        not isinstance(viewport.get(key), int) or isinstance(viewport[key], bool) or viewport[key] < 320
        for key in ("width", "height")
    ):
        raise SyntheticVisualError("capture viewport dimensions must be integers of at least 320")
    routes = spec.get("routes")
    if not isinstance(routes, list) or not routes:
        raise SyntheticVisualError("capture spec routes must be nonempty")
    auth = spec.get("auth") or {"required": False}
    if not isinstance(auth, dict) or not isinstance(auth.get("required"), bool):
        raise SyntheticVisualError("capture spec auth.required must be boolean")
    browser_profile = spec.get("browser_profile") or {}
    if not isinstance(browser_profile, dict) or set(browser_profile) - {
        "locale", "timezone_id", "accept_language"
    }:
        raise SyntheticVisualError("capture spec browser_profile is invalid")
    locale = browser_profile.get("locale")
    timezone_id = browser_profile.get("timezone_id")
    accept_language = browser_profile.get("accept_language")
    if locale is not None and (not isinstance(locale, str) or not LOCALE_RE.fullmatch(locale)):
        raise SyntheticVisualError("capture spec browser_profile.locale is invalid")
    if timezone_id is not None and (
        not isinstance(timezone_id, str) or not TIMEZONE_RE.fullmatch(timezone_id)
    ):
        raise SyntheticVisualError("capture spec browser_profile.timezone_id is invalid")
    if accept_language is not None and (
        not isinstance(accept_language, str) or not ACCEPT_LANGUAGE_RE.fullmatch(accept_language)
    ):
        raise SyntheticVisualError("capture spec browser_profile.accept_language is invalid")
    route_ids: set[str] = set()
    synthetic_routes: set[str] = set()
    source_origin = _origin(str(spec.get("source_origin") or ""))
    robots_cache: dict[str, RobotFileParser] = {}
    allowed_resource_origins = _normalized_origins(spec.get("allowed_resource_origins") or [])
    allowed_route_origins = set(_normalized_origins([
        source_origin, *(spec.get("allowed_route_origins") or [])
    ]))
    for resource_origin in allowed_resource_origins:
        resource_host = urlsplit(resource_origin).hostname or ""
        if _is_private_host(resource_host) and not (
            allow_private_network and spec.get("robots_policy") == "not_applicable_local_test"
        ):
            raise SourceBlockedError("private resource origins require the explicit local-test policy")
    for index, route in enumerate(routes):
        if not isinstance(route, dict) or not re.fullmatch(r"route_[a-z0-9_]+", str(route.get("id") or "")):
            raise SyntheticVisualError(f"capture routes[{index}] has an invalid id")
        if route["id"] in route_ids or route.get("synthetic_route") in synthetic_routes:
            raise SyntheticVisualError("capture route ids and synthetic routes must be unique")
        route_ids.add(route["id"])
        synthetic_routes.add(route.get("synthetic_route"))
        if not isinstance(route.get("synthetic_route"), str) or not route["synthetic_route"].startswith("/"):
            raise SyntheticVisualError(f"capture routes[{index}] synthetic_route is invalid")
        if route.get("comparison_mode", "source_visible") not in {"source_visible", "content_neutral"}:
            raise SyntheticVisualError(f"capture routes[{index}] comparison_mode is invalid")
        private_selectors = route.get("private_selectors") or []
        if not isinstance(private_selectors, list):
            raise SyntheticVisualError(f"capture routes[{index}].private_selectors must be an array")
        for private_index, item in enumerate(private_selectors):
            if (
                not isinstance(item, dict)
                or set(item) != {"selector", "replacement"}
                or not isinstance(item.get("selector"), str)
                or not item["selector"].strip()
                or len(item["selector"]) > 300
                or not isinstance(item.get("replacement"), str)
                or len(item["replacement"]) > 160
            ):
                raise SyntheticVisualError(
                    f"capture routes[{index}].private_selectors[{private_index}] is invalid"
                )
        allowed_quality_failures = route.get("allow_quality_failures") or []
        if (
            not isinstance(allowed_quality_failures, list)
            or any(item not in {"landmarks_missing"} for item in allowed_quality_failures)
        ):
            raise SyntheticVisualError(
                f"capture routes[{index}].allow_quality_failures is invalid"
            )
        if _origin(str(route.get("source_url") or "")) not in allowed_route_origins:
            raise SyntheticVisualError(f"capture routes[{index}] source URL is outside allowed route origins")
        _check_robots(
            route["source_url"],
            str(spec.get("user_agent") or "WebAnythingVisualReference/1.0"),
            str(spec.get("robots_policy") or "respect"),
            allow_private_network=allow_private_network,
            cache=robots_cache,
        )
    thresholds = {**DEFAULT_THRESHOLDS, **(spec.get("thresholds") or {})}
    if set(thresholds) != set(DEFAULT_THRESHOLDS) or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1
        for value in thresholds.values()
    ):
        raise SyntheticVisualError("every visual threshold must be between zero and one")
    weights = {**DEFAULT_WEIGHTS, **(spec.get("weights") or {})}
    if set(weights) != set(DEFAULT_WEIGHTS) or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0
        for value in weights.values()
    ) or abs(sum(weights.values()) - 1.0) > 1e-6:
        raise SyntheticVisualError("visual comparison weights must contain the five metrics and total one")
    return {
        **spec,
        "source_origin": source_origin,
        "allowed_resource_origins": allowed_resource_origins,
        "allowed_route_origins": sorted(allowed_route_origins - {source_origin}),
        "browser_profile": browser_profile,
        "thresholds": thresholds,
        "weights": weights,
    }


def _capture_error_state(page: Any, route: dict[str, Any]) -> str | None:
    current = page.url.casefold()
    if any(token.casefold() in current for token in route.get("auth_failure_url_contains", [])):
        return "auth_expired"
    for selector in route.get("auth_failure_selectors", []):
        if page.locator(selector).count() > 0:
            return "auth_expired"
    required = route.get("authenticated_selector")
    if required and page.locator(required).count() == 0:
        return "auth_expired"
    return None


def capture_visual_reference(
    *,
    spec_path: Path,
    output_dir: Path,
    auth_profile_path: Path | None = None,
    key_path: Path | None = None,
    browser_path: Path | None = None,
    dependency_path: Path | None = None,
    allow_private_network: bool = False,
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    """Capture and freeze only sanitized pixels and text-free DOM geometry."""
    spec_path = spec_path.resolve()
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SyntheticVisualError("visual reference output already exists; frozen references are immutable")
    spec = load_capture_spec(spec_path, allow_private_network=allow_private_network)
    auth = spec.get("auth") or {"required": False}
    storage_state = None
    auth_public = {"used": False, "profile_id": None}
    if auth.get("required") is True:
        if auth_profile_path is None or key_path is None:
            raise AuthExpiredError("authorized capture requires an encrypted profile and key file")
        storage_state, auth_metadata = load_auth_state(profile_path=auth_profile_path, key_path=key_path, site=spec["site"])
        allowed = set(auth_metadata["allowed_origins"])
        if spec["source_origin"] not in allowed:
            raise SyntheticVisualError("auth profile does not allow the capture source origin")
        auth_public = {"used": True, "profile_id": auth_metadata["profile_id"]}
    elif auth_profile_path is not None or key_path is not None:
        raise SyntheticVisualError("auth material was supplied to a capture spec that does not require it")
    if dependency_path:
        import sys
        sys.path.insert(0, str(dependency_path.resolve()))
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
    except ImportError as exc:
        raise CaptureFailedError("Playwright is required for visual reference capture") from exc
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    route_records: list[dict[str, Any]] = []
    private_fields_replaced = 0
    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if browser_path:
                launch["executable_path"] = str(browser_path.resolve())
            browser = playwright.chromium.launch(**launch)
            context_options: dict[str, Any] = {"viewport": spec["viewport"], "device_scale_factor": 1}
            browser_profile = spec["browser_profile"]
            if browser_profile.get("locale"):
                context_options["locale"] = browser_profile["locale"]
            if browser_profile.get("timezone_id"):
                context_options["timezone_id"] = browser_profile["timezone_id"]
            if browser_profile.get("accept_language"):
                context_options["extra_http_headers"] = {
                    "Accept-Language": browser_profile["accept_language"]
                }
            if storage_state is not None:
                context_options["storage_state"] = storage_state
            context = browser.new_context(**context_options)
            allowed_resource_origins = set(_normalized_origins([
                spec["source_origin"], *(spec.get("allowed_resource_origins") or [])
            ]))
            blocked_origins: set[str] = set()

            def route_request(route: Any) -> None:
                request_url = route.request.url
                parsed = urlsplit(request_url)
                if parsed.scheme not in {"http", "https"}:
                    route.continue_()
                    return
                origin = _origin(request_url)
                if origin in allowed_resource_origins:
                    route.continue_()
                else:
                    blocked_origins.add(origin)
                    route.abort("blockedbyclient")

            context.route("**/*", route_request)
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            for route_spec in spec["routes"]:
                try:
                    response = page.goto(route_spec["source_url"], wait_until="domcontentloaded", timeout=timeout_ms)
                    if response is None:
                        raise CaptureFailedError("capture navigation produced no response")
                    if response.status == 401 and storage_state is not None:
                        raise AuthExpiredError(f"authorized state was rejected for route {route_spec['id']}")
                    if response.status in {401, 403, 407, 429}:
                        raise SourceBlockedError(f"source returned blocking HTTP status {response.status}")
                    if response.status >= 400:
                        raise CaptureFailedError(f"source returned HTTP status {response.status}")
                    wait_for = route_spec.get("wait_for") or "body"
                    page.wait_for_selector(wait_for, state="visible", timeout=timeout_ms)
                    page.wait_for_timeout(int(route_spec.get("settle_ms", 750)))
                except PlaywrightTimeoutError as exc:
                    raise CaptureFailedError(f"capture route timed out: {route_spec['id']}") from exc
                state = _capture_error_state(page, route_spec)
                if state == "auth_expired":
                    raise AuthExpiredError(f"authorized state no longer reaches route {route_spec['id']}")
                private_fields_replaced += int(page.evaluate(
                    """items => {
                      let changed = 0;
                      for (const item of items || []) {
                        let matches = [];
                        try { matches = [...document.querySelectorAll(item.selector)]; } catch (_) { continue; }
                        for (const node of matches) {
                          if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
                            node.value = item.replacement || 'demo@example.test';
                            node.setAttribute('value', item.replacement || 'demo@example.test');
                          } else {
                            node.textContent = item.replacement || 'Demo User';
                          }
                          changed += 1;
                        }
                      }
                      return changed;
                    }""",
                    route_spec.get("private_selectors") or [],
                ))
                geometry = validate_geometry(page.evaluate(GEOMETRY_SCRIPT))
                quality = geometry_quality(geometry)
                for _quality_retry in range(2):
                    remaining_quality_failures = [
                        item for item in quality["failures"]
                        if item["code"] not in set(route_spec.get("allow_quality_failures") or [])
                    ]
                    if not remaining_quality_failures:
                        break
                    page.wait_for_timeout(1_500)
                    geometry = validate_geometry(page.evaluate(GEOMETRY_SCRIPT))
                    quality = geometry_quality(geometry)
                remaining_quality_failures = [
                    item for item in quality["failures"]
                    if item["code"] not in set(route_spec.get("allow_quality_failures") or [])
                ]
                if remaining_quality_failures:
                    codes = ", ".join(item["code"] for item in remaining_quality_failures)
                    raise SanitizeFailedError(f"sanitized reference quality failed for {route_spec['id']}: {codes}")
                raw_png = page.screenshot(type="png", full_page=False, animations="disabled")
                comparison_png = sanitize_screenshot(raw_png, geometry)
                stem = route_spec["id"]
                image_relative = f"routes/{stem}.source-visible.png"
                comparison_relative = f"routes/{stem}.comparison.png"
                geometry_relative = f"routes/{stem}.geometry.json"
                image_path = temporary / image_relative
                comparison_path = temporary / comparison_relative
                geometry_path = temporary / geometry_relative
                image_path.parent.mkdir(parents=True, exist_ok=True)
                image_path.write_bytes(raw_png)
                comparison_path.write_bytes(comparison_png)
                del raw_png
                atomic_json(geometry_path, geometry)
                accepted_quality_failure_codes = sorted(
                    item["code"] for item in quality["failures"]
                    if item["code"] in set(route_spec.get("allow_quality_failures") or [])
                )
                effective_quality = quality if not accepted_quality_failure_codes else {
                    **quality,
                    "status": "passed",
                    "failures": [],
                }
                route_records.append({
                    "id": stem,
                    "synthetic_route": route_spec["synthetic_route"],
                    "comparison_mode": route_spec.get("comparison_mode", "source_visible"),
                    "source_url_sha256": hashlib.sha256(route_spec["source_url"].encode("utf-8")).hexdigest(),
                    "screenshot": {"path": image_relative, "sha256": sha256_file(image_path)},
                    "comparison_screenshot": {"path": comparison_relative, "sha256": sha256_file(comparison_path)},
                    "geometry": {"path": geometry_relative, "sha256": sha256_file(geometry_path)},
                    "metrics": geometry_metrics(geometry),
                    "quality": effective_quality,
                    "accepted_quality_failure_codes": accepted_quality_failure_codes,
                })
            context.close()
            browser.close()
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
            "auth": auth_public,
            "private_fields_replaced": private_fields_replaced,
            "blocked_resource_origin_sha256s": sorted(
                hashlib.sha256(origin.encode("utf-8")).hexdigest() for origin in blocked_origins
            ),
            "thresholds": spec["thresholds"],
            "weights": spec["weights"],
            "routes": route_records,
            "raw_material_persisted": True,
        }
        manifest["reference_sha256"] = canonical_sha256(manifest)
        atomic_json(temporary / "synthetic_visual_reference.json", manifest)
        if output_dir.exists():
            output_dir.rmdir()
        os.replace(temporary, output_dir)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_visual_reference(path: Path) -> dict[str, Any]:
    path = path.resolve()
    manifest = _read_json(path, "synthetic visual reference")
    digest_input = {key: value for key, value in manifest.items() if key != "reference_sha256"}
    if (
        manifest.get("schema") != REFERENCE_SCHEMA
        or manifest.get("schema_version") != 1
        or manifest.get("state") != "reference_frozen"
        or manifest.get("sanitizer_version") != SANITIZER_VERSION
        or manifest.get("content_strategy") != CONTENT_STRATEGY
        or manifest.get("raw_material_persisted") is not True
        or not SITE_RE.fullmatch(str(manifest.get("site") or ""))
        or manifest.get("reference_sha256") != canonical_sha256(digest_input)
    ):
        raise SyntheticVisualError("synthetic visual reference identity is invalid")
    routes = manifest.get("routes")
    if not isinstance(routes, list) or not routes:
        raise SyntheticVisualError("synthetic visual reference routes are empty")
    viewport = manifest.get("viewport") or {}
    if any(
        not isinstance(viewport.get(key), int) or isinstance(viewport[key], bool) or viewport[key] < 320
        for key in ("width", "height")
    ):
        raise SyntheticVisualError("synthetic visual reference viewport is invalid")
    thresholds = manifest.get("thresholds") or {}
    if set(thresholds) != set(DEFAULT_THRESHOLDS) or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1
        for value in thresholds.values()
    ):
        raise SyntheticVisualError("synthetic visual reference thresholds are invalid")
    weights = manifest.get("weights") or {}
    if set(weights) != set(DEFAULT_WEIGHTS) or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0
        for value in weights.values()
    ) or abs(sum(weights.values()) - 1.0) > 1e-6:
        raise SyntheticVisualError("synthetic visual reference weights are invalid")
    blocked = manifest.get("blocked_resource_origin_sha256s", [])
    if not isinstance(blocked, list) or any(not isinstance(item, str) or not SHA256_RE.fullmatch(item) for item in blocked):
        raise SyntheticVisualError("synthetic visual reference blocked-origin hashes are invalid")
    seen: set[str] = set()
    for index, route in enumerate(routes):
        if (
            not isinstance(route, dict)
            or not re.fullmatch(r"route_[a-z0-9_]+", str(route.get("id") or ""))
            or route.get("id") in seen
            or not isinstance(route.get("synthetic_route"), str)
            or not route["synthetic_route"].startswith("/")
            or route.get("comparison_mode", "source_visible") not in {"source_visible", "content_neutral"}
            or not SHA256_RE.fullmatch(str(route.get("source_url_sha256") or ""))
        ):
            raise SyntheticVisualError(f"visual reference route[{index}] is invalid or duplicated")
        seen.add(route.get("id"))
        for key in ("screenshot", "comparison_screenshot", "geometry"):
            descriptor = route.get(key) or {}
            artifact = _safe_artifact(path.parent, descriptor.get("path"), f"routes[{index}].{key}.path")
            if artifact.is_symlink() or not artifact.is_file() or sha256_file(artifact) != descriptor.get("sha256"):
                raise SyntheticVisualError(f"visual reference {key} artifact is missing or stale: {artifact}")
        geometry = validate_geometry(_read_json(
            _safe_artifact(path.parent, route["geometry"]["path"], "geometry.path"),
            "sanitized geometry",
        ))
        expected_quality = geometry_quality(geometry)
        expected_failure_codes = sorted(item["code"] for item in expected_quality["failures"])
        accepted_failure_codes = route.get("accepted_quality_failure_codes", [])
        if (
            not isinstance(accepted_failure_codes, list)
            or any(not isinstance(item, str) for item in accepted_failure_codes)
            or accepted_failure_codes != sorted(set(accepted_failure_codes))
            or accepted_failure_codes != expected_failure_codes
        ):
            raise SyntheticVisualError(f"visual reference route[{index}] has stale or incomplete accepted quality failures")
        effective_quality = expected_quality if not accepted_failure_codes else {
            **expected_quality,
            "status": "passed",
            "failures": [],
        }
        if route.get("quality") != effective_quality or effective_quality["status"] != "passed":
            raise SyntheticVisualError(f"visual reference route[{index}] lacks a passing quality gate")
    return manifest


def masked_image_similarity(first: Path, second: Path) -> float:
    try:
        from PIL import Image, ImageChops
        with Image.open(first).convert("RGB") as left, Image.open(second).convert("RGB") as right:
            if right.size != left.size:
                right = right.resize(left.size, Image.Resampling.BILINEAR)
            difference = ImageChops.difference(left, right)
            histogram = difference.histogram()
            total = max(left.width * left.height * 3 * 255, 1)
            distance = sum((index % 256) * count for index, count in enumerate(histogram)) / total
            return round(max(0.0, 1.0 - distance), 6)
    except Exception as exc:
        raise SyntheticVisualError(f"sanitized screenshot comparison failed: {exc}") from exc


def _ratio_similarity(left: float, right: float) -> float:
    if left == right == 0:
        return 1.0
    return max(0.0, 1.0 - abs(left - right) / max(abs(left), abs(right), 1e-9))


def _grid(geometry: dict[str, Any], category: str, size: int = 24) -> set[tuple[int, int]]:
    width = max(geometry["viewport"]["width"], 1)
    height = max(geometry["viewport"]["height"], 1)
    occupied: set[tuple[int, int]] = set()
    for item in geometry["elements"]:
        if item["category"] != category:
            continue
        x0 = max(0, min(size - 1, int(item["x"] / width * size)))
        y0 = max(0, min(size - 1, int(item["y"] / height * size)))
        x1 = max(x0 + 1, min(size, int((item["x"] + item["width"]) / width * size + 0.999)))
        y1 = max(y0 + 1, min(size, int((item["y"] + item["height"]) / height * size + 0.999)))
        occupied.update((x, y) for x in range(x0, x1) for y in range(y0, y1))
    return occupied


def _cell_regions(cells: set[tuple[int, int]], *, viewport: dict[str, int], size: int, kind: str, category: str) -> list[dict[str, Any]]:
    tile_size = 4
    components: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for cell in cells:
        components.setdefault((cell[0] // tile_size, cell[1] // tile_size), set()).add(cell)
    regions = []
    for component in sorted(components.values(), key=lambda value: (-len(value), min(value))):
        xs = [item[0] for item in component]
        ys = [item[1] for item in component]
        x0, x1 = min(xs), max(xs) + 1
        y0, y1 = min(ys), max(ys) + 1
        left = round(x0 / size * viewport["width"])
        top = round(y0 / size * viewport["height"])
        right = round(x1 / size * viewport["width"])
        bottom = round(y1 / size * viewport["height"])
        regions.append({
            "kind": kind,
            "category": category,
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
            "grid_cells": len(component),
        })
    return regions


def geometry_mismatch_regions(reference: dict[str, Any], candidate: dict[str, Any], *, size: int = 24, limit: int = 24) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for category in GEOMETRY_CATEGORIES:
        expected = _grid(reference, category, size)
        actual = _grid(candidate, category, size)
        regions.extend(_cell_regions(expected - actual, viewport=reference["viewport"], size=size, kind="missing", category=category))
        regions.extend(_cell_regions(actual - expected, viewport=reference["viewport"], size=size, kind="extra", category=category))
    return sorted(regions, key=lambda item: (-item["grid_cells"], item["kind"], item["category"], item["y"], item["x"]))[:limit]


def geometry_density_deltas(reference: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    expected = geometry_metrics(reference)
    actual = geometry_metrics(candidate)
    deltas = []
    for category in GEOMETRY_CATEGORIES:
        if expected["counts"][category] == actual["counts"][category] and expected["coverage"][category] == actual["coverage"][category]:
            continue
        deltas.append({
            "category": category,
            "reference_count": expected["counts"][category],
            "candidate_count": actual["counts"][category],
            "reference_union_coverage": expected["coverage"][category],
            "candidate_union_coverage": actual["coverage"][category],
        })
    return deltas


def compare_geometry(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, float]:
    geometry_scores = []
    for category in GEOMETRY_CATEGORIES:
        left, right = _grid(reference, category), _grid(candidate, category)
        geometry_scores.append(1.0 if not left and not right else len(left & right) / len(left | right))
    reference_landmarks = reference.get("landmarks") or {}
    candidate_landmarks = candidate.get("landmarks") or {}
    for name in sorted(set(reference_landmarks) | set(candidate_landmarks)):
        left = reference_landmarks.get(name)
        right = candidate_landmarks.get(name)
        if left is None or right is None:
            geometry_scores.append(0.0)
            continue
        intersection = _intersection_area(left, right)
        union = _rect_area(left) + _rect_area(right) - intersection
        geometry_scores.append(intersection / max(union, 1))
    ref_metrics = geometry_metrics(reference)
    candidate_metrics = geometry_metrics(candidate)
    density_parts = []
    for category in GEOMETRY_CATEGORIES:
        density_parts.append(_ratio_similarity(
            float(ref_metrics["counts"][category]), float(candidate_metrics["counts"][category])
        ))
        density_parts.append(_ratio_similarity(
            float(ref_metrics["coverage"][category]), float(candidate_metrics["coverage"][category])
        ))
    column_parts = [
        _ratio_similarity(float(left), float(right))
        for left, right in zip(ref_metrics["columns"], candidate_metrics["columns"])
    ]
    left_gaps, right_gaps = ref_metrics["spacing"], candidate_metrics["spacing"]
    length = max(len(left_gaps), len(right_gaps), 1)
    spacing_parts = [
        _ratio_similarity(
            float(left_gaps[index]) if index < len(left_gaps) else 0.0,
            float(right_gaps[index]) if index < len(right_gaps) else 0.0,
        )
        for index in range(length)
    ]
    return {
        "geometry": round(sum(geometry_scores) / len(geometry_scores), 6),
        "density": round(sum(density_parts) / len(density_parts), 6),
        "columns": round(sum(column_parts) / len(column_parts), 6),
        "spacing": round(sum(spacing_parts) / len(spacing_parts), 6),
    }
