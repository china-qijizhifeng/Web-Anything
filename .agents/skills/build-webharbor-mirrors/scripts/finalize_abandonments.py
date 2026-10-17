#!/usr/bin/env python3
"""Finalize rejected mirrors only after every required source gate is evidenced."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TOOL_VERSION = "webharbor-abandonment-finalizer-v1"


class AbandonmentFinalizerError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_json(path: Path) -> tuple[Any, dict[str, Any]]:
    body = path.read_bytes()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AbandonmentFinalizerError(f"invalid JSON: {path}") from exc
    return value, {
        "path": str(path),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def entries_by_site(report: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    entries = report.get("entries")
    if not isinstance(entries, list):
        raise AbandonmentFinalizerError(f"{label} has no entries list")
    output = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("site"), str):
            raise AbandonmentFinalizerError(f"{label} contains an invalid entry")
        if entry["site"] in output:
            raise AbandonmentFinalizerError(
                f"{label} contains duplicate site {entry['site']}"
            )
        output[entry["site"]] = entry
    return output


def supplemental_findings(report: dict[str, Any], site: str) -> list[dict[str, Any]]:
    findings = []
    for provider, value in report.items():
        if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
            continue
        for finding in value["findings"]:
            if not isinstance(finding, dict):
                continue
            sites = finding.get("sites")
            if finding.get("site") == site or (
                isinstance(sites, list) and site in sites
            ):
                findings.append({
                    "provider": provider,
                    "decision": finding.get("decision"),
                    "reason": finding.get("reason"),
                })
    return findings


def finalizer(args: argparse.Namespace) -> dict[str, Any]:
    queue, queue_file = load_json(args.queue)
    index_probe, index_file = load_json(args.index_probe)
    parquet_scan, parquet_file = load_json(args.parquet_scan)
    common_crawl, common_crawl_file = load_json(args.common_crawl)
    supplemental, supplemental_file = load_json(args.supplemental)
    if not all(isinstance(value, dict) for value in (
        queue, index_probe, parquet_scan, common_crawl, supplemental
    )):
        raise AbandonmentFinalizerError("all evidence roots must be JSON objects")
    queue_entries = entries_by_site(queue, "queue")
    index_entries = entries_by_site(index_probe, "index probe")
    parquet_entries = entries_by_site(parquet_scan, "Parquet scan")
    crawl_entries = entries_by_site(common_crawl, "Common Crawl probe")
    rejected = [
        entry for entry in queue_entries.values() if entry.get("status") == "rejected"
    ]
    if not rejected:
        raise AbandonmentFinalizerError("queue contains no rejected entries")
    output_entries = []
    for queue_entry in rejected:
        site = queue_entry["site"]
        missing = [
            label
            for label, mapping in (
                ("index probe", index_entries),
                ("Parquet scan", parquet_entries),
                ("Common Crawl probe", crawl_entries),
            )
            if site not in mapping
        ]
        if missing:
            raise AbandonmentFinalizerError(
                f"{site} lacks evidence in: {', '.join(missing)}"
            )
        result_path = Path(queue_entry.get("result", ""))
        intake_path = result_path.with_name("intake.json")
        result, result_file = load_json(result_path)
        intake, intake_file = load_json(intake_path)
        if not isinstance(result, dict) or not isinstance(intake, dict):
            raise AbandonmentFinalizerError(f"{site} live evidence is invalid")
        index_entry = index_entries[site]
        parquet_entry = parquet_entries[site]
        crawl_entry = crawl_entries[site]
        gates = {
            "live_intake_rejected": (
                result.get("status") == "rejected"
                and intake.get("status") == "rejected"
            ),
            "hosted_indexes_have_no_candidate": (
                index_entry.get("status") == "no_exact_origin_candidate"
                and not index_entry.get("candidates")
            ),
            "parquet_url_columns_have_no_match": (
                parquet_entry.get("status") == "no_exact_origin_candidate"
            ),
            "common_crawl_has_no_eligible_candidate": (
                not crawl_entry.get("eligible_candidates")
            ),
            "exact_origin_policy_recorded": all((
                index_probe.get("selection", {}).get("exact_origin_required") is True,
                parquet_scan.get("selection", {}).get(
                    "exact_scheme_hostname_port_required"
                ) is True,
                common_crawl.get("selection", {}).get(
                    "exact_scheme_hostname_port_required"
                ) is True,
            )),
        }
        retry_conditions = []
        for entry in (index_entry, parquet_entry, crawl_entry):
            for condition in entry.get("retry_conditions", []):
                if isinstance(condition, str) and condition not in retry_conditions:
                    retry_conditions.append(condition)
        retry_conditions.append(
            "rerun the full HTML-first transactional pipeline only after a new exact-origin source satisfies structure, design-code, geometry, desktop visual, runtime/reset, and fidelity gates"
        )
        output_entries.append({
            "site": site,
            "upstream_url": queue_entry.get("upstream_url"),
            "status": "abandoned" if all(gates.values()) else "unresolved",
            "live": {
                "error": queue_entry.get("error") or result.get("error"),
                "attempts": queue_entry.get("attempts"),
                "failure_kind": intake.get("failure_kind"),
                "result": result_file,
                "intake": intake_file,
            },
            "hosted_index_probe": {
                "status": index_entry.get("status"),
                "queries": len(index_entry.get("queries", [])),
                "candidates": len(index_entry.get("candidates", [])),
            },
            "parquet_origin_scan": {
                "status": parquet_entry.get("status"),
                "exact_origin_matches": sum(
                    site in match.get("sites", [])
                    for match in parquet_scan.get("matches", [])
                    if isinstance(match, dict)
                ),
            },
            "common_crawl_probe": {
                "status": crawl_entry.get("status"),
                "queries": len(crawl_entry.get("queries", [])),
                "query_failures": sum(
                    query.get("status") == "unavailable"
                    for query in crawl_entry.get("queries", [])
                    if isinstance(query, dict)
                ),
                "records": len(crawl_entry.get("records", [])),
                "eligible_candidates": len(
                    crawl_entry.get("eligible_candidates", [])
                ),
            },
            "supplemental_findings": supplemental_findings(supplemental, site),
            "gates": gates,
            "retry_conditions": retry_conditions,
        })
    unresolved = sum(entry["status"] != "abandoned" for entry in output_entries)
    return {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "generated_at": utc_now(),
        "status": "passed" if not unresolved else "unresolved",
        "policy": {
            "desktop_only": True,
            "html_dom_first": True,
            "exact_scheme_hostname_port": True,
            "sibling_mobile_and_template_substitution_forbidden": True,
            "provider_unavailability_never_promotes_a_candidate": True,
        },
        "evidence_files": {
            "queue": queue_file,
            "hosted_index_probe": index_file,
            "parquet_origin_scan": parquet_file,
            "common_crawl_probe": common_crawl_file,
            "supplemental_historical_survey": supplemental_file,
        },
        "source_summary": {
            "hosted_index": index_probe.get("summary"),
            "parquet_scan": parquet_scan.get("summary"),
            "common_crawl": common_crawl.get("summary"),
            "pinned_parquet_providers": parquet_scan.get("providers"),
            "pinned_common_crawl_provider": common_crawl.get("provider"),
        },
        "summary": {
            "queue_passed": queue.get("summary", {}).get("passed"),
            "queue_rejected": queue.get("summary", {}).get("rejected"),
            "abandoned": len(output_entries) - unresolved,
            "unresolved": unresolved,
        },
        "entries": output_entries,
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    root = Path("mirror_factory/evidence/generated-replacement")
    value.add_argument("--queue", type=Path, default=root / "queue.json")
    value.add_argument(
        "--index-probe", type=Path, default=root / "index-probe.json"
    )
    value.add_argument(
        "--parquet-scan", type=Path, default=root / "parquet-origin-scan.json"
    )
    value.add_argument(
        "--common-crawl", type=Path, default=root / "common-crawl-probe.json"
    )
    value.add_argument(
        "--supplemental",
        type=Path,
        default=root / "historical-source-discovery.json",
    )
    value.add_argument("--output", type=Path, required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = finalizer(args)
    atomic_json(args.output, report)
    print(json.dumps({
        "status": report["status"],
        "summary": report["summary"],
        "output": str(args.output),
    }, sort_keys=True))
    return 0 if report["status"] == "passed" else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AbandonmentFinalizerError, OSError) as exc:
        print(f"abandonment finalizer error: {exc}", file=sys.stderr)
        raise SystemExit(2)
