#!/usr/bin/env python3
"""Build and verify catalog assets declared as generated from app imports."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from site_catalog import load_site_catalog


class GeneratedAssetError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_import(site_dir: Path, python: str) -> None:
    process = subprocess.run(
        [python, "-c", "import app"],
        cwd=site_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode:
        raise GeneratedAssetError(
            f"{site_dir.name}: app import failed ({process.returncode}): "
            f"{process.stderr.strip()}"
        )


def find_database(directory: Path, site_id: str) -> Path:
    preferred = directory / f"{site_id}.db"
    if preferred.is_file():
        return preferred
    candidates = sorted(directory.glob("*.db")) if directory.is_dir() else []
    if len(candidates) != 1:
        raise GeneratedAssetError(
            f"{site_id}: expected one generated database, found {len(candidates)}"
        )
    return candidates[0]


def prepare_generated_site(
    record: dict,
    *,
    root: Path = ROOT,
    python: str = sys.executable,
) -> dict:
    site_id = record["id"]
    site_dir = root / "sites" / site_id
    seed_dir = site_dir / "instance_seed"
    instance_dir = site_dir / "instance"
    expected_hash = record["assets"].get("sha256")
    expected_bytes = record["assets"].get("bytes")

    if not (site_dir / "app.py").is_file():
        raise GeneratedAssetError(f"{site_id}: app.py is missing")
    if instance_dir.exists():
        shutil.rmtree(instance_dir)
    if seed_dir.is_dir():
        shutil.copytree(seed_dir, instance_dir)
    else:
        run_import(site_dir, python)
        generated_db = find_database(instance_dir, site_id)
        seed_dir.mkdir(parents=True)
        shutil.copy2(generated_db, seed_dir / f"{site_id}.db")

    seed_db = find_database(seed_dir, site_id)
    seed_hash = sha256_file(seed_db)
    if expected_hash and seed_hash != expected_hash:
        raise GeneratedAssetError(
            f"{site_id}: seed SHA256 mismatch: expected {expected_hash}, got {seed_hash}"
        )
    if expected_bytes is not None and seed_db.stat().st_size != expected_bytes:
        raise GeneratedAssetError(
            f"{site_id}: seed size mismatch: expected {expected_bytes}, "
            f"got {seed_db.stat().st_size}"
        )
    with sqlite3.connect(f"file:{seed_db}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise GeneratedAssetError(f"{site_id}: SQLite integrity check returned {integrity}")

    runtime_db = find_database(instance_dir, site_id)
    before_import = sha256_file(runtime_db)
    run_import(site_dir, python)
    after_import = sha256_file(runtime_db)
    if before_import != after_import or after_import != seed_hash:
        raise GeneratedAssetError(
            f"{site_id}: populated app import changed the generated database"
        )
    shutil.rmtree(instance_dir)
    return {
        "site": site_id,
        "bytes": seed_db.stat().st_size,
        "sha256": seed_hash,
        "integrity_check": integrity,
        "second_import_byte_identical": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sites", nargs="*")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    catalog = load_site_catalog(args.root / "site_catalog.json")
    selected = set(args.sites)
    known = {record["id"] for record in catalog["sites"]}
    unknown = sorted(selected - known)
    if unknown:
        raise GeneratedAssetError(f"unknown sites: {', '.join(unknown)}")
    for record in catalog["sites"]:
        if record["assets"]["kind"] != "generated":
            continue
        if selected and record["id"] not in selected:
            continue
        result = prepare_generated_site(record, root=args.root, python=args.python)
        print(
            f"{result['site']}\t{result['bytes']}\t{result['sha256']}\t"
            f"integrity={result['integrity_check']}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GeneratedAssetError as exc:
        print(f"prepare_generated_assets: {exc}", file=sys.stderr)
        raise SystemExit(2)
