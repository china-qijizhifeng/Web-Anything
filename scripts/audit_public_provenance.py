#!/usr/bin/env python3
"""Compare the current WebAnything tree with a pinned public source tree.

This is an engineering provenance aid, not a copyright determination.  It
checks exact content matches, same-path similarity, likely moved/renamed text,
and legacy naming references.  The report always records both revisions and
the dirty working-tree state so its conclusions are reproducible.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_UPSTREAM_COMMIT = "7269be89c93b72cf1d8596b4fde65ba1c7453c15"
DEFAULT_UPSTREAM_URL = "https://github.com/aiming-lab/WebHarbor.git"
DEFAULT_OUTPUT = "docs/licensing/public-provenance-audit.json"
GENERATED_REPORTS = {
    DEFAULT_OUTPUT,
    "docs/licensing/public-provenance-history-audit.json",
}
TEXT_LIMIT = 2 * 1024 * 1024
HIGH_SIMILARITY = 0.80
MEDIUM_SIMILARITY = 0.50
SOURCE_SUFFIXES = {
    "", ".bash", ".c", ".cc", ".cfg", ".conf", ".css", ".csv", ".dockerfile",
    ".h", ".html", ".ini", ".java", ".js", ".json", ".jsonl", ".jsx", ".md",
    ".mjs", ".py", ".rst", ".sh", ".sql", ".svg", ".toml", ".ts", ".tsx",
    ".txt", ".xml", ".yaml", ".yml",
}
IGNORED_PARTS = {".git", ".pytest_cache", "__pycache__", "node_modules"}
LEGACY_PATTERN = re.compile(r"webharbor|websyn|nexgarden", re.IGNORECASE)
TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\w\s]", re.UNICODE)


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_text(path: Path, data: bytes) -> bool:
    if len(data) > TEXT_LIMIT or b"\0" in data[:8192]:
        return False
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    return suffix in SOURCE_SUFFIXES or name in {
        "dockerfile", "makefile", "license", "license-pending.md", "notice",
    }


def normalized_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def tokens(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_PATTERN.findall(text)]


def shingles(values: list[str], width: int = 5) -> set[tuple[str, ...]]:
    if len(values) < width:
        return {tuple(values)} if values else set()
    return {tuple(values[index:index + width]) for index in range(len(values) - width + 1)}


def path_family(path: Path) -> str:
    name = path.name.casefold()
    if name == "dockerfile":
        return "dockerfile"
    suffix = path.suffix.casefold()
    if suffix in {".md", ".rst", ".txt"}:
        return "prose"
    if suffix in {".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}:
        return "data"
    if suffix in {".html", ".css", ".svg"}:
        return "web"
    if suffix in {".js", ".mjs", ".jsx", ".ts", ".tsx"}:
        return "javascript"
    if suffix in {".py", ".sh", ".bash"}:
        return "script"
    return suffix or name


def repository_files(root: Path, excluded: set[str] | None = None) -> tuple[list[str], list[str]]:
    excluded = excluded or set()
    tracked = set(run_git(root, "ls-files", "-z").split("\0"))
    untracked = set(run_git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    paths = sorted(
        path for path in tracked | untracked
        if path and path not in excluded and (root / path).is_file()
    )
    deleted = sorted(path for path in tracked if path and not (root / path).exists())
    return paths, deleted


def directory_files(root: Path) -> list[str]:
    output = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        output.append(path.relative_to(root).as_posix())
    return sorted(output)


def load_records(root: Path, paths: Iterable[str]) -> list[dict[str, Any]]:
    records = []
    for relative in paths:
        path = root / relative
        data = path.read_bytes()
        records.append(build_record(path, relative, data))
    return records


def build_record(path: Path, relative: str, data: bytes) -> dict[str, Any]:
    text = normalized_text(data) if is_text(path, data) else None
    token_values = tokens(text) if text is not None else []
    return {
        "path": relative,
        "bytes": len(data),
        "sha256": sha256(data),
        "text": text,
        "tokens": token_values,
        "shingles": shingles(token_values),
        "family": path_family(path),
        "legacy_terms": sorted(set(match.group(0).casefold() for match in LEGACY_PATTERN.finditer(text or ""))),
    }


def load_git_records(root: Path, revision: str, excluded: set[str]) -> list[dict[str, Any]]:
    paths = sorted(
        path for path in run_git(root, "ls-tree", "-r", "--name-only", "-z", revision).split("\0")
        if path and path not in excluded
    )
    records = []
    for relative in paths:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"{revision}:{relative}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        records.append(build_record(Path(relative), relative, result.stdout))
    return records


def similarity(first: str, second: str) -> float:
    return difflib.SequenceMatcher(None, first, second, autojunk=True).ratio()


def candidate_ids(local: dict[str, Any], upstream: list[dict[str, Any]]) -> list[int]:
    candidates = []
    local_size = max(1, local["bytes"])
    local_name = Path(local["path"]).name.casefold()
    for index, record in enumerate(upstream):
        if record["text"] is None or record["family"] != local["family"]:
            continue
        size_ratio = record["bytes"] / local_size
        same_name = Path(record["path"]).name.casefold() == local_name
        shared = len(local["shingles"] & record["shingles"])
        smaller = max(1, min(len(local["shingles"]), len(record["shingles"])))
        containment = shared / smaller
        if same_name or (0.20 <= size_ratio <= 5.0 and containment >= 0.12):
            candidates.append(index)
    return candidates


def audit(local: list[dict[str, Any]], upstream: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path = {record["path"]: record for record in upstream}
    by_hash: dict[str, list[str]] = defaultdict(list)
    for record in upstream:
        by_hash[record["sha256"]].append(record["path"])

    findings = []
    for record in local:
        exact_paths = sorted(by_hash.get(record["sha256"], []))
        same_path = by_path.get(record["path"])
        best_path = None
        best_similarity = 0.0
        comparison_method = None

        if exact_paths:
            best_path = exact_paths[0]
            best_similarity = 1.0
            comparison_method = "exact_content"
        elif record["text"] is not None:
            candidates = candidate_ids(record, upstream)
            if same_path is not None and same_path["text"] is not None:
                same_index = upstream.index(same_path)
                if same_index not in candidates:
                    candidates.append(same_index)
            for index in candidates:
                upstream_record = upstream[index]
                score = similarity(record["text"], upstream_record["text"])
                if score > best_similarity:
                    best_path = upstream_record["path"]
                    best_similarity = score
                    comparison_method = "same_path" if best_path == record["path"] else "fuzzy_candidate"

        if exact_paths and record["bytes"] == 0:
            classification = "empty_placeholder_match"
        elif exact_paths:
            classification = "exact_public_match"
        elif best_similarity >= HIGH_SIMILARITY:
            classification = "high_public_similarity"
        elif best_similarity >= MEDIUM_SIMILARITY:
            classification = "medium_public_similarity"
        elif record["text"] is None:
            classification = "binary_no_exact_match"
        elif record["legacy_terms"]:
            classification = "legacy_name_only_or_unresolved"
        else:
            classification = "no_material_public_overlap_detected"

        findings.append({
            "path": record["path"],
            "bytes": record["bytes"],
            "sha256": record["sha256"],
            "text": record["text"] is not None,
            "legacy_terms": record["legacy_terms"],
            "same_path_upstream": same_path is not None,
            "exact_upstream_paths": exact_paths,
            "best_upstream_path": best_path,
            "best_similarity": round(best_similarity, 6),
            "comparison_method": comparison_method,
            "classification": classification,
        })
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--upstream-url", default=DEFAULT_UPSTREAM_URL)
    parser.add_argument("--upstream-commit", default=DEFAULT_UPSTREAM_COMMIT)
    parser.add_argument("--local-ref", help="audit a committed Git tree instead of the working tree")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    upstream_root = args.upstream_root.resolve()
    if not (root / ".git").exists():
        raise SystemExit(f"not a Git repository: {root}")
    if not upstream_root.is_dir():
        raise SystemExit(f"upstream tree not found: {upstream_root}")

    output = (args.output or root / DEFAULT_OUTPUT).resolve()
    excluded = set(GENERATED_REPORTS)
    try:
        excluded.add(output.relative_to(root).as_posix())
    except ValueError:
        pass
    if args.local_ref:
        local_records = load_git_records(root, args.local_ref, excluded)
        local_head = run_git(root, "rev-parse", args.local_ref).strip()
        deleted_paths: list[str] = []
        status = ""
        audit_scope = "git_revision"
    else:
        local_paths, deleted_paths = repository_files(root, excluded)
        local_records = load_records(root, local_paths)
        local_head = run_git(root, "rev-parse", "HEAD").strip()
        status = run_git(root, "status", "--porcelain=v1")
        audit_scope = "working_tree"
    upstream_paths = directory_files(upstream_root)
    upstream_records = load_records(upstream_root, upstream_paths)
    findings = audit(local_records, upstream_records)
    counts: dict[str, int] = defaultdict(int)
    for finding in findings:
        counts[finding["classification"]] += 1

    report = {
        "schema": "webanything-public-provenance-audit-v2",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "disclaimer": "Similarity evidence is an engineering signal, not a copyright determination.",
        "local": {
            "root": str(root),
            "scope": audit_scope,
            "requested_ref": args.local_ref,
            "head": local_head,
            "dirty": bool(status.strip()),
            "status": status.splitlines(),
            "files_audited": len(local_records),
            "deleted_tracked_paths": deleted_paths,
        },
        "upstream": {
            "root": str(upstream_root),
            "url": args.upstream_url,
            "commit": args.upstream_commit,
            "files_audited": len(upstream_records),
            "root_license_files": [
                name for name in ("LICENSE", "LICENSE.md", "COPYING", "NOTICE")
                if (upstream_root / name).is_file()
            ],
        },
        "thresholds": {"high_similarity": HIGH_SIMILARITY, "medium_similarity": MEDIUM_SIMILARITY},
        "summary": dict(sorted(counts.items())),
        "findings": findings,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps({"output": str(output), "summary": report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
