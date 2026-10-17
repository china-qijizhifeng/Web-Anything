#!/usr/bin/env python3
"""Compile a pinned historical benchmark DOM into a traceable SQLite replay."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageColor, ImageDraw

from compile_snapshot import (
    CompileError,
    SnapshotCompiler,
    catalog_port,
    repository_root,
    sha256_bytes,
    sha256_file,
    utc_now,
)


TOOL_VERSION = "webharbor-historical-geometry-compiler-v6"
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
SAFE_ATTRIBUTE_NAMES = {
    "class", "id", "name", "placeholder", "title", "type",
    "checked", "disabled", "multiple", "selected",
}
SAFE_SVG_ATTRIBUTE_NAMES = {
    "clip-path", "clip-rule", "cx", "cy", "d", "fill", "fill-rule",
    "font-family", "font-size", "font-style", "font-weight", "height",
    "marker-end", "marker-mid", "marker-start", "offset", "opacity",
    "pathlength", "points", "preserveaspectratio", "r", "rx", "ry", "stroke",
    "stroke-dasharray", "stroke-dashoffset", "stroke-linecap", "stroke-linejoin",
    "stroke-miterlimit", "stroke-opacity", "stroke-width", "transform", "viewbox",
    "text-anchor", "width", "x", "x1", "x2", "xmlns", "y", "y1", "y2",
}
SVG_ELEMENTS = {
    "circle", "clippath", "defs", "ellipse", "fecolormatrix", "fecomposite",
    "feflood", "fegaussianblur", "feoffset", "filter", "g", "image",
    "line", "lineargradient", "mask", "path", "polygon", "polyline", "rect",
    "stop", "svg", "symbol", "text", "tspan", "use",
}
SAFE_INLINE_STYLE_NAMES = {
    "background", "background-color", "border", "border-color", "border-radius",
    "color", "fill", "font-family", "font-size", "font-style", "font-weight",
    "line-height", "object-fit", "opacity", "overflow", "stroke", "text-align",
    "text-decoration", "text-overflow", "text-transform", "transform",
    "transform-origin", "visibility", "white-space",
    "overflow-x", "overflow-y",
}
IMAGE_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:icon|logo)(?:$|[\s_-])|(?:^|\s)sc-ir(?:\s|$)",
    re.IGNORECASE,
)
BACKGROUND_VISUAL_RE = re.compile(
    r"(?:^|[\s_-])(?:hero|banner|masthead|media|photo|image)(?:$|[\s_-])",
    re.IGNORECASE,
)
VISUAL_OVERLAY_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:gradient|overlay|mask|scrim|shade)(?:$|[\s_-])",
    re.IGNORECASE,
)
CLOSED_OVERLAY_RE = re.compile(
    r"(?:^|[\s_-])(?:modal|dialog|dropdown|popover|tooltip)(?:$|[\s_-])|__queue(?:$|\s)",
    re.IGNORECASE,
)
STRUCTURAL_SURFACE_RE = re.compile(
    r"(?:^|[\s_-])(?:main|page|panel|content|container|wrapper|shell|surface|canvas)"
    r"(?:$|[\s_-])",
    re.IGNORECASE,
)
SEMANTIC_SURFACE_RE = re.compile(
    r"(?:^|[\s_-])(?:header|nav|bar|panel|zone)(?:$|[\s_-])",
    re.IGNORECASE,
)
VISIBLE_STATE_TOKENS = {
    "active", "expanded", "is-active", "is-open", "m-active", "m-open",
    "m-visible", "open", "opened", "show", "shown", "visible",
}
FOREGROUND_MASK_TAGS = {
    "a", "button", "h1", "h2", "h3", "h4", "h5", "h6", "input",
    "label", "legend", "option", "p", "select", "textarea",
}
TEXT_CONTAINER_TAGS = {
    "a", "button", "caption", "cite", "dd", "dt", "figcaption", "h1",
    "h2", "h3", "h4", "h5", "h6", "label", "legend", "li", "option",
    "p", "summary", "td", "th",
}
NO_GEOMETRY_CONTAINER_TAGS = {
    "dialog", "form", "menu", "nav", "ol", "table", "ul",
}
SAFE_COLOR_SYNTAX_RE = re.compile(
    r"(?:#[0-9a-f]{3,4}|#[0-9a-f]{6}|#[0-9a-f]{8}|[a-z]{3,24}|"
    r"(?:rgb|rgba|hsl|hsla|hsv|hsva)\([-+0-9.,%/\s]+\))",
    re.IGNORECASE,
)


class HistoricalCompileError(CompileError):
    """Historical evidence cannot be converted into a trustworthy replay."""


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def key(self) -> tuple[int, int, int, int]:
        return tuple(round(value * 16) for value in (
            self.x, self.y, self.width, self.height
        ))

    def intersection(self, other: Rect) -> Rect | None:
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right - left, bottom - top)


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = None
    depth: int = 0

    @property
    def rect(self) -> Rect | None:
        raw = self.attrs.get("bounding_box_rect")
        if not raw:
            return None
        try:
            x, y, width, height = (float(value) for value in raw.split(","))
        except (TypeError, ValueError):
            return None
        values = (x, y, width, height)
        if not all(math.isfinite(value) for value in values):
            return None
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            return None
        return Rect(x, y, width, height)

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            parts.append(child.text() if isinstance(child, Node) else child)
        return " ".join(" ".join(parts).split())

    def direct_text(self) -> str:
        return " ".join(
            " ".join(child.split())
            for child in self.children
            if isinstance(child, str) and child.strip()
        ).strip()

    def descendants(self) -> list[Node]:
        output: list[Node] = []
        for child in self.children:
            if isinstance(child, Node):
                output.append(child)
                output.extend(child.descendants())
        return output


class HistoricalDomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], closed: bool) -> None:
        parent = self.stack[-1]
        node = Node(
            tag=tag.casefold(),
            attrs={key.casefold(): value or "" for key, value in attrs},
            parent=parent,
            depth=parent.depth + 1,
        )
        parent.children.append(node)
        if not closed and node.tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def format_px(value: float) -> str:
    rounded = round(value, 2)
    if rounded == int(rounded):
        return f"{int(rounded)}px"
    return f"{rounded:g}px"


def color_luminance(color: tuple[int, int, int]) -> float:
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


def color_distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def sampled_palette(
    image: Image.Image,
    rect: Rect,
    *,
    thumbnail_size: tuple[int, int] = (96, 96),
) -> list[tuple[int, tuple[int, int, int]]]:
    left = max(0, math.floor(rect.x))
    top = max(0, math.floor(rect.y))
    right = min(image.width, math.ceil(rect.right))
    bottom = min(image.height, math.ceil(rect.bottom))
    if right <= left or bottom <= top:
        return []
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    crop.thumbnail(thumbnail_size, Image.Resampling.LANCZOS)
    quantized = crop.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    raw_colors: list[tuple[int, tuple[int, int, int]]] = []
    for count, index in sorted(quantized.getcolors() or [], reverse=True):
        offset = index * 3
        raw_colors.append((count, tuple(palette[offset:offset + 3])))
    output: list[tuple[int, tuple[int, int, int]]] = []
    for count, color in raw_colors:
        for index, (existing_count, existing) in enumerate(output):
            if color_distance(color, existing) <= 18:
                total = existing_count + count
                merged = tuple(round(
                    (existing[channel] * existing_count + color[channel] * count) / total
                ) for channel in range(3))
                output[index] = (total, merged)
                break
        else:
            output.append((count, color))
    return sorted(output, reverse=True)


def palette_style(
    image: Image.Image,
    rect: Rect,
    *,
    text: bool,
) -> tuple[str | None, str | None, float]:
    palette = sampled_palette(
        image,
        rect,
        thumbnail_size=(512, 128) if text else (96, 96),
    )
    if not palette:
        return None, None, 0.0
    total = sum(count for count, _ in palette)
    dominant_count, background = palette[0]
    ratio = dominant_count / max(total, 1)
    foreground = None
    if text:
        if len(palette) > 1:
            fallback = max(
                (color for _, color in palette[1:]),
                key=lambda color: color_distance(color, background),
            )
            if color_distance(fallback, background) >= 24:
                foreground = fallback
        if foreground is None:
            foreground = (
                (245, 245, 245)
                if color_luminance(background) < 112
                else (51, 51, 51)
            )
    background_css = f"rgb{background}" if ratio >= 0.68 else None
    foreground_css = f"rgb{foreground}" if foreground is not None else None
    return background_css, foreground_css, ratio


def screenshot_corner_background(image: Image.Image) -> str | None:
    if image.width < 4 or image.height < 4:
        return None
    inset_x = max(1, min(12, image.width // 100))
    inset_y = max(1, min(12, image.height // 100))
    samples = [
        image.getpixel((inset_x, inset_y)),
        image.getpixel((image.width - inset_x - 1, inset_y)),
        image.getpixel((inset_x, image.height - inset_y - 1)),
        image.getpixel((image.width - inset_x - 1, image.height - inset_y - 1)),
    ]
    rgb_samples = [tuple(int(channel) for channel in sample[:3]) for sample in samples]
    best: list[tuple[int, int, int]] = []
    for candidate in rgb_samples:
        cluster = [color for color in rgb_samples if color_distance(color, candidate) <= 18]
        if len(cluster) > len(best):
            best = cluster
    if len(best) < 3:
        return None
    average = tuple(round(sum(color[channel] for color in best) / len(best)) for channel in range(3))
    return f"rgb{average}"


def crop_png(
    image: Image.Image,
    rect: Rect,
    *,
    allow_uniform: bool = False,
) -> tuple[bytes, tuple[int, int, int, int]] | None:
    left = max(0, math.floor(rect.x))
    top = max(0, math.floor(rect.y))
    right = min(image.width, math.ceil(rect.right))
    bottom = min(image.height, math.ceil(rect.bottom))
    if right <= left or bottom <= top:
        return None
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    palette = sampled_palette(image, rect)
    if not palette:
        return None
    total = sum(count for count, _ in palette)
    if not allow_uniform and palette[0][0] / max(total, 1) >= 0.995:
        return None
    output = io.BytesIO()
    crop.save(output, format="PNG", optimize=False)
    return output.getvalue(), (left, top, right - left, bottom - top)


def crop_background_png(
    image: Image.Image,
    node: Node,
) -> tuple[bytes, tuple[int, int, int, int], int] | None:
    rect = node.rect
    if rect is None:
        return None
    left = max(0, math.floor(rect.x))
    top = max(0, math.floor(rect.y))
    right = min(image.width, math.ceil(rect.right))
    bottom = min(image.height, math.ceil(rect.bottom))
    if right <= left or bottom <= top:
        return None
    crop = image.crop((left, top, right, bottom)).convert("RGBA")
    masked = 0
    draw = ImageDraw.Draw(crop)
    seen_regions: set[tuple[int, int, int, int]] = set()
    for descendant in node.descendants():
        if descendant.rect is None:
            continue
        has_bounded_foreground = (
            bool(descendant.direct_text())
            or (
                descendant.tag in FOREGROUND_MASK_TAGS
                and (
                    bool(descendant.text())
                    or descendant.tag in {"input", "select", "textarea"}
                )
            )
        )
        if not has_bounded_foreground:
            continue
        intersection = descendant.rect.intersection(rect)
        if (
            intersection is None
            or intersection.width * intersection.height > rect.width * rect.height * 0.75
        ):
            continue
        region = (
            max(0, math.floor(intersection.x - rect.x)),
            max(0, math.floor(intersection.y - rect.y)),
            min(crop.width, math.ceil(intersection.right - rect.x)),
            min(crop.height, math.ceil(intersection.bottom - rect.y)),
        )
        if region in seen_regions or region[2] <= region[0] or region[3] <= region[1]:
            continue
        seen_regions.add(region)
        draw.rectangle(region, fill=(0, 0, 0, 0))
        masked += 1
    alpha = crop.getchannel("A")
    opaque_bounds = alpha.getbbox()
    if opaque_bounds is None:
        return None
    histogram = alpha.histogram()
    opaque_pixels = sum(histogram[1:])
    if opaque_pixels / max(crop.width * crop.height, 1) < 0.05:
        return None
    output = io.BytesIO()
    crop.save(output, format="PNG", optimize=False)
    return output.getvalue(), (left, top, right - left, bottom - top), masked


def is_explicit_image(node: Node) -> bool:
    role = node.attrs.get("aria_role") or node.attrs.get("role")
    return node.tag in {"canvas", "img"} or (role or "").casefold() == "img"


def is_bounded_background_visual(node: Node) -> bool:
    rect = node.rect
    if rect is None:
        return False
    marker = " ".join((node.attrs.get("class", ""), node.attrs.get("id", "")))
    return (
        bool(BACKGROUND_VISUAL_RE.search(marker))
        and rect.width >= 320
        and rect.height >= 160
        and rect.width <= 2048
        and rect.height <= 1200
        and rect.width * rect.height <= 2_500_000
    )


def has_banner_ancestor(node: Node) -> bool:
    ancestor = node.parent
    while ancestor is not None:
        role = (
            ancestor.attrs.get("aria_role")
            or ancestor.attrs.get("role")
            or ""
        ).casefold()
        if ancestor.tag == "header" or role == "banner":
            return True
        ancestor = ancestor.parent
    return False


def is_semantic_brand_visual(node: Node) -> bool:
    rect = node.rect
    return (
        rect is not None
        and node.tag == "a"
        and has_banner_ancestor(node)
        and bool(node.text())
        and 80 <= rect.width <= 400
        and 50 <= rect.height <= 200
    )


def is_visual_candidate(node: Node) -> bool:
    rect = node.rect
    if rect is None:
        return False
    if is_explicit_image(node):
        # A real desktop hero image is a bounded HTML visual element, even when
        # it spans the viewport. Keep it below full-page dimensions so a page
        # screenshot cannot become the replay background.
        return (
            rect.width <= 2048
            and rect.height <= 1200
            and rect.width * rect.height <= 2_500_000
        )
    if is_bounded_background_visual(node):
        return True
    if is_semantic_brand_visual(node):
        return True
    if rect.width > 900 or rect.height > 900 or rect.width * rect.height > 500_000:
        return False
    classes = node.attrs.get("class", "")
    if IMAGE_CLASS_RE.search(classes) and rect.width <= 320 and rect.height <= 320:
        return True
    clickable = node.attrs.get("is_clickable", "").casefold() == "true"
    return (
        clickable
        and not node.text()
        and rect.width <= 80
        and rect.height <= 80
    )


def visual_nodes(nodes: list[Node]) -> list[Node]:
    candidates = [node for node in nodes if is_visual_candidate(node)]
    groups: dict[tuple[int, int, int, int], list[Node]] = {}
    for node in candidates:
        groups.setdefault(node.rect.key(), []).append(node)  # type: ignore[union-attr]
    selected = []
    for group in groups.values():
        selected.append(max(
            group,
            key=lambda node: (
                is_explicit_image(node),
                is_bounded_background_visual(node),
                bool(IMAGE_CLASS_RE.search(node.attrs.get("class", ""))),
                node.depth,
            ),
        ))
    return sorted(selected, key=lambda node: (node.rect.y, node.rect.x, node.depth))  # type: ignore[union-attr]


def has_svg_ancestor(node: Node) -> bool:
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.tag == "svg":
            return True
        ancestor = ancestor.parent
    return False


def is_svg_generated_text_leaf(node: Node) -> bool:
    return (
        node.tag == "text"
        and node.parent is not None
        and node.parent.tag in {"text", "tspan"}
        and has_svg_ancestor(node)
    )


def html_tag(node: Node) -> str:
    if node.tag == "text" and not has_svg_ancestor(node):
        return "span"
    if re.fullmatch(r"[a-z][a-z0-9-]*", node.tag):
        return node.tag
    return "div"


def class_indicates_closed_overlay(classes: str) -> bool:
    tokens = set(classes.casefold().split())
    if "dialog-off-canvas-main-canvas" in tokens:
        return False
    return bool(CLOSED_OVERLAY_RE.search(classes)) and not (tokens & VISIBLE_STATE_TOKENS)


def node_indicates_closed_overlay(node: Node) -> bool:
    classes = node.attrs.get("class", "")
    if class_indicates_closed_overlay(classes):
        return True
    role = (node.attrs.get("aria_role") or node.attrs.get("role") or "").casefold()
    if role not in {"dialog", "alertdialog"}:
        if role == "region" and node.parent is not None:
            return any(
                isinstance(sibling, Node)
                and sibling.tag == "input"
                and sibling.attrs.get("type", "").casefold() == "checkbox"
                and "checked" not in sibling.attrs
                for sibling in node.parent.children
            )
        return False
    tokens = set(classes.casefold().split())
    explicitly_open = (
        "open" in node.attrs
        or node.attrs.get("aria-modal", node.attrs.get("aria_modal", "")).casefold() == "true"
        or node.attrs.get("aria-expanded", node.attrs.get("aria_expanded", "")).casefold() == "true"
        or bool(tokens & VISIBLE_STATE_TOKENS)
    )
    return not explicitly_open


def class_indicates_visual_overlay(classes: str) -> bool:
    return bool(VISUAL_OVERLAY_CLASS_RE.search(classes))


def lacks_rendered_geometry(node: Node) -> bool:
    return (
        node.rect is None
        and node.tag in NO_GEOMETRY_CONTAINER_TAGS
        and not any(descendant.rect is not None for descendant in node.descendants())
    )


def safe_inline_style(value: str) -> list[str]:
    output: list[str] = []
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        name, raw_value = declaration.split(":", 1)
        name = name.strip().casefold()
        raw_value = raw_value.strip()
        if not raw_value or not re.fullmatch(r"--[a-z0-9_-]+|[a-z-]+", name):
            continue
        lowered = raw_value.casefold()
        if any(token in lowered for token in ("url(", "expression(", "@import", "javascript:")):
            continue
        if (
            name.startswith("--")
            or name in SAFE_INLINE_STYLE_NAMES
            or (name in {"height", "width"} and raw_value.casefold() in {"0", "0px"})
        ):
            output.append(f"{name}:{raw_value}")
    return output


def safe_css_color(value: str) -> str | None:
    candidate = value.strip()
    if not candidate or not SAFE_COLOR_SYNTAX_RE.fullmatch(candidate):
        return None
    try:
        channels = ImageColor.getrgb(candidate)
    except ValueError:
        return None
    if len(channels) == 3:
        return f"rgb({channels[0]},{channels[1]},{channels[2]})"
    red, green, blue, alpha = channels
    if alpha == 255:
        return f"rgb({red},{green},{blue})"
    alpha_css = f"{alpha / 255:.3f}".rstrip("0").rstrip(".")
    return f"rgba({red},{green},{blue},{alpha_css})"


def html_page_background(nodes: list[Node]) -> tuple[str | None, str | None]:
    for node in nodes:
        if (
            node.tag == "meta"
            and node.attrs.get("name", "").casefold() == "theme-color"
        ):
            color = safe_css_color(node.attrs.get("content", ""))
            if color:
                return color, "html_meta_theme_color"

    for tag in ("body", "html"):
        for node in nodes:
            if node.tag != tag:
                continue
            for declaration in safe_inline_style(node.attrs.get("style", "")):
                name, raw_value = declaration.split(":", 1)
                if name not in {"background", "background-color"}:
                    continue
                color = safe_css_color(raw_value)
                if color:
                    return color, f"html_{tag}_inline_{name.replace('-', '_')}"
    return None, None


def safe_link_href(value: str) -> str | None:
    value = value.strip()
    if not value or value.startswith("//"):
        return None
    parsed = urlsplit(value)
    if value.startswith(("/", "#", "?")) or parsed.scheme in {"http", "https"}:
        return value
    return None


def safe_attributes(node: Node, tag: str, asset_url: str | None) -> list[tuple[str, str | None]]:
    output: list[tuple[str, str | None]] = []
    for key in sorted(SAFE_ATTRIBUTE_NAMES):
        if key not in node.attrs:
            continue
        value = node.attrs[key]
        output.append((key, None if key in {"checked", "disabled", "multiple", "selected"} else value))
    if tag in SVG_ELEMENTS:
        for key in sorted(SAFE_SVG_ATTRIBUTE_NAMES):
            if key in node.attrs:
                output.append((key, node.attrs[key]))
        if tag == "use" and node.attrs.get("href", "").startswith("#"):
            output.append(("href", node.attrs["href"]))
    role = node.attrs.get("aria_role") or node.attrs.get("role")
    if role:
        output.append(("role", role))
    aria_label = node.attrs.get("aria_label") or node.attrs.get("aria-label")
    if aria_label:
        output.append(("aria-label", aria_label))
    if tag == "input" and "value" not in node.attrs and node.attrs.get("input_value"):
        output.append(("value", node.attrs["input_value"]))
    if tag == "a":
        href = safe_link_href(node.attrs.get("href", ""))
        if href:
            output.append(("href", href))
        elif node.attrs.get("is_clickable", "").casefold() == "true":
            output.append(("href", "#"))
    if tag == "form":
        action = node.attrs.get("action", "/")
        if not action.startswith("/") or action.startswith("//"):
            action = "/"
        output.extend((("method", "get"), ("action", action)))
    node_id = node.attrs.get("backend_node_id") or node.attrs.get("data-webtasks-id")
    if node_id:
        output.append(("data-historical-node", node_id))
    if node.attrs.get("is_clickable", "").casefold() == "true":
        output.append(("data-historical-clickable", "true"))
    if asset_url and tag == "img":
        output.append(("src", asset_url))
        output.append(("alt", node.attrs.get("aria_label") or node.attrs.get("title") or ""))
    return output


BASE_STYLE = """<style data-webharbor-historical-layout>
html,body{margin:0;padding:0;width:100%;min-width:1280px;letter-spacing:0}
body{position:relative;font-family:Arial,Helvetica,sans-serif;color:#333;background:#f2f2f2}
*,*::before,*::after{box-sizing:border-box;letter-spacing:0}
[data-historical-bbox]{position:absolute;margin:0;padding:0}
h1,h2,h3,h4,h5,h6{font:inherit}
cite{font-style:normal}ul,ol{list-style:none}button,input,select,textarea{font:inherit;color:inherit}
button{border:0;background:transparent}a{color:inherit;text-decoration:none}
img{display:block;object-fit:fill}span[role="img"]{display:block;background-repeat:no-repeat;background-size:100% 100%}
</style>"""


def geometry_font_size(node: Node, rect: Rect, text_value: str) -> int:
    classes = node.attrs.get("class", "").casefold()
    tag = html_tag(node)
    if len(text_value) == 1 and rect.width <= rect.height:
        return max(8, min(22, round(rect.height * 0.72)))
    leaf_text = node.direct_text() and not any(
        isinstance(child, Node) for child in node.children
    )
    if node.tag == "text" or leaf_text:
        if rect.height <= 18:
            return max(8, min(32, round(rect.height * 0.72)))
        if rect.height <= 40:
            return max(8, min(32, round(rect.height * 0.75)))
    scale = 0.36 if rect.height >= 48 else 0.48
    cap = 28 if tag in {"h1", "h2", "h3", "strong"} or "title" in classes else 24
    return max(8, min(cap, round(rect.height * scale)))


def styled_text_value(node: Node) -> str:
    direct = node.direct_text()
    if direct:
        return direct
    role = (node.attrs.get("aria_role") or node.attrs.get("role") or "").casefold()
    if node.tag in TEXT_CONTAINER_TAGS or role == "heading":
        return node.text()
    return ""


def direct_text_fragment_count(node: Node) -> int:
    return sum(
        1
        for child in node.children
        if isinstance(child, Node) and child.tag == "text" and child.text()
    )


def descendant_text_rect(node: Node) -> Rect | None:
    rects = [
        descendant.rect
        for descendant in node.descendants()
        if descendant.rect is not None and descendant.text()
    ]
    if not rects:
        return None
    left = min(rect.x for rect in rects)
    top = min(rect.y for rect in rects)
    right = max(rect.right for rect in rects)
    bottom = max(rect.bottom for rect in rects)
    return Rect(left, top, right - left, bottom - top)


def has_padded_text_content(node: Node) -> bool:
    rect = node.rect
    text_rect = descendant_text_rect(node)
    if rect is None or text_rect is None:
        return False
    return (
        rect.width - text_rect.width >= 8
        or rect.height - text_rect.height >= 6
    )


def has_typographic_emphasis(node: Node) -> bool:
    current: Node | None = node
    while current is not None:
        classes = set(current.attrs.get("class", "").casefold().split())
        if (
            current.tag in {"b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"}
            or classes & {"h1", "h2", "h3", "heading", "headline", "title"}
        ):
            return True
        current = current.parent
    return False


def structural_surface_color(
    image: Image.Image,
    node: Node,
) -> tuple[str | None, float]:
    rect = node.rect
    if rect is None:
        return None, 0.0
    marker = " ".join((node.attrs.get("id", ""), node.attrs.get("class", "")))
    if node.tag not in {"article", "main"} and not STRUCTURAL_SURFACE_RE.search(marker):
        return None, 0.0
    visible = rect.intersection(Rect(0, 0, image.width, image.height))
    if (
        visible is None
        or visible.width < image.width * 0.4
        or visible.height < image.height * 0.5
    ):
        return None, 0.0
    palette = sampled_palette(image, visible)
    if not palette:
        return None, 0.0
    total = sum(count for count, _ in palette)
    count, color = palette[0]
    ratio = count / max(total, 1)
    if ratio < 0.55:
        return None, ratio
    return f"rgb{color}", ratio


def semantic_surface_color(
    image: Image.Image,
    node: Node,
) -> tuple[str | None, float]:
    rect = node.rect
    role = (node.attrs.get("aria_role") or node.attrs.get("role") or "").casefold()
    marker = " ".join((node.attrs.get("id", ""), node.attrs.get("class", "")))
    semantic_role = node.tag in {"header", "nav"} or role in {"banner", "navigation"}
    semantic_marker = bool(SEMANTIC_SURFACE_RE.search(marker))
    if rect is None or not (semantic_role or semantic_marker):
        return None, 0.0
    visible = rect.intersection(Rect(0, 0, image.width, image.height))
    if visible is None:
        return None, 0.0
    palette = sampled_palette(image, visible)
    if not palette:
        return None, 0.0
    total = sum(count for count, _ in palette)
    count, color = palette[0]
    ratio = count / max(total, 1)
    minimum_ratio = 0.3 if semantic_role else 0.5
    if ratio < minimum_ratio:
        return None, ratio
    return f"rgb{color}", ratio


class HistoricalLayout:
    def __init__(
        self,
        root: Node,
        screenshot: Image.Image,
        *,
        upstream_url: str,
        display_name: str,
    ) -> None:
        self.root = root
        self.screenshot = screenshot.convert("RGB")
        self.upstream_url = upstream_url
        self.display_name = display_name
        self.nodes = root.descendants()
        self.page_background, self.page_background_source = html_page_background(
            self.nodes
        )
        if self.page_background is None:
            corner_background = screenshot_corner_background(self.screenshot)
            screenshot_background = corner_background
            if corner_background is None:
                screenshot_background, _, _ = palette_style(
                    self.screenshot,
                    Rect(0, 0, self.screenshot.width, self.screenshot.height),
                    text=False,
                )
            if screenshot_background:
                self.page_background = screenshot_background
                self.page_background_source = (
                    "reference_screenshot_corner_palette"
                    if corner_background is not None
                    else "reference_screenshot_dominant_palette"
                )
            else:
                self.page_background = "rgb(242,242,242)"
                self.page_background_source = "neutral_fallback"
        self.asset_urls: dict[Node, str] = {}
        self.asset_modes: dict[Node, str] = {}
        self.complex_background_nodes: set[Node] = set()
        self.assets: list[dict[str, Any]] = []
        self.visual_candidates = visual_nodes(self.nodes)
        self.text_elements = 0
        self.positioned_elements = 0
        self.palette_calibrations = 0
        self.hidden_subtrees = 0
        self.invalid_geometry_nodes = 0
        self.clipped_containers = 0
        self.missing_explicit_image_fallbacks = 0

    def extract_assets(self, asset_dir: Path, local_prefix: str) -> None:
        """Keep screenshots as references only; never turn their pixels into assets."""
        asset_dir.mkdir(parents=True, exist_ok=True)

    def has_complex_background_ancestor(self, node: Node) -> bool:
        ancestor = node.parent
        while ancestor is not None:
            if ancestor in self.complex_background_nodes:
                return True
            ancestor = ancestor.parent
        return False

    def node_style(self, node: Node, parent_rect: Rect | None) -> str | None:
        rect = node.rect
        if rect is None:
            return None
        self.positioned_elements += 1
        left = rect.x - (parent_rect.x if parent_rect else 0)
        top = rect.y - (parent_rect.y if parent_rect else 0)
        styles = [
            f"left:{format_px(left)}", f"top:{format_px(top)}",
            f"width:{format_px(rect.width)}", f"height:{format_px(rect.height)}",
        ]
        text_value = styled_text_value(node)
        classes = node.attrs.get("class", "").casefold()
        role = (node.attrs.get("aria_role") or node.attrs.get("role") or "").casefold()
        visual_overlay = class_indicates_visual_overlay(classes)
        sample_rect = rect.intersection(parent_rect) if parent_rect else rect
        sample_rect = sample_rect or rect
        background, foreground, background_ratio = palette_style(
            self.screenshot, sample_rect, text=bool(text_value)
        )
        surface_background, surface_ratio = structural_surface_color(
            self.screenshot, node
        )
        semantic_background, semantic_ratio = semantic_surface_color(
            self.screenshot, node
        )
        if node in self.asset_urls:
            if html_tag(node) != "img":
                styles.extend((
                    f"background-image:url('{self.asset_urls[node]}')",
                    "background-size:100% 100%",
                    "background-repeat:no-repeat",
                ))
            styles.append("overflow:hidden")
            if "rounded" in node.attrs.get("class", "").casefold():
                styles.append("border-radius:50%")
        elif (
            (background or surface_background or semantic_background)
            and not visual_overlay
            and not self.has_complex_background_ancestor(node)
            and (
                node.tag in {"header", "footer", "input", "select", "textarea"}
                or role == "banner"
                or (
                    node.tag in {"a", "button", "nav"}
                    and has_padded_text_content(node)
                )
                or surface_background is not None
            )
        ):
            styles.append(
                f"background-color:{surface_background or semantic_background or background}"
            )
            self.palette_calibrations += 1
        if text_value:
            self.text_elements += 1
            text_fragments = direct_text_fragment_count(node)
            if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and rect.height >= 60:
                line_count = max(1, text_fragments)
                line_height = rect.height / line_count
                font_size = max(18, min(112, round(line_height * 0.7)))
            else:
                line_height = rect.height
                font_size = geometry_font_size(node, rect, text_value)
            styles.extend((
                f"font-size:{font_size}px",
                f"line-height:{format_px(line_height)}",
                "white-space:normal" if text_fragments > 1 else "white-space:nowrap",
                "overflow:hidden",
            ))
            if foreground:
                styles.append(f"color:{foreground}")
                self.palette_calibrations += 1
            classes = node.attrs.get("class", "").casefold()
            if has_typographic_emphasis(node):
                styles.append("font-weight:600")
            if "sc-truncate" in classes:
                styles.append("text-overflow:ellipsis")
        if "image__rounded" in classes or "avatar" in classes:
            styles.append("border-radius:50%")
        if role == "banner" or node.tag == "header":
            styles.append("z-index:100")
        if html_tag(node) == "input":
            styles.extend(("border:0", "padding:0 8px", "outline:0"))
        if any(
            isinstance(child, Node)
            and child.rect is not None
            and (
                child.rect.x < rect.x - 1
                or child.rect.y < rect.y - 1
                or child.rect.right > rect.right + 1
                or child.rect.bottom > rect.bottom + 1
            )
            for child in node.children
        ):
            styles.append("overflow:hidden")
            self.clipped_containers += 1
        styles.extend(safe_inline_style(node.attrs.get("style", "")))
        return ";".join(styles)

    def render_node(self, node: Node, parent_rect: Rect | None) -> str:
        if is_svg_generated_text_leaf(node):
            return "".join(
                self.render_node(child, parent_rect)
                if isinstance(child, Node)
                else html.escape(" ".join(child.split()))
                for child in node.children
                if not isinstance(child, str) or child.strip()
            )
        tag = html_tag(node)
        missing_explicit_image = tag == "img" and node not in self.asset_urls
        if missing_explicit_image:
            tag = "span"
            self.missing_explicit_image_fallbacks += 1
        rect = node.rect
        classes = node.attrs.get("class", "").casefold()
        if (
            "visuallyhidden" in classes
            or "visually-hidden" in classes
            or "hidden" in node.attrs
            or node.attrs.get("aria_hidden", "").casefold() == "true"
            or node_indicates_closed_overlay(node)
            or lacks_rendered_geometry(node)
        ):
            self.hidden_subtrees += 1
            return ""
        invalid_geometry = "bounding_box_rect" in node.attrs and rect is None
        if invalid_geometry:
            self.invalid_geometry_nodes += 1
        next_parent = rect or parent_rect
        attributes = safe_attributes(node, tag, self.asset_urls.get(node))
        if missing_explicit_image:
            if not any(key == "role" for key, _ in attributes):
                attributes.append(("role", "img"))
            label = (
                node.attrs.get("aria_label")
                or node.attrs.get("aria-label")
                or node.attrs.get("alt")
                or node.attrs.get("title")
            )
            if label and not any(key == "aria-label" for key, _ in attributes):
                attributes.append(("aria-label", label))
        style = (
            None
            if invalid_geometry or has_svg_ancestor(node)
            else self.node_style(node, parent_rect)
        )
        if style:
            attributes.append(("data-historical-bbox", "true"))
            attributes.append(("style", style))
        elif tag not in {"html", "body", "style"} and tag not in SVG_ELEMENTS:
            inline_style = safe_inline_style(node.attrs.get("style", ""))
            attributes.append((
                "style",
                ";".join(inline_style) if inline_style else "display:contents",
            ))
        rendered_attrs = "".join(
            f" {key}" if value is None else f' {key}="{html.escape(value, quote=True)}"'
            for key, value in attributes
        )
        if tag in VOID_ELEMENTS:
            return f"<{tag}{rendered_attrs}>"
        children = []
        replace_children_with_crop = self.asset_modes.get(node) == "replace"
        for child in node.children:
            if isinstance(child, Node) and not replace_children_with_crop:
                children.append(self.render_node(child, next_parent))
            elif isinstance(child, str) and child.strip() and not invalid_geometry and not replace_children_with_crop:
                children.append(html.escape(" ".join(child.split())))
        return f"<{tag}{rendered_attrs}>{''.join(children)}</{tag}>"

    def render(self) -> str:
        body = next((node for node in self.nodes if node.tag == "body"), None)
        content = body.children if body else self.root.children
        rendered = []
        for child in content:
            if isinstance(child, Node):
                rendered.append(self.render_node(child, None))
            elif child.strip():
                rendered.append(html.escape(" ".join(child.split())))
        height = max(
            [self.screenshot.height]
            + [math.ceil(node.rect.bottom) for node in self.nodes if node.rect]
        )
        style = BASE_STYLE.replace(
            "min-width:1280px",
            f"min-width:{self.screenshot.width}px",
        ).replace(
            "background:#f2f2f2",
            f"background:{self.page_background}",
        ).replace(
            "body{position:relative;",
            f"body{{position:relative;min-height:{height}px;",
        )
        return (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width={self.screenshot.width}\">"
            f"<title>{html.escape(self.display_name)}</title>{style}</head>"
            f"<body>{''.join(rendered)}</body></html>"
        )


class HistoricalCompiler:
    def __init__(self, root: Path, site: str, evidence_path: Path) -> None:
        self.root = root.resolve()
        self.site = site
        self.evidence_path = evidence_path.resolve()
        self.site_dir = self.root / "sites" / site
        self.capture_root = self.site_dir / "static" / "external_cache" / "upstream"
        self.evidence = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        if self.evidence.get("site") != site or self.evidence.get("status") != "passed":
            raise HistoricalCompileError("historical evidence must pass for the requested site")
        if self.evidence.get("source_mode") != "historical_benchmark_snapshot":
            raise HistoricalCompileError("historical evidence has the wrong source mode")
        if len(self.evidence.get("pages", [])) != 1:
            raise HistoricalCompileError("historical evidence must contain exactly one page")

    def verified_page(self) -> tuple[dict[str, Any], bytes]:
        record = self.evidence["pages"][0]
        path = (self.capture_root / record["local_path"]).resolve()
        if self.capture_root.resolve() not in path.parents or not path.is_file():
            raise HistoricalCompileError("historical HTML is missing or outside capture cache")
        body = path.read_bytes()
        if sha256_bytes(body) != record.get("sha256"):
            raise HistoricalCompileError("historical HTML hash changed")
        return record, body

    def verified_screenshot(self) -> Image.Image:
        record = self.evidence["screenshot"]
        path = (self.root / record["local_path"]).resolve()
        cache = (self.site_dir / "static" / "external_cache").resolve()
        if cache not in path.parents or not path.is_file():
            raise HistoricalCompileError("historical screenshot is missing or outside site cache")
        if sha256_file(path) != record.get("sha256"):
            raise HistoricalCompileError("historical screenshot hash changed")
        image = Image.open(path).convert("RGB")
        if image.size != (record.get("width"), record.get("height")):
            raise HistoricalCompileError("historical screenshot dimensions changed")
        return image

    def compile(
        self,
        *,
        display_name: str | None,
        port: int,
        replace: bool = False,
    ) -> dict[str, Any]:
        page_record, source_html = self.verified_page()
        screenshot = self.verified_screenshot()
        parser = HistoricalDomParser()
        parser.feed(source_html.decode("utf-8", errors="replace"))
        parser.close()
        nodes = parser.root.descendants()
        if sum(node.rect is not None for node in nodes) < 20:
            raise HistoricalCompileError("historical DOM lacks enough usable geometry")

        display_name = display_name or self.site.replace("_", " ").title()
        derived_root = self.capture_root / "historical" / "derived"
        if derived_root.exists():
            shutil.rmtree(derived_root)
        page_dir = derived_root / "pages"
        asset_dir = derived_root / "assets"
        page_dir.mkdir(parents=True, exist_ok=True)
        layout = HistoricalLayout(
            parser.root,
            screenshot,
            upstream_url=self.evidence["scope"]["upstream_url"],
            display_name=display_name,
        )
        asset_prefix = "historical/derived/assets"
        layout.extract_assets(asset_dir, asset_prefix)
        rendered = layout.render().encode("utf-8")
        page_path = page_dir / "index.html"
        page_path.write_bytes(rendered)

        historical_hash = sha256_file(self.evidence_path)
        derived_evidence = {
            "schema_version": 1,
            "tool": TOOL_VERSION,
            "phase": "historical_derived_source",
            "site": self.site,
            "generated_at": self.evidence.get("generated_at"),
            "status": "passed",
            "source_mode": "historical_benchmark_snapshot",
            "historical_source_evidence_sha256": historical_hash,
            "authorization": self.evidence.get("authorization"),
            "scope": self.evidence["scope"],
            "pages": [{
                "bytes": len(rendered),
                "content_type": "text/html; charset=utf-8",
                "final_url": page_record["final_url"],
                "local_path": "historical/derived/pages/index.html",
                "sha256": sha256_bytes(rendered),
                "source_page_sha256": page_record.get("source_page_sha256", page_record["sha256"]),
                "status": 200,
                "url": page_record["url"],
            }],
            "assets": layout.assets,
            "asset_failures": [],
            "optional_page_failures": [],
            "failures": [],
            "geometry": {
                "bbox_elements": sum(node.rect is not None for node in nodes),
                "clipped_containers": layout.clipped_containers,
                "design_source": "historical_html_structure_classes_roles_text_and_geometry",
                "hidden_subtrees": layout.hidden_subtrees,
                "invalid_geometry_nodes": layout.invalid_geometry_nodes,
                "missing_explicit_image_fallbacks": layout.missing_explicit_image_fallbacks,
                "palette_calibrations": layout.palette_calibrations,
                "page_background": layout.page_background,
                "page_background_source": layout.page_background_source,
                "positioned_elements": layout.positioned_elements,
                "reference_height": screenshot.height,
                "reference_width": screenshot.width,
                "text_elements": layout.text_elements,
                "visual_candidates": len(layout.visual_candidates),
                "screenshot_crop_assets": 0,
                "visual_crops": 0,
            },
            "limitations": [
                "HTML structure, classes, semantic roles, text, controls, and bounding geometry are the primary design source",
                "the reference screenshot is used only for comparison and palette calibration where HTML has no CSS value",
                "layout is reconstructed from historical element geometry rather than current production CSS",
                "no screenshot pixels are served as images, logos, icons, heroes, or backgrounds",
                "missing original visual assets remain semantic role=img fallbacks until an original or disclosed high-resolution replacement is supplied",
                "the full reference screenshot is not served by the mirror",
                "historical task actions beyond the observed static DOM require separate reconstruction",
            ],
        }
        derived_path = self.root / "mirror_factory" / "evidence" / self.site / "historical-derived.json"
        derived_path.parent.mkdir(parents=True, exist_ok=True)
        derived_path.write_text(
            json.dumps(derived_evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        result = SnapshotCompiler(self.root, self.site, derived_path).compile(
            display_name=display_name,
            port=port,
            replace=replace,
        )
        result.update({
            "tool": TOOL_VERSION,
            "phase": "historical_snapshot_reconstruction",
            "source_mode": "historical_benchmark_snapshot",
            "historical_source_evidence_sha256": historical_hash,
            "derived_source_evidence": {
                "path": derived_path.relative_to(self.root).as_posix(),
                "sha256": sha256_file(derived_path),
            },
            "replay_compiler": result["tool"],
            "geometry": derived_evidence["geometry"],
            "publication": "not_authorized",
            "generated_at": utc_now(),
        })
        result["workflow_gaps"]["unsupported"].extend(derived_evidence["limitations"])
        (self.site_dir / "workflow_gaps.json").write_text(
            json.dumps(result["workflow_gaps"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--display-name")
    parser.add_argument("--port", type=int)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--output-evidence", type=Path)
    args = parser.parse_args(argv)
    root = repository_root()
    evidence = args.evidence or root / "mirror_factory" / "evidence" / args.site / "historical-source.json"
    result = HistoricalCompiler(root, args.site, evidence).compile(
        display_name=args.display_name,
        port=args.port or catalog_port(root, args.site),
        replace=args.replace,
    )
    output = args.output_evidence or root / "mirror_factory" / "evidence" / args.site / "reconstruction.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "site": args.site,
        "status": result["status"],
        "source_mode": result["source_mode"],
        "pages": result["pages"],
        "assets": result["assets"],
        "tasks": result["tasks"],
        "evidence": str(output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HistoricalCompileError as exc:
        print(f"historical compile error: {exc}", file=sys.stderr)
        raise SystemExit(2)
