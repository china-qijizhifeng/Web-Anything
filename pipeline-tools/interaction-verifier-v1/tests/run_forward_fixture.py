#!/usr/bin/env python3
"""Run the bounded gateway fixture twice and print deterministic summaries."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from browser_verifier import verify
from fixture import prepare_workspace, running_fixture


def main() -> int:
    summaries = []
    with tempfile.TemporaryDirectory(prefix="interaction-forward-") as temporary:
        workspace = Path(temporary)
        contract = prepare_workspace(workspace)
        with running_fixture(gateway=True) as base_url:
            for name in ("first", "second"):
                _, closure, repair = verify(
                    workspace=workspace,
                    site="demo",
                    base_url=base_url,
                    contract_path=contract,
                    evidence_dir=workspace / name,
                    timeout_ms=5000,
                )
                summaries.append({
                    "status": closure["status"],
                    "gateway_prefix": closure["surface"]["gateway_prefix"],
                    "visited_pages": closure["crawl"]["visited_pages"],
                    "active_control_resolution_percent": closure["measured_rates"]["active_control_resolution_percent"],
                    "critical_workflow_semantics_percent": closure["measured_rates"]["critical_workflow_semantics_percent"],
                    "hard_gaps": closure["raw_counts"]["triangulation_hard_gaps"],
                    "repair_groups": repair["group_count"],
                })
    print(json.dumps({"runs": summaries, "deterministic": summaries[0] == summaries[1]}, sort_keys=True))
    return 0 if summaries[0] == summaries[1] and summaries[0]["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
