#!/usr/bin/env python3
"""Emit deterministic discovery and Site Contract artifacts for a run workspace."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from webanything.discovery import DiscoveryError, emit_contract_artifacts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--site", required=True)
    parser.add_argument("--entry-url", required=True)
    parser.add_argument("--profile", default="task-driven")
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=1000)
    parser.add_argument("--discovery-output", type=Path)
    parser.add_argument("--contract-output", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    try:
        result = emit_contract_artifacts(
            args.workspace,
            site=args.site,
            entry_url=args.entry_url,
            profile=args.profile,
            viewport_width=args.viewport_width,
            viewport_height=args.viewport_height,
            discovery_output=args.discovery_output,
            contract_output=args.contract_output,
        )
    except (DiscoveryError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"contract artifact emission failed: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
