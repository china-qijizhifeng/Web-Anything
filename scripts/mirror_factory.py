#!/usr/bin/env python3
"""Acquire, seed, and audit WebHarbor mirrors with provenance evidence."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_http import request_loopback_url


DEFAULT_MANIFEST = ROOT / "mirror_factory" / "targets.jsonl"
DEFAULT_EVIDENCE_ROOT = ROOT / "mirror_factory" / "evidence"
CATALOG_PATH = ROOT / "site_catalog.json"
SITE_ID_RE = re.compile(r"^[a-z0-9_]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LOCAL_TASK_URL_RE = re.compile(
    r"^http://(?:localhost|127\.0\.0\.1):\d+(?:/[^\s]*)?$"
)
PHASES = (
    "discovered",
    "sourced",
    "imported",
    "seeded",
    "task_ready",
    "runtime_verified",
    "reviewed",
)
BASE_TASK_KEYS = {"web_name", "id", "ques", "web", "upstream_url"}
IGNORED_TREE_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "instance",
    "scraped_data",
    "venv",
}


class FactoryError(RuntimeError):
    """A deterministic mirror-factory contract failure."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_tree_files(path: Path):
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        if any(part in IGNORED_TREE_PARTS for part in relative.parts):
            continue
        if item.is_file() and not item.is_symlink():
            yield item, relative


def sha256_tree(path: Path) -> str:
    if not path.is_dir():
        raise FactoryError(f"tree does not exist: {path}")
    digest = hashlib.sha256()
    for item, relative in iter_tree_files(path):
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def sqlite_logical_differences(first: Path, second: Path, *, limit: int = 8) -> list[str]:
    differences: list[str] = []
    with sqlite3.connect(first) as left, sqlite3.connect(second) as right:
        left_tables = {
            row[0]
            for row in left.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
            )
        }
        right_tables = {
            row[0]
            for row in right.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
            )
        }
        if left_tables != right_tables:
            differences.append(
                f"tables differ: {sorted(left_tables)} != {sorted(right_tables)}"
            )
            return differences[:limit]
        for table in sorted(left_tables):
            quoted = '"' + table.replace('"', '""') + '"'
            columns = list(left.execute(f"PRAGMA table_info({quoted})"))
            column_names = [row[1] for row in columns]
            primary_keys = [row[1] for row in columns if row[5]]
            order = primary_keys or ["rowid"]
            order_sql = ", ".join('"' + item.replace('"', '""') + '"' for item in order)
            try:
                left_rows = list(left.execute(f"SELECT * FROM {quoted} ORDER BY {order_sql}"))
                right_rows = list(right.execute(f"SELECT * FROM {quoted} ORDER BY {order_sql}"))
            except sqlite3.OperationalError:
                left_rows = sorted(left.execute(f"SELECT * FROM {quoted}"), key=repr)
                right_rows = sorted(right.execute(f"SELECT * FROM {quoted}"), key=repr)
            if len(left_rows) != len(right_rows):
                differences.append(
                    f"{table}: row count {len(left_rows)} != {len(right_rows)}"
                )
                if len(differences) >= limit:
                    break
            for row_number, (left_row, right_row) in enumerate(
                zip(left_rows, right_rows), 1
            ):
                if left_row == right_row:
                    continue
                changed = [
                    column_names[index]
                    for index, values in enumerate(zip(left_row, right_row))
                    if values[0] != values[1]
                ]
                differences.append(
                    f"{table}[{row_number}]: changed columns {', '.join(changed)}"
                )
                if len(differences) >= limit:
                    break
            if len(differences) >= limit:
                break
    if not differences and sha256_file(first) != sha256_file(second):
        differences.append("logical rows match but SQLite file bytes differ")
    return differences[:limit]


def canonicalize_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        indexes = list(
            connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND sql IS NOT NULL ORDER BY name"
            )
        )
        for name, _ in indexes:
            quoted = '"' + name.replace('"', '""') + '"'
            connection.execute(f"DROP INDEX {quoted}")
        connection.commit()
        connection.execute("VACUUM")
        for _, sql in indexes:
            connection.execute(sql)
        connection.commit()
        connection.execute("VACUUM")


def manifest_sha256(path: Path) -> str:
    return sha256_file(path)


