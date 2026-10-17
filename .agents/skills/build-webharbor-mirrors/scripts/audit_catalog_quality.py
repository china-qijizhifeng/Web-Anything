#!/usr/bin/env python3
"""Classify catalog candidates against current visual and three-fidelity proof."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fidelity_contract import fidelity_evidence_issues, fidelity_evidence_state
from quality_contract import quality_evidence_issues, quality_evidence_state


TOOL_VERSION = "webharbor-catalog-quality-audit-v1"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def replacement_statuses(root: Path) -> dict[str, str]:
    queue = load_json(
        root / "mirror_factory/evidence/generated-replacement/queue.json"
    )
    if not isinstance(queue, dict):
        return {}
    return {
        entry["site"]: entry.get("status", "unknown")
        for entry in queue.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("site"), str)
    }


def classify_site(root: Path, record: dict[str, Any], queue_status: str | None) -> dict[str, Any]:
    site = record["id"]
    evidence_dir = root / "mirror_factory/evidence" / site
    fidelity_path = evidence_dir / "fidelity.json"
    quality_path = evidence_dir / "quality.json"
    contract_path = root / "sites" / site / "mirror_contract.json"
    fidelity = load_json(fidelity_path)
    quality = load_json(quality_path)
    fidelity_state = fidelity_evidence_state(fidelity, root=root)
    fidelity_issues = fidelity_evidence_issues(fidelity, root=root)
    quality_state = quality_evidence_state(quality, root=root)
    quality_issues = quality_evidence_issues(quality, root=root)

    if queue_status == "rejected":
        classification = "source_rejected"
        next_action = "wait_for_promotable_exact_origin_source"
    elif fidelity_state == "failed_current":
        classification = "fidelity_failed"
        next_action = "repair_or_recapture_design_resources"
    elif fidelity_state != "passed_current":
        classification = "fidelity_missing_or_stale"
        next_action = "run_current_desktop_fidelity"
    elif quality_state == "failed_current":
        classification = "quality_failed"
        next_action = "implement_adapter_or_fix_data_interaction_contract"
    elif quality_state != "passed_current":
        classification = "quality_missing_or_stale"
        next_action = "generate_contract_and_run_three_fidelity_audit"
    else:
        classification = "quality_passed"
        next_action = "run_reviewer_owned_task_verifiers"

    return {
        "site": site,
        "display_name": record.get("display_name"),
        "tier": record.get("tier"),
        "classification": classification,
        "next_action": next_action,
        "queue_status": queue_status,
        "fidelity": {
            "state": fidelity_state,
            "evidence": fidelity_path.relative_to(root).as_posix(),
            "issues": fidelity_issues[:20],
        },
        "quality": {
            "state": quality_state,
            "grade": quality.get("grade") if isinstance(quality, dict) else None,
            "evidence": quality_path.relative_to(root).as_posix(),
            "issues": quality_issues[:20],
        },
        "mirror_contract": {
            "path": contract_path.relative_to(root).as_posix(),
            "present": contract_path.is_file(),
        },
    }


def audit(
    root: Path,
    *,
    tiers: set[str] | None = None,
    sites: set[str] | None = None,
) -> dict[str, Any]:
    catalog = load_json(root / "site_catalog.json")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("sites"), list):
        raise ValueError("site_catalog.json is missing or invalid")
    queue = replacement_statuses(root)
    selected = []
    for record in catalog["sites"]:
        tier = record.get("tier", "")
        if tiers is None and not tier.startswith("candidate_"):
            continue
        if tiers is not None and tier not in tiers:
            continue
        if sites is not None and record.get("id") not in sites:
            continue
        selected.append(classify_site(root, record, queue.get(record["id"])))

    classifications = Counter(row["classification"] for row in selected)
    tier_counts = Counter(row["tier"] for row in selected)
    by_tier: dict[str, Counter[str]] = defaultdict(Counter)
    for row in selected:
        by_tier[row["tier"]][row["classification"]] += 1
    return {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "phase": "catalog_three_fidelity_audit",
        "generated_at": utc_now(),
        "status": "passed" if classifications.get("quality_passed") == len(selected) else "incomplete",
        "selection": {
            "tiers": sorted(tiers) if tiers is not None else "all_candidate_tiers",
            "sites": sorted(sites) if sites is not None else "all",
        },
        "summary": {
            "sites": len(selected),
            "classifications": dict(sorted(classifications.items())),
            "tiers": dict(sorted(tier_counts.items())),
            "by_tier": {
                tier: dict(sorted(counts.items()))
                for tier, counts in sorted(by_tier.items())
            },
        },
        "sites": selected,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("sites", nargs="*")
    value.add_argument("--tier", action="append", dest="tiers")
    value.add_argument(
        "--output",
        type=Path,
        default=Path("mirror_factory/evidence/catalog-quality-audit.json"),
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = repository_root()
    result = audit(
        root,
        tiers=set(args.tiers) if args.tiers else None,
        sites=set(args.sites) if args.sites else None,
    )
    output = args.output if args.output.is_absolute() else root / args.output
    atomic_json(output, result)
    print(json.dumps({
        "status": result["status"],
        "summary": result["summary"],
        "output": output.relative_to(root).as_posix(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
