#!/usr/bin/env python3
"""Create a deterministic, offline-safe CSS copy from a captured stylesheet."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


class LocalizationError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def rewrite_css(source: str, replacements: dict[str, str], allow_external: bool = False) -> tuple[str, dict[str, int]]:
    counts = {key: 0 for key in replacements}

    def replace(match: re.Match[str]) -> str:
        value = match.group(2)
        if value not in replacements:
            return match.group(0)
        counts[value] += 1
        return replacements[value]

    output = URL_RE.sub(replace, source)
    missing = [key for key, count in counts.items() if count == 0]
    if missing:
        raise LocalizationError(f"CSS replacements not found: {', '.join(missing)}")
    external = [
        match.group(2)
        for match in URL_RE.finditer(output)
        if match.group(2).startswith(("http://", "https://", "//"))
    ]
    if external and not allow_external:
        raise LocalizationError(f"localized CSS still has external URLs: {', '.join(sorted(set(external)))}")
    return output, counts


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    args = parser.parse_args(argv)
    root = repository_root()
    spec_path = args.spec.resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    source = (root / spec["input"]).resolve()
    output = (root / spec["output"]).resolve()
    site_root = (root / "sites" / spec["site"]).resolve()
    if site_root not in source.parents or site_root not in output.parents:
        raise LocalizationError("input and output must stay under the selected site")
    raw = source.read_bytes()
    actual_hash = sha256_bytes(raw)
    if actual_hash != spec["input_sha256"]:
        raise LocalizationError(f"captured CSS hash mismatch: {actual_hash}")
    localized, counts = rewrite_css(
        raw.decode("utf-8"), spec["replacements"], bool(spec.get("allow_external", False))
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(localized, encoding="utf-8")
    result = {
        "site": spec["site"],
        "input_sha256": actual_hash,
        "output": output.relative_to(root).as_posix(),
        "output_sha256": sha256_bytes(output.read_bytes()),
        "replacement_counts": counts,
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LocalizationError as exc:
        print(f"localization error: {exc}", file=sys.stderr)
        raise SystemExit(1)