def load_targets(path: Path = DEFAULT_MANIFEST) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FactoryError(f"manifest does not exist: {path}")
    targets = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            target = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FactoryError(f"{path}:{line_number}: {exc}") from exc
        if not isinstance(target, dict):
            raise FactoryError(f"{path}:{line_number}: target must be an object")
        target["_line"] = line_number
        targets.append(target)
    return targets


def load_catalog_ids() -> set[str]:
    if not CATALOG_PATH.is_file():
        return set()
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {site["id"] for site in raw.get("sites", [])}


def _require_string(target: dict[str, Any], key: str) -> str:
    value = target.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FactoryError(f"line {target['_line']}: {key} must be a non-empty string")
    return value


def _effective_port(parsed) -> int | None:
    """Return the URL's effective port without changing its textual origin."""
    if not parsed.hostname:
        return None
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None


def same_upstream_origin(expected: str, candidate: Any) -> bool:
    if not isinstance(candidate, str):
        return False
    expected_url = urlparse(expected)
    candidate_url = urlparse(candidate)
    if candidate_url.scheme != "https" or not candidate_url.hostname:
        return False
    expected_host = (expected_url.hostname or "").casefold().removeprefix("www.")
    candidate_host = candidate_url.hostname.casefold().removeprefix("www.")
    return (
        candidate_host == expected_host
        and _effective_port(candidate_url) == _effective_port(expected_url)
    )


def validate_targets(
    targets: list[dict[str, Any]],
    *,
    allow_catalog_overlap: bool = False,
) -> dict[str, Any]:
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    catalog_ids = load_catalog_ids()
    source_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    for target in targets:
        site_id = _require_string(target, "id")
        if not SITE_ID_RE.fullmatch(site_id):
            raise FactoryError(f"line {target['_line']}: invalid site id: {site_id!r}")
        if site_id in seen_ids:
            raise FactoryError(f"line {target['_line']}: duplicate site id: {site_id}")
        seen_ids.add(site_id)
        # Catalog overlap is expected as targets advance through the pipeline.
        # It remains visible in the summary so callers can distinguish queued
        # targets from already registered ones.

        _require_string(target, "display_name")
        upstream_url = _require_string(target, "upstream_url")
        if not upstream_url.startswith("https://"):
            raise FactoryError(
                f"line {target['_line']}: upstream_url must be pinned to https: {upstream_url}"
            )
        if upstream_url in seen_urls:
            raise FactoryError(f"line {target['_line']}: duplicate upstream_url: {upstream_url}")
        seen_urls.add(upstream_url)

        source = target.get("source")
        if not isinstance(source, dict):
            raise FactoryError(f"line {target['_line']}: source must be an object")
        source_kind = source.get("kind")
        if source_kind not in {"git", "greenfield", "missing_code"}:
            raise FactoryError(f"line {target['_line']}: unsupported source kind: {source_kind}")
        source_counts[source_kind] += 1
        if source_kind == "git":
            for key in ("repo", "ref", "revision", "path"):
                value = source.get(key)
                if not isinstance(value, str) or not value:
                    raise FactoryError(f"line {target['_line']}: git source needs {key}")
            if not source["repo"].startswith("https://github.com/"):
                raise FactoryError(f"line {target['_line']}: git repo must be GitHub https URL")
            if not SHA_RE.fullmatch(source["revision"]):
                raise FactoryError(f"line {target['_line']}: git revision must be a 40-char SHA")
            expected_path = f"sites/{site_id}"
            if source["path"].rstrip("/") != expected_path:
                raise FactoryError(
                    f"line {target['_line']}: git path must be {expected_path!r}"
                )

        asset = target.get("asset")
        if not isinstance(asset, dict) or asset.get("kind") not in {
            "generated",
            "hf_tar",
            "missing",
        }:
            raise FactoryError(f"line {target['_line']}: unsupported asset contract")
        if asset["kind"] == "hf_tar":
            for key in ("repo", "revision", "filename"):
                if not isinstance(asset.get(key), str) or not asset[key]:
                    raise FactoryError(f"line {target['_line']}: hf_tar needs {key}")
            if not SHA_RE.fullmatch(asset["revision"]):
                raise FactoryError(f"line {target['_line']}: HF revision must be a 40-char SHA")
            if asset["filename"] != f"{site_id}.tar.gz":
                raise FactoryError(
                    f"line {target['_line']}: asset filename must be {site_id}.tar.gz"
                )
            expected_hash = asset.get("sha256")
            if expected_hash is not None and not (
                isinstance(expected_hash, str) and SHA256_RE.fullmatch(expected_hash)
            ):
                raise FactoryError(f"line {target['_line']}: invalid asset sha256")

        stage = target.get("initial_stage")
        if stage not in PHASES[:2]:
            raise FactoryError(
                f"line {target['_line']}: initial_stage must be discovered or sourced"
            )
        if stage == "sourced" and source_kind != "git":
            raise FactoryError(f"line {target['_line']}: sourced target must have git code")
        stage_counts[stage] += 1

        task_count = target.get("task_count")
        if task_count is not None and (not isinstance(task_count, int) or task_count < 0):
            raise FactoryError(f"line {target['_line']}: invalid task_count")

    return {
        "target_count": len(targets),
        "source_counts": dict(sorted(source_counts.items())),
        "initial_stage_counts": dict(sorted(stage_counts.items())),
        "catalog_overlap": sorted(seen_ids & catalog_ids),
    }


