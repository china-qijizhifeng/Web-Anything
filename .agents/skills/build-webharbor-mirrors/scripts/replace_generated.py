#!/usr/bin/env python3
"""Replace candidate_generated sites through the one-URL mirror pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fidelity_contract import (
    FIDELITY_CONTRACT_VERSION,
    TOOL_VERSION as FIDELITY_TOOL_VERSION,
    fidelity_evidence_issues,
    fidelity_evidence_state,
)
from quality_contract import (
    QUALITY_CONTRACT_VERSION,
    TOOL_VERSION as QUALITY_TOOL_VERSION,
    quality_evidence_issues,
    quality_evidence_state,
)


TOOL_VERSION = "webharbor-generated-replacement-v5"
QUEUE_RELATIVE = Path("mirror_factory/evidence/generated-replacement/queue.json")
WORK_RELATIVE = Path(".assets/generated-replacement")
GLOBAL_PATHS = (Path("site_catalog.json"), Path("mirror_factory/targets.jsonl"), Path("Dockerfile"))
MANAGED_SITE_PATHS = (
    Path("app.py"),
    Path("_health.py"),
    Path("tasks.jsonl"),
    Path("workflow_gaps.json"),
    Path("mirror_contract.json"),
    Path("templates"),
    Path("instance_seed"),
    Path("instance"),
    Path("static/css"),
    Path("static/js"),
    Path("static/icons"),
    Path("static/images"),
    Path("static/external_cache/replay_bundle.json.gz"),
)


class ReplacementError(RuntimeError):
    pass


def load_historical_specs(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplacementError(f"invalid historical specs: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ReplacementError("historical specs require schema_version=1")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ReplacementError("historical specs require a non-empty entries list")
    defaults = {
        key: value.get(key)
        for key in (
            "provider", "dataset", "revision", "config", "split", "license",
            "minimum_source_image_ssim",
        )
        if value.get(key) is not None
    }
    output: dict[str, dict[str, Any]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise ReplacementError("historical spec entries must be objects")
        entry = {**defaults, **raw}
        provider = entry.get("provider", "mind2web")
        if provider not in {"mind2web", "weblinx", "webui_dom_snapshot"}:
            raise ReplacementError(f"unsupported historical provider: {provider}")
        required = {"site", "upstream_url", "dataset", "revision"}
        if provider == "mind2web":
            required.update({"website", "split", "row_index"})
        elif provider == "weblinx":
            required.update({
                "demonstration", "page", "bbox", "screenshot",
                "html_sha256", "bbox_sha256", "screenshot_sha256", "license",
            })
        else:
            required.update({
                "capture_url", "config", "split", "row_index", "dom_sha256",
                "row_metadata_sha256", "screenshot_sha256", "license",
            })
        missing = sorted(required - set(entry))
        if missing:
            raise ReplacementError(
                f"historical spec entry is missing {', '.join(missing)}"
            )
        site = entry["site"]
        if not isinstance(site, str) or site in output:
            raise ReplacementError("historical spec site ids must be unique strings")
        if not isinstance(entry["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", entry["revision"]):
            raise ReplacementError(f"{site}: revision must be a full lowercase commit SHA")
        if provider in {"mind2web", "webui_dom_snapshot"}:
            if not isinstance(entry["row_index"], int) or entry["row_index"] < 0:
                raise ReplacementError(f"{site}: row_index must be a non-negative integer")
            supported_splits = (
                {"train", "test_domain", "test_task", "test_website"}
                if provider == "mind2web"
                else {"train"}
            )
            if entry["split"] not in supported_splits:
                raise ReplacementError(f"{site}: unsupported historical split")
        if provider == "weblinx":
            for name in ("demonstration", "page", "bbox", "screenshot"):
                if not isinstance(entry[name], str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]*", entry[name]
                ):
                    raise ReplacementError(f"{site}: {name} is not a safe dataset path component")
            for name in ("html_sha256", "bbox_sha256", "screenshot_sha256"):
                if not isinstance(entry[name], str) or not re.fullmatch(r"[0-9a-f]{64}", entry[name]):
                    raise ReplacementError(f"{site}: {name} must be a lowercase SHA256")
        elif provider == "webui_dom_snapshot":
            for name in ("dom_sha256", "row_metadata_sha256", "screenshot_sha256"):
                if not isinstance(entry[name], str) or not re.fullmatch(r"[0-9a-f]{64}", entry[name]):
                    raise ReplacementError(f"{site}: {name} must be a lowercase SHA256")
            minimum_ssim = entry.get("minimum_source_image_ssim", 0.7)
            if not isinstance(minimum_ssim, (int, float)) or not 0 <= minimum_ssim <= 1:
                raise ReplacementError(
                    f"{site}: minimum_source_image_ssim must be between zero and one"
                )
        output[site] = entry
    return output


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_targets(path: Path) -> dict[str, dict[str, Any]]:
    rows = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReplacementError(f"invalid target line {line_number}: {exc}") from exc
        rows[row["id"]] = row
    return rows


def new_queue(root: Path) -> dict[str, Any]:
    catalog = json.loads((root / "site_catalog.json").read_text(encoding="utf-8"))
    targets = read_targets(root / "mirror_factory/targets.jsonl")
    entries = []
    for record in catalog["sites"]:
        if record.get("tier") != "candidate_generated":
            continue
        site = record["id"]
        target = targets.get(site)
        if target is None or not isinstance(target.get("upstream_url"), str):
            raise ReplacementError(f"{site}: candidate has no upstream target URL")
        entries.append({
            "site": site,
            "display_name": record["display_name"],
            "upstream_url": target["upstream_url"],
            "initial_tier": "candidate_generated",
            "status": "pending",
            "attempts": 0,
        })
    if not entries:
        raise ReplacementError("catalog contains no candidate_generated sites")
    return {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "phase": "generated_replacement",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "status": "pending",
        "entries": entries,
    }


def queue_summary(queue: dict[str, Any]) -> dict[str, int]:
    counts = Counter(entry["status"] for entry in queue["entries"])
    return {name: counts[name] for name in sorted(counts)}


def refresh_fidelity_states(queue: dict[str, Any], root: Path) -> None:
    """Invalidate entries whose visual or three-fidelity proof is old or failed."""
    eligible = {
        "passed", "stale_fidelity", "fidelity_failed",
        "stale_quality", "quality_failed",
    }
    for entry in queue["entries"]:
        if entry.get("status") not in eligible:
            continue
        path = root / "mirror_factory" / "evidence" / entry["site"] / "fidelity.json"
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evidence = None
        state = fidelity_evidence_state(evidence, root=root)
        issues = fidelity_evidence_issues(evidence, root=root)
        fidelity_status = {
            "passed_current": "passed",
            "failed_current": "fidelity_failed",
            "stale": "stale_fidelity",
        }[state]
        entry["status"] = fidelity_status
        entry["fidelity_contract"] = {
            "required_tool": FIDELITY_TOOL_VERSION,
            "required_version": FIDELITY_CONTRACT_VERSION,
            "evidence": str(path.relative_to(root)),
            "state": state,
            "issues": issues[:20],
        }
        if state != "passed_current":
            entry.pop("quality_contract", None)
            continue
        quality_path = root / "mirror_factory" / "evidence" / entry["site"] / "quality.json"
        try:
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            quality = None
        quality_state = quality_evidence_state(quality, root=root)
        quality_issues = quality_evidence_issues(quality, root=root)
        entry["status"] = {
            "passed_current": "passed",
            "failed_current": "quality_failed",
            "stale": "stale_quality",
        }[quality_state]
        entry["quality_contract"] = {
            "required_tool": QUALITY_TOOL_VERSION,
            "required_version": QUALITY_CONTRACT_VERSION,
            "evidence": str(quality_path.relative_to(root)),
            "state": quality_state,
            "grade": quality.get("grade") if isinstance(quality, dict) else None,
            "issues": quality_issues[:20],
        }


def write_queue(path: Path, queue: dict[str, Any]) -> None:
    counts = queue_summary(queue)
    queue["updated_at"] = utc_now()
    queue["summary"] = counts
    if counts.get("pending") or counts.get("running"):
        queue["status"] = "running"
    elif counts.get("stale_fidelity") or counts.get("stale_quality"):
        queue["status"] = "quality_reaudit_required"
    elif (
        counts.get("failed")
        or counts.get("rejected")
        or counts.get("rollback_failed")
        or counts.get("fidelity_failed")
        or counts.get("quality_failed")
    ):
        queue["status"] = "completed_with_failures"
    else:
        queue["status"] = "passed"
        queue["completed_at"] = utc_now()
    atomic_json(path, queue)


def load_or_create_queue(root: Path, path: Path) -> dict[str, Any]:
    if path.is_file():
        queue = json.loads(path.read_text(encoding="utf-8"))
        if queue.get("schema_version") != 1 or not isinstance(queue.get("entries"), list):
            raise ReplacementError(f"invalid replacement queue: {path}")
        sites = [entry.get("site") for entry in queue["entries"]]
        if len(sites) != len(set(sites)) or any(not isinstance(site, str) for site in sites):
            raise ReplacementError("replacement queue contains invalid or duplicate site ids")
        queue["tool"] = TOOL_VERSION
        refresh_fidelity_states(queue, root)
        return queue
    queue = new_queue(root)
    write_queue(path, queue)
    return queue


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def backup_path(root: Path, work_root: Path, site: str) -> Path:
    return work_root / "backups" / site


def create_backup(root: Path, work_root: Path, site: str) -> Path:
    destination = backup_path(root, work_root, site)
    if destination.exists():
        raise ReplacementError(f"{site}: unresolved replacement backup already exists")
    destination.mkdir(parents=True)
    existing_site_paths = []
    site_root = root / "sites" / site
    for relative in MANAGED_SITE_PATHS:
        source = site_root / relative
        if source.exists() or source.is_symlink():
            copy_path(source, destination / "site" / relative)
            existing_site_paths.append(relative.as_posix())
    for relative in GLOBAL_PATHS:
        copy_path(root / relative, destination / "global" / relative)
    atomic_json(destination / "metadata.json", {
        "schema_version": 1,
        "site": site,
        "created_at": utc_now(),
        "site_paths": existing_site_paths,
        "global_paths": [path.as_posix() for path in GLOBAL_PATHS],
    })
    return destination


def restore_backup(root: Path, work_root: Path, site: str) -> None:
    source = backup_path(root, work_root, site)
    metadata_path = source / "metadata.json"
    if not metadata_path.is_file():
        raise ReplacementError(f"{site}: replacement backup metadata is missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("site") != site:
        raise ReplacementError(f"{site}: replacement backup belongs to another site")
    site_root = root / "sites" / site
    existing = set(metadata["site_paths"])
    for relative in MANAGED_SITE_PATHS:
        destination = site_root / relative
        remove_path(destination)
        if relative.as_posix() in existing:
            copy_path(source / "site" / relative, destination)
    for relative in GLOBAL_PATHS:
        destination = root / relative
        remove_path(destination)
        copy_path(source / "global" / relative, destination)
    shutil.rmtree(source)


def discard_backup(root: Path, work_root: Path, site: str) -> None:
    source = backup_path(root, work_root, site)
    if source.exists():
        shutil.rmtree(source)


def recover_interrupted(queue: dict[str, Any], root: Path, work_root: Path) -> None:
    for entry in queue["entries"]:
        backup = backup_path(root, work_root, entry["site"])
        if not backup.exists():
            if entry["status"] == "running":
                entry["status"] = "pending"
                entry["recovery"] = "interrupted without a live backup; queued for retry"
            continue
        if entry["status"] == "passed":
            discard_backup(root, work_root, entry["site"])
            continue
        restore_backup(root, work_root, entry["site"])
        entry["status"] = "pending"
        entry["recovery"] = "restored interrupted replacement backup"


def build_command(root: Path, entry: dict[str, Any], args: argparse.Namespace) -> list[str]:
    runtime_python = os.environ.get("WEBHARBOR_RUNTIME_PYTHON", "python3")
    dependency_path = os.environ.get("WEBHARBOR_RUNTIME_DEPENDENCY_PATH")
    historical = getattr(args, "historical_by_site", {}).get(entry["site"])
    if historical is not None:
        if historical["upstream_url"] != entry["upstream_url"]:
            raise ReplacementError(
                f"{entry['site']}: historical spec upstream_url differs from frozen queue"
            )
        provider = historical.get("provider", "mind2web")
        command = [
            sys.executable,
            str(root / ".agents/skills/build-webharbor-mirrors/scripts/build_historical.py"),
            "--resume",
            "--replace-existing",
            "--provider", provider,
            "--site", entry["site"],
            "--display-name", entry["display_name"],
            "--upstream-url", entry["upstream_url"],
            "--python", runtime_python,
            "--stage-timeout", str(args.stage_timeout),
        ]
        if dependency_path:
            command.extend(["--dependency-path", dependency_path])
        for name in ("dataset", "revision"):
            if historical.get(name) is not None:
                command.extend([f"--{name}", str(historical[name])])
        if provider == "mind2web":
            for name in ("website", "split", "row_index", "config"):
                if historical.get(name) is not None:
                    command.extend([f"--{name.replace('_', '-')}", str(historical[name])])
        elif provider == "weblinx":
            for name in (
                "demonstration", "page", "bbox", "screenshot", "html_sha256",
                "bbox_sha256", "screenshot_sha256", "license",
            ):
                command.extend([f"--{name.replace('_', '-')}", str(historical[name])])
        else:
            for name in (
                "capture_url", "config", "split", "row_index", "dom_sha256",
                "row_metadata_sha256", "screenshot_sha256",
                "minimum_source_image_ssim", "license",
            ):
                if historical.get(name) is not None:
                    command.extend([f"--{name.replace('_', '-')}", str(historical[name])])
        if historical.get("minimum_pixel_similarity") is not None:
            command.extend([
                "--minimum-pixel-similarity",
                str(historical["minimum_pixel_similarity"]),
            ])
        return command
    command = [
        sys.executable,
        str(root / ".agents/skills/build-webharbor-mirrors/scripts/build_from_upstream.py"),
        "--resume",
        "--replace-existing",
        "--site", entry["site"],
        "--display-name", entry["display_name"],
        "--python", runtime_python,
        "--stage-timeout", str(args.stage_timeout),
    ]
    if dependency_path:
        command.extend(["--dependency-path", dependency_path])
    if args.private_research_snapshot:
        command.append("--private-research-snapshot")
    capture_url = entry.get("capture_url", entry["upstream_url"])
    if not isinstance(capture_url, str):
        raise ReplacementError(f"{entry['site']}: capture_url must be a string")
    if capture_url != entry["upstream_url"]:
        command.extend(["--represented-url", entry["upstream_url"]])
    command.append(capture_url)
    return command


def run_entry(
    queue: dict[str, Any],
    queue_path: Path,
    entry: dict[str, Any],
    root: Path,
    work_root: Path,
    args: argparse.Namespace,
) -> bool:
    site = entry["site"]
    create_backup(root, work_root, site)
    entry.update({
        "status": "running",
        "started_at": utc_now(),
        "attempts": int(entry.get("attempts", 0)) + 1,
    })
    entry.pop("error", None)
    write_queue(queue_path, queue)
    log_path = work_root / "logs" / f"{site}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(root, entry, args)
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=args.site_timeout,
        )
        output = completed.stdout or ""
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        output += f"\nsite timed out after {args.site_timeout:g} seconds\n"
        returncode = 124
    log_path.write_text(output, encoding="utf-8")

    result_path = root / "mirror_factory" / "evidence" / site / "result.json"
    result = None
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            result = None
    passed = returncode == 0 and result is not None and result.get("status") == "passed"
    if passed:
        entry.update({
            "status": "passed",
            "finished_at": utc_now(),
            "returncode": returncode,
            "result": str(result_path.relative_to(root)),
            "pages": result.get("pages"),
            "assets": result.get("assets"),
            "tasks": result.get("tasks"),
            "quality_grade": result.get("quality_grade"),
            "log": str(log_path.relative_to(root)),
        })
        write_queue(queue_path, queue)
        discard_backup(root, work_root, site)
        return True

    status = "rejected" if returncode == 2 or (result and result.get("status") == "rejected") else "failed"
    try:
        restore_backup(root, work_root, site)
    except Exception as exc:
        status = "rollback_failed"
        rollback_error = str(exc)
    else:
        rollback_error = None
    error = result.get("error") if result else None
    if not error:
        error = "\n".join(output.strip().splitlines()[-20:]) or f"pipeline exited {returncode}"
    if rollback_error:
        error += f"\nrollback failed: {rollback_error}"
    entry.update({
        "status": status,
        "finished_at": utc_now(),
        "returncode": returncode,
        "error": error[-6000:],
        "result": str(result_path.relative_to(root)) if result_path.is_file() else None,
        "log": str(log_path.relative_to(root)),
    })
    write_queue(queue_path, queue)
    if status == "rollback_failed":
        raise ReplacementError(f"{site}: rollback failed; stopping before another replacement")
    return False


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("sites", nargs="*", help="optional queue site ids")
    value.add_argument("--limit", type=int, help="maximum sites to attempt in this run")
    value.add_argument("--retry-failed", action="store_true")
    value.add_argument(
        "--private-research-snapshot",
        action="store_true",
        help="freeze the user's explicit local-only research authorization in this queue",
    )
    value.add_argument(
        "--rebuild-passed",
        action="store_true",
        help="rebuild explicitly named passed sites after a pipeline correction",
    )
    value.add_argument("--status", action="store_true", help="print queue state without running")
    value.add_argument(
        "--historical-specs",
        type=Path,
        help="JSON batch mapping queue sites to pinned historical benchmark rows",
    )
    value.add_argument("--stage-timeout", type=float, default=600.0)
    value.add_argument("--site-timeout", type=float, default=1800.0)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")
    if args.stage_timeout <= 0 or args.site_timeout <= 0:
        raise SystemExit("timeouts must be positive")
    root = repository_root()
    args.historical_by_site = (
        load_historical_specs(args.historical_specs.resolve())
        if args.historical_specs
        else {}
    )
    queue_path = root / QUEUE_RELATIVE
    work_root = root / WORK_RELATIVE
    queue = load_or_create_queue(root, queue_path)
    if args.private_research_snapshot:
        queue["authorization"] = {
            "basis": "user_directed_bounded_private_research_snapshot",
            "recorded_at": utc_now(),
            "publication": "not_authorized",
            "scope": "public unauthenticated robots-allowed GET pages and assets only",
        }
    args.private_research_snapshot = bool(
        args.private_research_snapshot
        or queue.get("authorization", {}).get("basis")
        == "user_directed_bounded_private_research_snapshot"
    )
    if args.status:
        write_queue(queue_path, queue)
        print(json.dumps({
            "status": queue.get("status"),
            "sites": len(queue["entries"]),
            "summary": queue_summary(queue),
            "queue": str(queue_path.relative_to(root)),
            "authorization": queue.get("authorization"),
            "fidelity_contract": {
                "tool": FIDELITY_TOOL_VERSION,
                "version": FIDELITY_CONTRACT_VERSION,
            },
            "quality_contract": {
                "tool": QUALITY_TOOL_VERSION,
                "version": QUALITY_CONTRACT_VERSION,
            },
        }, sort_keys=True))
        return 0

    recover_interrupted(queue, root, work_root)
    by_site = {entry["site"]: entry for entry in queue["entries"]}
    unknown_historical = sorted(set(args.historical_by_site) - set(by_site))
    if unknown_historical:
        raise ReplacementError(
            "historical specs contain sites outside the frozen queue: "
            + ", ".join(unknown_historical)
        )
    if args.sites:
        unknown = sorted(set(args.sites) - set(by_site))
        if unknown:
            raise ReplacementError("unknown replacement sites: " + ", ".join(unknown))
        if args.historical_by_site:
            missing_specs = sorted(set(args.sites) - set(args.historical_by_site))
            if missing_specs:
                raise ReplacementError(
                    "selected sites are missing historical specs: " + ", ".join(missing_specs)
                )
        candidates = [
            by_site[site]
            for site in args.sites
            if by_site[site]["status"] != "passed" or args.rebuild_passed
        ]
    else:
        allowed = {"pending"}
        if args.retry_failed:
            allowed.update({
                "failed", "rejected", "fidelity_failed", "stale_fidelity",
                "quality_failed", "stale_quality",
            })
        candidates = [entry for entry in queue["entries"] if entry["status"] in allowed]
        if args.historical_by_site:
            candidates = [
                entry for entry in candidates if entry["site"] in args.historical_by_site
            ]
    if args.limit is not None:
        candidates = candidates[: args.limit]
    write_queue(queue_path, queue)
    attempted = 0
    passed = 0
    for entry in candidates:
        attempted += 1
        print(
            f"[replace-generated {attempted}/{len(candidates)}] {entry['site']} "
            f"{entry.get('capture_url', entry['upstream_url'])}",
            flush=True,
        )
        if run_entry(queue, queue_path, entry, root, work_root, args):
            passed += 1
            print(f"{entry['site']}\tpassed\tpages={entry['pages']}\tassets={entry['assets']}", flush=True)
        else:
            print(f"{entry['site']}\t{entry['status']}\t{entry['error'].splitlines()[0]}", flush=True)
    write_queue(queue_path, queue)
    report = {
        "status": queue["status"],
        "attempted": attempted,
        "passed_this_run": passed,
        "sites": len(queue["entries"]),
        "summary": queue_summary(queue),
        "queue": str(queue_path.relative_to(root)),
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if queue["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReplacementError as exc:
        print(f"replace-generated: {exc}", file=sys.stderr)
        raise SystemExit(2)
