#!/usr/bin/env python3
"""Validate a deterministic local WebHarbor auth profile."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


MODES = {"default_authenticated", "explicit_login", "logged_out"}
SECRET_KEY = re.compile(r"(password|passwd|secret|token|cookie|oauth|api[_-]?key|private[_-]?key)", re.I)
REQUIRED = ("schema_version", "mode", "persona_id", "handle", "display_name", "roles", "permissions")


def walk_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if SECRET_KEY.search(str(key)):
                found.append(child_path)
            found.extend(walk_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(walk_keys(child, f"{path}[{index}]"))
    return found


def validate(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read JSON: {exc}"]
    if not isinstance(payload, dict):
        return ["profile must be a JSON object"]
    failures = [f"missing required field: {key}" for key in REQUIRED if key not in payload]
    if payload.get("schema_version") != 1:
        failures.append("schema_version must be 1")
    if payload.get("mode") not in MODES:
        failures.append(f"mode must be one of {sorted(MODES)}")
    for key in ("persona_id", "handle", "display_name"):
        if key in payload and (not isinstance(payload[key], str) or not payload[key].strip()):
            failures.append(f"{key} must be a non-empty string")
    for key in ("roles", "permissions"):
        if key in payload and (not isinstance(payload[key], list) or not all(isinstance(item, str) and item for item in payload[key])):
            failures.append(f"{key} must be a list of non-empty strings")
    secret_paths = walk_keys(payload)
    if secret_paths:
        failures.append(f"secret-like fields are forbidden: {', '.join(secret_paths)}")
    email = payload.get("email")
    if email is not None and (not isinstance(email, str) or not email.endswith("@local.invalid")):
        failures.append("email must use the reserved @local.invalid domain")
    if payload.get("mode") == "default_authenticated" and "authenticated" not in payload.get("roles", []):
        failures.append("default_authenticated profiles must include the authenticated role")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    args = parser.parse_args()
    failures = validate(args.profile)
    result = {"path": str(args.profile), "status": "failed" if failures else "passed", "failures": failures}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
