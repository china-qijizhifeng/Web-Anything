"""Thin adapter for a distributed batch supervisor.

The adapter only validates worker declarations and renders a bounded repair brief;
it never starts a site, edits a candidate, or imports the legacy Builder skill.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SITE_RE = re.compile(r"^[a-z0-9_]+$")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _compact(value: Any, limit: int = 30) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def repair_brief(round_number: int, coverage: dict, evidence_failures: list[str], *, acceptance_profile: str = "full-site") -> str:
    """Render a deterministic handoff for a later Builder continuation."""
    package = coverage.get("builder_repair_package") or {}
    summary = {
        "round": round_number,
        "meaningful_local_percent": coverage.get("clickthrough", {}).get("meaningful_local_percent", 0),
        "requested_min_percent": coverage.get("clickthrough", {}).get("requested_min_percent", 100),
        "next_candidates": _compact(coverage.get("next_candidates")),
        "unimplemented_links": _compact(coverage.get("unimplemented_links")),
        "resource_issues": _compact(coverage.get("resource_issues")),
        "browser_errors": _compact(coverage.get("browser_errors")),
        "current_evidence_failures": list(evidence_failures),
        "builder_repair_package": package if acceptance_profile == "immersive" else None,
    }
    if acceptance_profile == "immersive":
        prefix = f"Continue the forced immersive interaction-closure loop, round {round_number}.\n\n"
        suffix = (
            "\n\nClose every inventoried active-looking control and every discovered local route. "
            "The immersive acceptance profile requires exactly 100% active-control resolution, "
            "100% critical workflow semantics, and zero hard source/DOM/route gaps."
        )
    elif acceptance_profile == "reviewable-workflow":
        prefix = f"Continue the forced reviewable-workflow closure loop, round {round_number}.\n\n"
        suffix = "\n\nImplement exactly one source-grounded browser workflow with a visible return path."
    else:
        prefix = f"Continue the forced full-site interaction-closure loop, round {round_number}.\n\n"
        suffix = "\n\nFix the highest-priority source-evidenced interaction gaps within the declared scope."
    return prefix + "```json\n" + json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n```" + suffix


def load_batch_spec(path: Path) -> dict:
    """Validate independent verifier workers without launching them."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    supported_batches = {
        "webanything-interaction-verifier-batch-v1",
        "webharbor-interaction-verifier-batch-v1",
    }
    if spec.get("schema_version") != 1 or spec.get("batch") not in supported_batches:
        raise ValueError("unsupported batch spec")
    workers = spec.get("workers")
    if not isinstance(workers, list) or not 1 <= len(workers) <= 64:
        raise ValueError("batch must declare between one and 64 workers")
    ids = [item.get("site") for item in workers]
    if any(not isinstance(site, str) or not SITE_RE.fullmatch(site) for site in ids):
        raise ValueError("worker site ids must contain lowercase letters, digits, or underscores")
    if len(set(ids)) != len(ids):
        raise ValueError("worker site ids must be unique")
    evidence = [item.get("evidence_dir") for item in workers]
    if any(not isinstance(item, str) or not item for item in evidence) or len(set(evidence)) != len(evidence):
        raise ValueError("worker evidence directories must be non-empty and unique")
    ports: list[int] = []
    for item in workers:
        site = item.get("site")
        path_fields = ("workspace", "site_path", "contract", "evidence_dir", "browser_path", "dependency_path")
        resolved: dict[str, Path] = {}
        for field in path_fields:
            value = item.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{site}: {field} is required")
            path = Path(value)
            if not path.is_absolute():
                raise ValueError(f"{site}: {field} must be absolute")
            resolved[field] = path.resolve()
        if not resolved["workspace"].is_dir() or not resolved["site_path"].is_dir():
            raise ValueError(f"{site}: workspace and site_path must exist")
        if resolved["site_path"] != resolved["workspace"] / "sites" / str(site):
            raise ValueError(f"{site}: site_path must be the exact workspace site directory")
        if not resolved["contract"].is_file():
            raise ValueError(f"{site}: contract must exist")
        if _inside(resolved["evidence_dir"], resolved["site_path"]):
            raise ValueError(f"{site}: evidence_dir must be outside the candidate site")
        if not resolved["browser_path"].is_file() or not resolved["browser_path"].stat().st_mode & 0o111:
            raise ValueError(f"{site}: browser_path must be an executable file")
        if not resolved["dependency_path"].is_dir():
            raise ValueError(f"{site}: dependency_path must be a directory")
        launch = item.get("launch")
        if not isinstance(launch, dict):
            raise TypeError(f"{site}: an owned launch declaration is required")
        command = launch.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(value, str) and value for value in command):
            raise ValueError(f"{site}: launch.command must be a non-empty string list")
        if Path(str(launch.get("cwd") or "")).resolve() != resolved["workspace"]:
            raise ValueError(f"{site}: launch.cwd must equal workspace")
        parsed = urlsplit(str(launch.get("base_url") or ""))
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path != "/":
            raise ValueError(f"{site}: launch.base_url must be an owned loopback root URL")
        ports.append(parsed.port)
        env = launch.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
            raise ValueError(f"{site}: launch.env must contain only strings")
        viewport = item.get("viewport")
        budgets = item.get("budgets")
        if not isinstance(viewport, dict) or not all(isinstance(viewport.get(key), int) and viewport[key] > 0 for key in ("width", "height")):
            raise ValueError(f"{site}: viewport must contain positive integer dimensions")
        if not isinstance(budgets, dict) or not (
            isinstance(budgets.get("max_pages"), int) and budgets["max_pages"] > 0
            and isinstance(budgets.get("max_depth"), int) and budgets["max_depth"] >= 0
            and isinstance(budgets.get("timeout_ms"), int) and budgets["timeout_ms"] > 0
        ):
            raise ValueError(f"{site}: budgets are invalid")
    if len(set(ports)) != len(ports):
        raise ValueError("worker launch ports must be unique")
    return spec


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-spec", type=Path, required=True)
    args = parser.parse_args()
    try:
        spec = load_batch_spec(args.batch_spec.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": "valid", "workers": [item["site"] for item in spec["workers"]]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
