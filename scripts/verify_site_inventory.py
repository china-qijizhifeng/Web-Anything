"""Verify manifest integrity and, optionally, all 244 artifact entrypoint hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "sites244.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def verify(manifest_path: Path, webharbor_root: Path | None = None) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sites = manifest["sites"]
    errors = []
    if manifest.get("schema_version") != 1 or manifest.get("site_count") != 244 or len(sites) != 244:
        errors.append("Manifest schema or site count is not the frozen 244-site inventory")
    ids = [site.get("site_id") for site in sites]
    if len(set(ids)) != len(ids):
        errors.append("Duplicate site IDs")

    checked = 0
    for site in sites:
        site_id = site.get("site_id", "<unknown>")
        rel = Path(site.get("artifact_relpath", ""))
        expected = site.get("app_sha256", "")
        if rel.is_absolute() or not rel.parts or ".." in rel.parts:
            errors.append(f"{site_id}: unsafe artifact path")
            continue
        if not SHA256.fullmatch(expected):
            errors.append(f"{site_id}: invalid app SHA-256")
            continue
        if webharbor_root is None:
            continue
        app = webharbor_root / rel / "app.py"
        if not app.is_file():
            errors.append(f"{site_id}: missing {app}")
            continue
        actual = hashlib.sha256(app.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"{site_id}: app.py SHA-256 mismatch")
            continue
        checked += 1

    return {
        "manifest_sites": len(sites),
        "artifact_hashes_checked": checked,
        "artifact_root": str(webharbor_root) if webharbor_root else None,
        "ok": not errors,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--webharbor-root", type=Path)
    args = parser.parse_args()
    result = verify(args.manifest, args.webharbor_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
