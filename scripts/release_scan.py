#!/usr/bin/env python3
"""Fail closed on private, generated, or machine-specific release material."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
FORBIDDEN_PARTS = {"generated", "private-upstream-screenshots", "auth-profiles", "stage-logs"}
MACHINE_PATTERNS = (
    re.compile(r"/cuaxietianci/"),
    re.compile(r"/Users/xietianci/"),
    re.compile(r"my-server-via-jms"),
    re.compile(r"[a-z0-9]+\.hk-prod-e2b\.xiaobei\.top"),
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [^-]+PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|ghp|github_pat|xox[baprs]|AKIA)[-_][A-Za-z0-9._-]{16,}"),
)
LABELED_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|password)"
    r"\s*[:=]\s*['\"]?(?P<value>[A-Za-z0-9._~+/=-]{20,})"
)
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".sh", ".md", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".txt", ".html", ".css", ".ini", ".cfg",
}


def scan(root: Path) -> dict:
    failures: list[dict[str, str]] = []
    files = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        lowered_parts = tuple(part.casefold() for part in relative.parts)
        forbidden_path = any(part in FORBIDDEN_PARTS for part in lowered_parts) or any(
            marker in relative.name.casefold()
            for marker in ("storage-state", "storage_state", "cookies.json", "auth-profile", "encrypted-auth-profile")
        )
        if path.is_dir():
            if forbidden_path:
                failures.append({"path": relative.as_posix(), "reason": "forbidden release directory"})
            continue
        if not path.is_file():
            continue
        files += 1
        if forbidden_path:
            failures.append({"path": relative.as_posix(), "reason": "forbidden release file"})
        if path.suffix.casefold() not in TEXT_SUFFIXES and path.name not in {"Dockerfile", "LICENSE"}:
            continue
        if relative.as_posix() == "scripts/release_scan.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in MACHINE_PATTERNS:
            if pattern.search(text):
                failures.append({"path": relative.as_posix(), "reason": f"machine-specific reference: {pattern.pattern}"})
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(0).casefold()
                if any(marker in value for marker in ("-test-", "dummy", "example", "fake", "placeholder")):
                    continue
                failures.append({"path": relative.as_posix(), "reason": "possible credential material"})
        for match in LABELED_SECRET.finditer(text):
            candidate = match.group("value")
            lowered = candidate.casefold()
            if any(marker in lowered for marker in ("process.env", "os.environ", "getenv", "redacted", "temporary", "placeholder", "example", "dummy", "fake", "test")):
                continue
            classes = sum((
                any(character.islower() for character in candidate),
                any(character.isupper() for character in candidate),
                any(character.isdigit() for character in candidate),
                any(not character.isalnum() for character in candidate),
            ))
            if classes >= 3:
                failures.append({"path": relative.as_posix(), "reason": "possible labeled credential material"})
    return {
        "schema": "webanything-release-scan-v1",
        "root": str(root),
        "status": "passed" if not failures else "failed",
        "files_scanned": files,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"release root is not a directory: {root}")
    result = scan(root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
