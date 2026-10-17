"""Small dependency-free helpers for WebAnything Flask/SQLite replay sites."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote


def normalize_prefix(value: str | None) -> str:
    """Normalize a reverse-proxy prefix without accepting a URL or traversal."""
    if not value or value == "/":
        return ""
    if "://" in value or "?" in value or "#" in value:
        raise ValueError("proxy prefix must be a path")
    parts = [part for part in value.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("proxy prefix contains traversal")
    return "/" + "/".join(quote(part, safe="-._~") for part in parts)


def effective_prefix(headers: object, script_name: str | None = None) -> str:
    """Read the standard deployment prefix from a Flask-like headers object."""
    forwarded = headers.get("X-Forwarded-Prefix") if hasattr(headers, "get") else None
    return normalize_prefix(forwarded or script_name)


def scoped_path(prefix: str | None, path: str) -> str:
    """Join a normalized deployment prefix with one local absolute path."""
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("scoped path must be local and absolute")
    return f"{normalize_prefix(prefix)}{path}"


def scope_html_urls(html: str, prefix: str | None) -> str:
    """Scope local native HTML URL attributes exactly once.

    This intentionally recognises only native URL attributes.  In particular,
    matching a bare ``href`` substring would also match the suffix of
    ``data-webanything-auth-href`` and duplicate a reverse-proxy prefix.
    """
    normalized = normalize_prefix(prefix)
    if not normalized:
        return html

    pattern = re.compile(
        r"(?P<attribute>(?<![\w:-])(?:href|src|action|poster))"
        r"(?P<equals>\s*=\s*)(?P<quote>['\"])(?P<url>[^'\"]*)(?P=quote)",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        if not url.startswith("/") or url.startswith("//"):
            return match.group(0)
        if url == normalized or url.startswith(normalized + "/"):
            return match.group(0)
        return f'{match.group("attribute")}{match.group("equals")}{match.group("quote")}{normalized}{url}{match.group("quote")}'

    return pattern.sub(replace, html)


def atomic_seed_reset(seed_path: Path, runtime_path: Path) -> None:
    """Restore runtime SQLite bytes exactly from an immutable seed."""
    seed_path = seed_path.resolve()
    runtime_path = runtime_path.resolve()
    if not seed_path.is_file():
        raise FileNotFoundError(seed_path)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=runtime_path.parent, prefix=f".{runtime_path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        with seed_path.open("rb") as source:
            shutil.copyfileobj(source, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, runtime_path)


def health_payload(*, site_id: str, prefix: str | None) -> dict[str, object]:
    return {
        "status": "ok",
        "site_id": site_id,
        "build_mode": "benchmark_synthetic_replay",
        "proxy_prefix_aware": True,
        "effective_prefix": normalize_prefix(prefix),
    }