def target_map(targets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {target["id"]: target for target in targets}


def select_targets(
    targets: list[dict[str, Any]], site_ids: list[str]
) -> list[dict[str, Any]]:
    if not site_ids:
        return targets
    by_id = target_map(targets)
    unknown = sorted(set(site_ids) - set(by_id))
    if unknown:
        raise FactoryError(f"unknown target ids: {', '.join(unknown)}")
    return [by_id[site_id] for site_id in site_ids]


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    process = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode:
        rendered = " ".join(command)
        raise FactoryError(
            f"command failed ({process.returncode}): {rendered}\n{process.stderr.strip()}"
        )
    return process


def evidence_path(evidence_root: Path, site_id: str, phase: str) -> Path:
    return evidence_root / site_id / f"{phase}.json"


def write_evidence(
    manifest: Path,
    evidence_root: Path,
    target: dict[str, Any],
    phase: str,
    payload: dict[str, Any],
) -> Path:
    if phase not in {
        "import",
        "asset",
        "seed",
        "audit",
        "runtime-local",
        "runtime",
        "review",
    }:
        raise FactoryError(f"unsupported evidence phase: {phase}")
    path = evidence_path(evidence_root, target["id"], phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "site": target["id"],
        "phase": phase,
        "status": "pass",
        "generated_at": utc_now(),
        "manifest_sha256": manifest_sha256(manifest),
        "source_revision": target.get("source", {}).get("revision"),
        **payload,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def safe_archive_members(archive: tarfile.TarFile, source_path: str):
    prefix = PurePosixPath(source_path)
    members = []
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise FactoryError(f"unsafe git archive path: {member.name}")
        if (
            member.isdir()
            and len(path.parts) < len(prefix.parts)
            and prefix.parts[: len(path.parts)] == path.parts
        ):
            members.append(member)
            continue
        if path.parts[: len(prefix.parts)] != prefix.parts:
            raise FactoryError(f"git archive escaped source path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise FactoryError(f"unsupported git archive member: {member.name}")
        members.append(member)
    if not members:
        raise FactoryError(f"git archive contains no files under {source_path}")
    return members


def import_git_target(
    target: dict[str, Any],
    manifest: Path,
    evidence_root: Path,
    *,
    destination_root: Path = ROOT / "sites",
) -> Path:
    site_id = target["id"]
    source = target["source"]
    if source["kind"] != "git":
        raise FactoryError(f"{site_id}: source is not git")
    destination = destination_root / site_id
    if destination.exists():
        raise FactoryError(f"{site_id}: destination already exists: {destination}")

    with tempfile.TemporaryDirectory(prefix=f"webharbor-import-{site_id}-") as temp_raw:
        temp = Path(temp_raw)
        bare = temp / "source.git"
        run(["git", "init", "--bare", str(bare)])
        run(
            [
                "git",
                "--git-dir",
                str(bare),
                "fetch",
                "--depth=1",
                source["repo"],
                source["ref"],
            ]
        )
        actual_revision = run(
            ["git", "--git-dir", str(bare), "rev-parse", "FETCH_HEAD"]
        ).stdout.strip()
        if actual_revision != source["revision"]:
            raise FactoryError(
                f"{site_id}: source ref moved: expected {source['revision']}, got {actual_revision}"
            )
        archive_bytes = subprocess.run(
            [
                "git",
                "--git-dir",
                str(bare),
                "archive",
                "FETCH_HEAD",
                source["path"],
            ],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        extract_root = temp / "extract"
        extract_root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            members = safe_archive_members(archive, source["path"])
            archive.extractall(extract_root, members=members, filter="data")
        extracted = extract_root / source["path"]
        if not (extracted / "app.py").is_file():
            raise FactoryError(f"{site_id}: imported source has no app.py")
        shutil.copytree(
            extracted,
            destination,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "instance", "scraped_data", "venv"
            ),
        )

    tree_hash = sha256_tree(destination)
    write_evidence(
        manifest,
        evidence_root,
        target,
        "import",
        {
            "source": {key: source[key] for key in ("repo", "ref", "revision", "path")},
            "artifact": {
                "path": destination.relative_to(ROOT).as_posix(),
                "tree_sha256": tree_hash,
            },
        },
    )
    return destination


def _validate_asset_members(site_id: str, archive: tarfile.TarFile):
    allowed = {"instance_seed", "static"}
    safe = []
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise FactoryError(f"unsafe asset archive path: {member.name}")
        if path.parts == (site_id,) and member.isdir():
            safe.append(member)
            continue
        if len(path.parts) < 2 or path.parts[0] != site_id:
            raise FactoryError(f"asset escaped {site_id}/: {member.name}")
        if any(part == ".DS_Store" or part.startswith("._") for part in path.parts[1:]):
            continue
        if path.parts[1] not in allowed:
            raise FactoryError(f"unexpected asset root: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise FactoryError(f"unsupported asset member: {member.name}")
        safe.append(member)
    if not safe:
        raise FactoryError(f"{site_id}: asset archive has no usable files")
    return safe


def fetch_asset_target(
    target: dict[str, Any],
    manifest: Path,
    evidence_root: Path,
    *,
    cache_root: Path = ROOT / "sites" / ".cache" / "mirror_factory",
    destination_root: Path = ROOT / "sites",
) -> Path:
    site_id = target["id"]
    asset = target["asset"]
    if asset["kind"] != "hf_tar":
        raise FactoryError(f"{site_id}: asset source is not hf_tar")
    cache_dir = cache_root / asset["repo"].replace("/", "--") / asset["revision"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    tarball = cache_dir / asset["filename"]
    if not tarball.is_file():
        url = (
            f"https://huggingface.co/datasets/{asset['repo']}/resolve/"
            f"{asset['revision']}/{asset['filename']}"
        )
        part = tarball.with_suffix(tarball.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "webharbor-mirror-factory/1"})
        token = os.environ.get("HF_TOKEN")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=120) as response, part.open("wb") as out:
                shutil.copyfileobj(response, out)
            part.replace(tarball)
        except Exception:
            part.unlink(missing_ok=True)
            raise
    if tarball.stat().st_size == 0:
        raise FactoryError(f"{site_id}: downloaded empty asset archive")
    tar_hash = sha256_file(tarball)
    expected_hash = asset.get("sha256")
    if expected_hash and tar_hash != expected_hash:
        raise FactoryError(
            f"{site_id}: asset sha256 mismatch: expected {expected_hash}, got {tar_hash}"
        )
    with tempfile.TemporaryDirectory(prefix=f"webharbor-asset-{site_id}-") as temp_raw:
        staged_root = Path(temp_raw)
        with tarfile.open(tarball, "r:gz") as archive:
            members = _validate_asset_members(site_id, archive)
            archive.extractall(staged_root, members=members, filter="data")
        staged_site = staged_root / site_id
        for relative in (Path("instance_seed"), Path("static/images"), Path("static/external_cache")):
            source = staged_site / relative
            if source.exists():
                shutil.copytree(
                    source,
                    destination_root / site_id / relative,
                    dirs_exist_ok=True,
                )
    seed_db = destination_root / site_id / "instance_seed" / f"{site_id}.db"
    if not seed_db.is_file():
        raise FactoryError(f"{site_id}: asset archive did not produce {seed_db}")
    write_evidence(
        manifest,
        evidence_root,
        target,
        "asset",
        {
            "asset": {
                "repo": asset["repo"],
                "revision": asset["revision"],
                "filename": asset["filename"],
                "bytes": tarball.stat().st_size,
                "sha256": tar_hash,
            },
            "seed_db_sha256": sha256_file(seed_db),
        },
    )
    return tarball


def seed_target(
    target: dict[str, Any],
    manifest: Path,
    evidence_root: Path,
    *,
    python: str,
) -> Path:
    site_id = target["id"]
    source_dir = ROOT / "sites" / site_id
    if not (source_dir / "app.py").is_file():
        raise FactoryError(f"{site_id}: app.py is missing")
    with tempfile.TemporaryDirectory(prefix=f"webharbor-seed-{site_id}-") as temp_raw:
        def generate(label: str) -> tuple[Path, str]:
            temp_site = Path(temp_raw) / label / site_id
            shutil.copytree(
                source_dir,
                temp_site,
                ignore=shutil.ignore_patterns(
                    "instance", "instance_seed", "__pycache__", "venv"
                ),
            )
            command = [python, "-c", "import app"]
            run(command, cwd=temp_site)
            database = temp_site / "instance" / f"{site_id}.db"
            if not database.is_file():
                candidates = sorted((temp_site / "instance").glob("*.db"))
                if len(candidates) != 1:
                    raise FactoryError(
                        f"{site_id}: seeding did not produce one SQLite database"
                    )
                database = candidates[0]
            canonicalize_sqlite(database)
            initial_hash = sha256_file(database)
            run(command, cwd=temp_site)
            populated_hash = sha256_file(database)
            if initial_hash != populated_hash:
                raise FactoryError(
                    f"{site_id}: importing a populated DB changed bytes "
                    f"({initial_hash} -> {populated_hash})"
                )
            return database, initial_hash

        database, first_hash = generate("first")
        second_database, second_hash = generate("second")
        if first_hash != second_hash:
            logical = sqlite_logical_differences(database, second_database)
            raise FactoryError(
                f"{site_id}: clean seed generations differ ({first_hash} -> {second_hash}); "
                + "; ".join(logical)
            )
        destination = source_dir / "instance_seed" / f"{site_id}.db"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(database, destination)
    write_evidence(
        manifest,
        evidence_root,
        target,
        "seed",
        {
            "python": python,
            "seed_db": {
                "path": destination.relative_to(ROOT).as_posix(),
                "bytes": destination.stat().st_size,
                "sha256": sha256_file(destination),
            },
            "second_import_byte_identical": True,
            "clean_generation_byte_identical": True,
        },
    )
    return destination


def audit_target(
    target: dict[str, Any],
    manifest: Path,
    evidence_root: Path,
    *,
    write: bool,
) -> dict[str, Any]:
    site_id = target["id"]
    site_dir = ROOT / "sites" / site_id
    checks: dict[str, Any] = {}
    failures = []
    required_paths = [
        site_dir / "app.py",
        site_dir / "templates",
        site_dir / "tasks.jsonl",
        site_dir / "instance_seed" / f"{site_id}.db",
    ]
    missing = [path.relative_to(ROOT).as_posix() for path in required_paths if not path.exists()]
    checks["required_paths"] = {"pass": not missing, "missing": missing}
    if missing:
        failures.append("required_paths")

    syntax_errors = []
    python_files = list(site_dir.rglob("*.py")) if site_dir.is_dir() else []
    for path in python_files:
        try:
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
        except (SyntaxError, UnicodeDecodeError) as exc:
            syntax_errors.append(f"{path.relative_to(ROOT)}: {exc}")
    checks["python_syntax"] = {
        "pass": bool(python_files) and not syntax_errors,
        "file_count": len(python_files),
        "errors": syntax_errors,
    }
    if not checks["python_syntax"]["pass"]:
        failures.append("python_syntax")

    task_errors = []
    task_ids = set()
    task_count = 0
    tasks_path = site_dir / "tasks.jsonl"
    if tasks_path.is_file():
        for line_number, raw in enumerate(tasks_path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            task_count += 1
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                task_errors.append(f"line {line_number}: {exc}")
                continue
            missing_keys = sorted(BASE_TASK_KEYS - set(row))
            if missing_keys:
                task_errors.append(f"line {line_number}: missing {missing_keys}")
            task_id = row.get("id")
            if task_id in task_ids:
                task_errors.append(f"line {line_number}: duplicate id {task_id}")
            task_ids.add(task_id)
            if "answer" in row:
                task_errors.append(f"line {line_number}: answer must not live in tasks.jsonl")
            task_url = row.get("web")
            if not isinstance(task_url, str) or not LOCAL_TASK_URL_RE.fullmatch(task_url):
                task_errors.append(
                    f"line {line_number}: web must be a local benchmark URL"
                )
            upstream_url = row.get("upstream_url")
            if not same_upstream_origin(target["upstream_url"], upstream_url):
                task_errors.append(
                    f"line {line_number}: upstream_url is not on the manifest origin"
                )
            verifier_path = row.get("verifier_path")
            if verifier_path is not None:
                if not isinstance(verifier_path, str) or not (ROOT / verifier_path).is_file():
                    task_errors.append(
                        f"line {line_number}: verifier_path is missing: {verifier_path!r}"
                    )
    else:
        task_errors.append("tasks.jsonl missing")
    expected_count = target.get("task_count")
    if expected_count is not None and task_count != expected_count:
        task_errors.append(f"expected {expected_count} tasks, found {task_count}")
    checks["tasks"] = {"pass": task_count > 0 and not task_errors, "count": task_count, "errors": task_errors}
    if not checks["tasks"]["pass"]:
        failures.append("tasks")

    cross_imports = []
    pattern = re.compile(r"(?:from|import)\s+sites\.([a-z0-9_]+)")
    for path in python_files:
        for match in pattern.finditer(path.read_text(encoding="utf-8-sig")):
            if match.group(1) != site_id:
                cross_imports.append(f"{path.relative_to(ROOT)} -> {match.group(1)}")
    checks["site_isolation"] = {"pass": not cross_imports, "cross_imports": cross_imports}
    if cross_imports:
        failures.append("site_isolation")

    runtime_data_errors = []
    app_path = site_dir / "app.py"
    if app_path.is_file() and "scraped_data" in app_path.read_text(encoding="utf-8-sig"):
        runtime_data_errors.append("app.py references scraped_data")
    checks["runtime_data"] = {
        "pass": not runtime_data_errors,
        "errors": runtime_data_errors,
    }
    if runtime_data_errors:
        failures.append("runtime_data")

    seed_db = site_dir / "instance_seed" / f"{site_id}.db"
    sqlite_integrity = None
    if seed_db.is_file() and seed_db.stat().st_size > 0:
        try:
            with sqlite3.connect(f"file:{seed_db}?mode=ro", uri=True) as connection:
                sqlite_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            sqlite_integrity = f"error: {exc}"
    checks["seed_db"] = {
        "pass": sqlite_integrity == "ok",
        "bytes": seed_db.stat().st_size if seed_db.is_file() else 0,
        "sha256": sha256_file(seed_db) if seed_db.is_file() else None,
        "integrity_check": sqlite_integrity,
    }
    if not checks["seed_db"]["pass"]:
        failures.append("seed_db")

    result = {
        "site": site_id,
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "checks": checks,
        "tree_sha256": sha256_tree(site_dir) if site_dir.is_dir() else None,
    }
    if write:
        if failures:
            raise FactoryError(f"{site_id}: audit failed: {', '.join(failures)}")
        write_evidence(
            manifest,
            evidence_root,
            target,
            "audit",
            {key: value for key, value in result.items() if key != "status"},
        )
    return result


def current_phase(target: dict[str, Any], evidence_root: Path) -> str:
    site_id = target["id"]
    present = {
        phase
        for phase in ("import", "asset", "seed", "audit", "runtime", "review")
        if evidence_path(evidence_root, site_id, phase).is_file()
    }
    if "review" in present:
        return "reviewed"
    if "runtime" in present:
        return "runtime_verified"
    if "audit" in present:
        return "task_ready"
    if "seed" in present or "asset" in present:
        return "seeded"
    if "import" in present:
        return "imported"
    return target["initial_stage"]


def catalog_site_records() -> dict[str, dict[str, Any]]:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    base_port = raw["base_port"]
    records = {}
    for index, record in enumerate(raw["sites"]):
        records[record["id"]] = {**record, "port": base_port + index}
    return records


def read_json_url(url: str, *, method: str = "GET") -> tuple[int, dict[str, Any]]:
    try:
        status, body = request_loopback_url(url, method=method, timeout=30)
        return status, json.loads(body)
    except Exception as exc:
        raise FactoryError(f"request failed: {method} {url}: {exc}") from exc


def verify_local_runtime_target(
    target: dict[str, Any],
    manifest: Path,
    evidence_root: Path,
    *,
    control_url: str,
    require_dirty: bool,
    mutation_evidence: str | None,
    write: bool,
) -> dict[str, Any]:
    site_id = target["id"]
    record = catalog_site_records().get(site_id)
    if record is None:
        raise FactoryError(f"{site_id}: site is not registered in the catalog")
    port = record["port"]
    site_url = f"http://127.0.0.1:{port}/"
    site_dir = ROOT / "sites" / site_id
    seed_db = site_dir / "instance_seed" / f"{site_id}.db"
    runtime_db = site_dir / "instance" / f"{site_id}.db"
    if not seed_db.is_file() or not runtime_db.is_file():
        raise FactoryError(f"{site_id}: seed or runtime database is missing")

    seed_hash = sha256_file(seed_db)
    before_hash = sha256_file(runtime_db)
    if require_dirty and before_hash == seed_hash:
        raise FactoryError(f"{site_id}: runtime database is not dirty before reset")
    if require_dirty and not mutation_evidence:
        raise FactoryError(f"{site_id}: --mutation-evidence is required with --require-dirty")

    try:
        before_http, _ = request_loopback_url(
            site_url, timeout=15, read_limit=1
        )
    except Exception as exc:
        raise FactoryError(f"{site_id}: pre-reset homepage failed: {exc}") from exc

    reset_status, reset_payload = read_json_url(
        f"{control_url.rstrip('/')}/reset/{site_id}", method="POST"
    )
    health_status, health_payload = read_json_url(
        f"{control_url.rstrip('/')}/health"
    )
    try:
        after_http, _ = request_loopback_url(
            site_url, timeout=15, read_limit=1
        )
    except Exception as exc:
        raise FactoryError(f"{site_id}: post-reset homepage failed: {exc}") from exc

    after_hash = sha256_file(runtime_db)
    site_health = health_payload.get("sites", {}).get(site_id, {})
    checks = {
        "pre_reset_http_200": before_http == 200,
        "reset_http_200": reset_status == 200,
        "reset_ready": reset_payload.get("ready") is True,
        "control_health_200": health_status == 200,
        "site_alive": site_health.get("alive") is True,
        "post_reset_http_200": after_http == 200,
        "runtime_matches_seed_after_reset": after_hash == seed_hash,
        "runtime_was_dirty_before_reset": before_hash != seed_hash,
    }
    required = [key for key in checks if key != "runtime_was_dirty_before_reset"]
    if require_dirty:
        required.append("runtime_was_dirty_before_reset")
    failures = [key for key in required if not checks[key]]
    result = {
        "site": site_id,
        "status": "pass" if not failures else "fail",
        "scope": "local_control_plane",
        "advances_runtime_phase": False,
        "port": port,
        "control_url": control_url,
        "mutation_evidence": mutation_evidence,
        "before": {"runtime_db_sha256": before_hash, "seed_db_sha256": seed_hash},
        "after": {"runtime_db_sha256": after_hash, "seed_db_sha256": seed_hash},
        "reset": reset_payload,
        "health": health_payload,
        "checks": checks,
        "failures": failures,
    }
    if failures:
        raise FactoryError(f"{site_id}: local runtime checks failed: {', '.join(failures)}")
    if write:
        write_evidence(
            manifest,
            evidence_root,
            target,
            "runtime-local",
            {key: value for key, value in result.items() if key != "status"},
        )
    return result


def command_validate(args: argparse.Namespace) -> int:
    targets = load_targets(args.manifest)
    summary = validate_targets(targets, allow_catalog_overlap=args.allow_catalog_overlap)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def command_status(args: argparse.Namespace) -> int:
    targets = load_targets(args.manifest)
    validate_targets(targets, allow_catalog_overlap=True)
    selected = select_targets(targets, args.sites)
    counts: Counter[str] = Counter()
    for target in selected:
        phase = current_phase(target, args.evidence_root)
        counts[phase] += 1
        print(f"{target['id']}\t{phase}\t{target['source']['kind']}\t{target['upstream_url']}")
    print("summary\t" + json.dumps(dict(sorted(counts.items())), sort_keys=True))
    return 0


def command_import(args: argparse.Namespace) -> int:
    targets = load_targets(args.manifest)
    validate_targets(targets, allow_catalog_overlap=True)
    for target in select_targets(targets, args.sites):
        destination = import_git_target(target, args.manifest, args.evidence_root)
        print(f"{target['id']}\timported\t{destination}")
    return 0


def command_fetch_assets(args: argparse.Namespace) -> int:
    targets = load_targets(args.manifest)
    validate_targets(targets, allow_catalog_overlap=True)
    for target in select_targets(targets, args.sites):
        tarball = fetch_asset_target(target, args.manifest, args.evidence_root)
        print(f"{target['id']}\tasset\t{tarball}")
    return 0


def command_seed(args: argparse.Namespace) -> int:
    targets = load_targets(args.manifest)
    validate_targets(targets, allow_catalog_overlap=True)
    for target in select_targets(targets, args.sites):
        database = seed_target(
            target, args.manifest, args.evidence_root, python=args.python
        )
        print(f"{target['id']}\tseeded\t{database}")
    return 0


def command_audit(args: argparse.Namespace) -> int:
    targets = load_targets(args.manifest)
    validate_targets(targets, allow_catalog_overlap=True)
    failures = 0
    for target in select_targets(targets, args.sites):
        result = audit_target(
            target, args.manifest, args.evidence_root, write=args.write_evidence
        )
        print(json.dumps(result, sort_keys=True))
        failures += result["status"] != "pass"
    return 1 if failures else 0


def command_verify_local_runtime(args: argparse.Namespace) -> int:
    targets = load_targets(args.manifest)
    validate_targets(targets, allow_catalog_overlap=True)
    for target in select_targets(targets, args.sites):
        result = verify_local_runtime_target(
            target,
            args.manifest,
            args.evidence_root,
            control_url=args.control_url,
            require_dirty=args.require_dirty,
            mutation_evidence=args.mutation_evidence,
            write=args.write_evidence,
        )
        print(json.dumps(result, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__)
    top.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    top.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    commands = top.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate the target manifest")
    validate.add_argument("--allow-catalog-overlap", action="store_true")
    validate.set_defaults(handler=command_validate)

    status = commands.add_parser("status", help="show evidence-derived phases")
    status.add_argument("sites", nargs="*")
    status.set_defaults(handler=command_status)

    import_command = commands.add_parser("import", help="import pinned git site trees")
    import_command.add_argument("sites", nargs="+")
    import_command.set_defaults(handler=command_import)

    assets = commands.add_parser("fetch-assets", help="fetch pinned HF asset archives")
    assets.add_argument("sites", nargs="+")
    assets.set_defaults(handler=command_fetch_assets)

    seed = commands.add_parser("seed", help="generate and idempotency-check a seed DB")
    seed.add_argument("--python", default=sys.executable)
    seed.add_argument("sites", nargs="+")
    seed.set_defaults(handler=command_seed)

    audit = commands.add_parser("audit", help="run static site and task contracts")
    audit.add_argument("--write-evidence", action="store_true")
    audit.add_argument("sites", nargs="+")
    audit.set_defaults(handler=command_audit)

    local_runtime = commands.add_parser(
        "verify-local-runtime",
        help="verify local HTTP/control reset without advancing container runtime phase",
    )
    local_runtime.add_argument("--control-url", default="http://127.0.0.1:8101")
    local_runtime.add_argument("--require-dirty", action="store_true")
    local_runtime.add_argument("--mutation-evidence")
    local_runtime.add_argument("--write-evidence", action="store_true")
    local_runtime.add_argument("sites", nargs="+")
    local_runtime.set_defaults(handler=command_verify_local_runtime)
    return top


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except FactoryError as exc:
        print(f"mirror_factory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
