"""Turn the frozen 2026-10-09 resource audit into a portable site manifest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def import_inventory(evidence_path: Path, table_path: Path, source_root: Path) -> dict:
    evidence_bytes = evidence_path.read_bytes()
    evidence = json.loads(evidence_bytes)
    with table_path.open(encoding="utf-8-sig", newline="") as stream:
        table = {row["site_id"]: row for row in csv.DictReader(stream)}

    if len(evidence) != 244 or len(table) != 244:
        raise ValueError("Expected 244 records in both frozen inputs")

    sites = []
    for item in sorted(evidence, key=lambda row: row["site_id"]):
        site_id = item["site_id"]
        if site_id not in table:
            raise ValueError(f"Missing table row: {site_id}")
        artifact = Path(item["artifact_path"])
        try:
            relative = artifact.relative_to(source_root)
        except ValueError as exc:
            raise ValueError(f"Artifact outside source root: {artifact}") from exc
        if ".." in relative.parts:
            raise ValueError(f"Invalid artifact path: {artifact}")
        row = table[site_id]
        report = item["pipeline_report"] or None
        if report:
            try:
                report = Path(report).relative_to(source_root).as_posix()
            except ValueError as exc:
                raise ValueError(f"Pipeline report outside source root: {report}") from exc
        sites.append(
            {
                "site_id": site_id,
                "site_name": row["site_name"],
                "source_url": item["source_url"],
                "domain": row["domain"],
                "category": row["category"],
                "artifact_relpath": relative.as_posix(),
                "app_sha256": item["app_sha256"],
                "provenance_kind": item["provenance_kind"] or None,
                "pipeline_report_relpath": report,
                "homepage_http_status": item["startup_http_status"],
                "homepage_observation": item["startup_observation"],
                "diagnosis_scope": item["diagnosis_scope"],
                "diagnosis_status": item["diagnosis_status"],
                "homepage_screenshot_sha256": item["current_screenshot_sha256"],
            }
        )

    ids = [site["site_id"] for site in sites]
    if len(set(ids)) != 244:
        raise ValueError("Duplicate site IDs")
    return {
        "schema_version": 1,
        "frozen_date": "2026-10-09",
        "artifact_root_layout": ["sites", "generated"],
        "source_evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "source_table_sha256": hashlib.sha256(table_path.read_bytes()).hexdigest(),
        "scope": "Selected historical artifacts; homepage evidence is not a functional or release-gate pass.",
        "site_count": len(sites),
        "sites": sites,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--table", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = import_inventory(args.evidence, args.table, args.source_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
