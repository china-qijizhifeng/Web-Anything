#!/usr/bin/env python3
"""Load and validate the shared WebAnything site catalog."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).with_name("site_catalog.json")
SITE_ID_RE = re.compile(r"^[a-z0-9_]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load_site_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1:
        raise ValueError("site catalog schema_version must be 1")
    if not isinstance(catalog.get("base_port"), int):
        raise ValueError("site catalog base_port must be an integer")
    if not catalog.get("asset_repo") or not catalog.get("default_asset_revision"):
        raise ValueError("site catalog must define asset_repo and default_asset_revision")

    sites = catalog.get("sites")
    if not isinstance(sites, list):
        raise ValueError("site catalog sites must be a list")

    seen: set[str] = set()
    for index, site in enumerate(sites):
        site_id = site.get("id") if isinstance(site, dict) else None
        if not isinstance(site_id, str) or not SITE_ID_RE.fullmatch(site_id):
            raise ValueError(f"invalid site id at index {index}: {site_id!r}")
        if site_id in seen:
            raise ValueError(f"duplicate site id: {site_id}")
        seen.add(site_id)
        site["port"] = catalog["base_port"] + index
        site.setdefault("assets", {})
        assets = site["assets"]
        assets.setdefault("kind", "hf_tar")
        if assets["kind"] == "hf_tar":
            assets.setdefault("repo", catalog["asset_repo"])
            assets.setdefault("revision", catalog["default_asset_revision"])
            assets.setdefault("filename", f"{site_id}.tar.gz")
            for key in ("repo", "revision", "filename"):
                if not isinstance(assets.get(key), str) or not assets[key]:
                    raise ValueError(f"{site_id}: hf_tar assets require {key}")
        elif assets["kind"] == "generated":
            if assets.get("generator") != "app_import":
                raise ValueError(
                    f"{site_id}: generated assets require generator='app_import'"
                )
            if not isinstance(assets.get("sha256"), str) or not SHA256_RE.fullmatch(
                assets["sha256"]
            ):
                raise ValueError(f"{site_id}: generated assets require a SHA256")
        else:
            raise ValueError(f"{site_id}: unsupported asset kind: {assets['kind']!r}")
    return catalog


def selected_site_records(
    catalog: dict[str, Any] | None = None,
    selection: str | None = None,
) -> list[dict[str, Any]]:
    catalog = catalog or load_site_catalog()
    sites = catalog["sites"]
    selection = os.environ.get("WEBANYTHING_SITES") if selection is None else selection
    if not selection:
        return sites

    requested = [item.strip() for item in selection.split(",") if item.strip()]
    if not requested:
        raise ValueError("WEBANYTHING_SITES must contain at least one site id")
    if len(requested) != len(set(requested)):
        raise ValueError("WEBANYTHING_SITES contains duplicate site ids")
    known = {site["id"] for site in sites}
    unknown = sorted(set(requested) - known)
    if unknown:
        raise ValueError(f"unknown WEBANYTHING_SITES entries: {', '.join(unknown)}")
    requested_set = set(requested)
    return [site for site in sites if site["id"] in requested_set]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="ignore WEBANYTHING_SITES and print every catalog entry",
    )
    parser.add_argument(
        "--assets",
        action="store_true",
        help="also print each site's pinned asset revision",
    )
    args = parser.parse_args()

    catalog = load_site_catalog()
    records = catalog["sites"] if args.all else selected_site_records(catalog)
    for site in records:
        columns = [site["id"], str(site["port"])]
        if args.assets:
            assets = site["assets"]
            columns.extend(
                [
                    assets["kind"],
                    assets.get("repo", ""),
                    assets.get("revision", ""),
                    assets.get("filename", ""),
                ]
            )
        print("\t".join(columns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
