"""Deterministic identity for the code and shipped assets of one mirror site."""

from __future__ import annotations

import hashlib
from pathlib import Path

EXCLUDED_DIRS = {"instance", "__pycache__", ".pytest_cache"}
EXCLUDED_RELATIVE_PREFIXES = {"static/external_cache/fidelity"}


def site_implementation_fingerprint(site_root: Path) -> dict[str, object]:
    site_root = site_root.resolve()
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for path in sorted(site_root.rglob("*")):
        relative = path.relative_to(site_root).as_posix()
        if any(part in EXCLUDED_DIRS for part in Path(relative).parts):
            continue
        if any(
            relative == prefix or relative.startswith(prefix + "/")
            for prefix in EXCLUDED_RELATIVE_PREFIXES
        ):
            continue
        if path.is_symlink():
            payload = ("symlink:" + path.readlink().as_posix()).encode("utf-8")
        elif path.is_file():
            payload = path.read_bytes()
        else:
            continue
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        files += 1
        total_bytes += len(payload)
    return {
        "algorithm": "webharbor-site-tree-v1",
        "sha256": digest.hexdigest(),
        "files": files,
        "bytes": total_bytes,
    }
