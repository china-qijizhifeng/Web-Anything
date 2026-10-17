#!/usr/bin/env python3
"""Build a bounded WebHarbor mirror from one public upstream URL."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from capture_upstream import (
    TOOL_VERSION as CAPTURE_TOOL_VERSION,
    CaptureSpec,
    CaptureRunner,
    LinkCollector,
    canonical_prerender_resource_url,
    normalize_url,
    prerender_resource_allowed,
    sha256_file,
)
from compile_snapshot import GENERATED_MARKER, SnapshotCompiler, catalog_port
from intake_upstream import (
    TOOL_VERSION as INTAKE_TOOL_VERSION,
    DiscoveryParser,
    IntakeError,
    build_get_form_candidates,
    derive_site_id,
    normalize_entry_url,
    run_intake,
    same_site_host,
)
from prerender_snapshot import (
    TOOL_VERSION as PRERENDER_TOOL_VERSION,
    pages_requiring_prerender,
    prerender_evidence_valid,
    effective_page_records,
    static_structure,
)


TOOL_VERSION = "webharbor-build-from-upstream-v34"
DEFAULT_STAGE_TIMEOUT_SECONDS = 600.0
MAX_PRERENDER_DISCOVERY_ROUNDS = 6
MAX_PRERENDER_RESOURCES = 2048
MAX_AUTO_CAPTURE_ASSETS = 2048
MAX_AUTO_PRESENTATION_HOSTS_PER_CAPTURE = 4
PRERENDER_TERMINATION_GRACE_SECONDS = 2.0
PRERENDER_RESOURCE_PRIORITIES = {
    "stylesheet": 0,
    "fetch": 1,
    "xhr": 1,
    "script": 1,
    "font": 2,
    "image": 3,
    "media": 3,
}
PRESENTATION_RESOURCE_TYPES = {"font", "image", "media", "stylesheet"}
STATIC_SCRIPT_WASM_LITERAL_RE = re.compile(
    r'''(?P<quote>["'`])(?P<url>(?:https?://|/|\./|\.\./)[^"'`\s\\]{1,2048}\.wasm(?:\?[^"'`\s\\]*)?)(?P=quote)''',
    re.IGNORECASE,
)
STAGES = (
    "environment", "intake", "manifest", "capture", "reconstruction", "seed",
    "catalog", "audit", "runtime_local", "fidelity", "quality", "package",
)


class PipelineError(RuntimeError):
    """The one-URL pipeline stopped at a traced stage boundary."""

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        kind: str = "pipeline_failure",
        resumable: bool = True,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.kind = kind
        self.resumable = resumable


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def same_origin(first: str, second: str) -> bool:
    return same_site_host(first, second)


def classify_failure(stage: str, message: str, rejected: bool = False) -> tuple[str, bool]:
    """Return a stable machine-facing failure class and whether retry can help."""
    folded = message.casefold()
    if stage == "environment":
        return "environment_prerequisite", True
    if "occupied" in folded or "address already in use" in folded:
        return "local_resource_conflict", True
    policy_markers = (
        "authorization basis", "robots", "access challenge", "challenge marker",
        "access-control", "non-public address", "private", "credentials are not allowed",
    )
    if rejected or any(marker in folded for marker in policy_markers):
        return "upstream_policy_rejection", False
    if stage in {"intake", "capture", "fidelity"}:
        return "upstream_or_fidelity_failure", True
    if stage == "quality":
        return "three_fidelity_quality_failure", True
    return "pipeline_failure", True


def result_exit_code(result: dict[str, Any]) -> int:
    if result.get("status") in {"passed", "stopped"}:
        return 0
    if result.get("failure_kind") == "upstream_policy_rejection":
        return 2
    if result.get("failure_kind") == "environment_prerequisite":
        return 3
    return 4


def read_targets(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise PipelineError(f"invalid target manifest line {line_number}: {exc}") from exc
    return rows


def write_targets(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_text(
        path,
        "".join(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n" for row in rows),
    )


def resolve_site_id(root: Path, url: str, requested: str | None) -> str:
    if requested:
        if not re.fullmatch(r"[a-z0-9_]+", requested):
            raise PipelineError("--site must contain lowercase letters, digits, or underscores")
        return requested
    base = derive_site_id(url)
    targets = read_targets(root / "mirror_factory" / "targets.jsonl")
    existing = {row["id"]: row.get("upstream_url", "") for row in targets}
    if base not in existing or same_origin(existing[base], url):
        return base
    marker = hashlib.sha256(normalize_entry_url(url).encode("utf-8")).hexdigest()[:8]
    return f"{base}_{marker}"


def display_name_from_intake(report: dict[str, Any], site: str) -> str:
    title = report.get("entry_page", {}).get("title", "").strip()
    if title and len(title) <= 100:
        parts = [title]
        for separator in (" | ", " - ", " — ", " :: "):
            if separator in title:
                parts = [part.strip() for part in title.split(separator) if part.strip()]
                break
        if parts:
            return min(parts, key=lambda part: (len(part) < 3, len(part)))
    return site.replace("_", " ").title()


def catalog_display_name(root: Path, site: str) -> str | None:
    path = root / "site_catalog.json"
    if not path.is_file():
        return None
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for record in catalog.get("sites", []):
        if record.get("id") == site:
            value = record.get("display_name")
            return value if isinstance(value, str) and value.strip() else None
    return None


def ensure_target(
    manifest: Path,
    *,
    site: str,
    display_name: str,
    upstream_url: str,
    task_count: int,
) -> dict[str, Any]:
    rows = read_targets(manifest)
    matches = [row for row in rows if row["id"] == site]
    if matches and not same_origin(matches[0]["upstream_url"], upstream_url):
        raise PipelineError(f"{site}: existing manifest target represents another upstream")
    target = matches[0] if matches else {
        "id": site,
        "display_name": display_name,
        "upstream_url": upstream_url,
        "purpose": f"Bounded offline mirror of public {display_name} information and navigation workflows.",
        "source": {"kind": "greenfield"},
        "asset": {"kind": "generated"},
        "initial_stage": "discovered",
        "task_count": task_count,
    }
    target.update({
        "display_name": display_name,
        "upstream_url": upstream_url,
        "purpose": f"Bounded offline mirror of public {display_name} information and navigation workflows.",
        "task_count": task_count,
        "capture": {
            "spec": f"mirror_factory/upstream_specs/{site}.json",
            "evidence": f"mirror_factory/evidence/{site}/upstream.json",
            "tool": CAPTURE_TOOL_VERSION,
        },
    })
    if not matches:
        rows.append(target)
    write_targets(manifest, rows)
    return target


def register_catalog(
    root: Path,
    *,
    site: str,
    display_name: str,
    task_count: int,
    seed_path: Path,
) -> int:
    path = root / "site_catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    records = catalog["sites"]
    existing_index = next((index for index, row in enumerate(records) if row["id"] == site), None)
    record = records[existing_index] if existing_index is not None else {"id": site}
    record.update({
        "display_name": display_name,
        "tier": "candidate_reconstructed",
        "code": {
            "path": f"sites/{site}",
            "provenance": {
                "kind": "automated_upstream_snapshot",
                "tool": TOOL_VERSION,
                "capture": {
                    "spec": f"mirror_factory/upstream_specs/{site}.json",
                    "evidence": f"mirror_factory/evidence/{site}/upstream.json",
                },
                "reconstruction": f"mirror_factory/evidence/{site}/reconstruction.json",
            },
        },
        "assets": {
            "kind": "generated",
            "generator": "app_import",
            "sha256": sha256_file(seed_path),
            "bytes": seed_path.stat().st_size,
        },
        "tasks": {
            "count": task_count,
            "deterministic_verifiers": False,
            "review_status": "candidate",
        },
    })
    if existing_index is None:
        records.append(record)
        existing_index = len(records) - 1
    atomic_json(path, catalog)
    return catalog["base_port"] + existing_index


def subprocess_stage(
    command: list[str],
    *,
    root: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: float = DEFAULT_STAGE_TIMEOUT_SECONDS,
) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        output = output.strip()
        raise PipelineError(
            f"command timed out after {timeout_seconds:g} seconds: {' '.join(command)}"
            + (f"\n{output[-12000:]}" if output else ""),
            kind="stage_timeout",
            resumable=True,
        ) from exc
    if completed.returncode != 0:
        output = completed.stdout.strip()
        raise PipelineError(
            f"command failed ({completed.returncode}): {' '.join(command)}"
            + (f"\n{output[-12000:]}" if output else "")
        )
    return completed.stdout.strip()


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = PRERENDER_TERMINATION_GRACE_SECONDS,
) -> None:
    """Terminate a start_new_session child and every process it spawned."""
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    deadline = time.monotonic() + grace_seconds
    while process_group_exists(process_group_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def supervised_process_group(
    command: list[str],
    *,
    root: Path,
    env: dict[str, str],
    timeout_seconds: float,
) -> str:
    """Run one worker with a deadline that also owns Chromium descendants."""
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        raise PipelineError(
            f"prerender worker timed out after {timeout_seconds:g} seconds"
            + (f"\n{partial[-12000:]}" if partial else ""),
            stage="capture",
            kind="capture_timeout",
            resumable=True,
        ) from exc
    except BaseException:
        terminate_process_group(process)
        raise

    # A worker can exit while a browser descendant remains alive. It still owns
    # that isolated process group, so clean the remainder before using outputs.
    if process_group_exists(process.pid):
        terminate_process_group(process)
    if process.returncode != 0:
        raise PipelineError(
            f"prerender worker failed ({process.returncode}): {' '.join(command)}"
            + (f"\n{output[-12000:]}" if output else ""),
            stage="capture",
            kind="prerender_worker_failed",
            resumable=True,
        )
    return output.strip()


@contextmanager
def capture_stage_timeout(timeout_seconds: float):
    """Apply one hard deadline to the complete in-process capture stage."""
    if not all(
        hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
    ):
        raise PipelineError(
            "capture hard timeout is unavailable on this platform",
            stage="capture",
            kind="capture_timeout",
            resumable=True,
        )

    try:
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        started_at = time.monotonic()

        def raise_timeout(_signum: int, _frame: Any) -> None:
            raise PipelineError(
                f"capture timed out after {timeout_seconds:g} seconds",
                stage="capture",
                kind="capture_timeout",
                resumable=True,
            )

        signal.signal(signal.SIGALRM, raise_timeout)
    except (AttributeError, ValueError) as exc:
        raise PipelineError(
            "capture hard timeout must run in the main process thread",
            stage="capture",
            kind="capture_timeout",
            resumable=True,
        ) from exc

    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        previous_delay, previous_interval = previous_timer
        if previous_delay > 0 or previous_interval > 0:
            elapsed = time.monotonic() - started_at
            signal.setitimer(
                signal.ITIMER_REAL,
                max(previous_delay - elapsed, 1e-6) if previous_delay > 0 else 0,
                previous_interval,
            )


def port_available(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def wait_port_available(port: int, timeout: float = 5.0) -> bool:
    """Wait briefly for a terminated site process group to release its listener."""
    deadline = time.monotonic() + timeout
    while True:
        if port_available(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def package_managed_assets(root: Path, site: str) -> dict[str, Any]:
    site_dir = root / "sites" / site
    selected = [
        site_dir / "mirror_contract.json",
        site_dir / "instance_seed",
        site_dir / "static" / "images",
        site_dir / "static" / "external_cache",
    ]
    files = sorted(
        path
        for candidate in selected
        if candidate.exists()
        for path in (
            [candidate]
            if candidate.is_file()
            else candidate.rglob("*")
        )
        if path.is_file() and not path.is_symlink()
    )
    if not files:
        raise PipelineError(f"{site}: no managed assets are available to package")
    output = root / ".assets" / "generated" / f"{site}.tar.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            relative = path.relative_to(site_dir)
            data = path.read_bytes()
            info = tarfile.TarInfo(f"{site}/{relative.as_posix()}")
            info.size = len(data)
            info.mode = path.stat().st_mode & 0o777
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as stream:
        stream.write(raw.getvalue())
    payload = compressed.getvalue()
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, output)
    return {
        "schema_version": 1,
        "tool": TOOL_VERSION,
        "phase": "asset_package",
        "site": site,
        "generated_at": utc_now(),
        "status": "passed",
        "artifact": {
            "path": output.relative_to(root).as_posix(),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "members": len(files),
        },
        "publication": {
            "status": "not_uploaded",
            "reason": "External dataset publication requires repository credentials and is not implicit.",
        },
    }


def restore_runtime_database(root: Path, site: str) -> dict[str, str]:
    """Restore state changed by quality workflows and prove byte identity."""
    seed = root / "sites" / site / "instance_seed" / f"{site}.db"
    runtime = root / "sites" / site / "instance" / f"{site}.db"
    if not seed.is_file():
        raise PipelineError(f"{site}: seed database is missing after quality verification")
    runtime.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        runtime.with_name(runtime.name + suffix).unlink(missing_ok=True)
    shutil.copy2(seed, runtime)
    seed_hash = sha256_file(seed)
    runtime_hash = sha256_file(runtime)
    if runtime_hash != seed_hash:
        raise PipelineError(f"{site}: quality workflow reset is not byte-identical")
    return {"seed_sha256": seed_hash, "runtime_sha256": runtime_hash}


def discover_captured_get_queries(
    root: Path,
    site: str,
    capture: dict[str, Any],
    existing: list[dict[str, Any]],
    *,
    max_queries: int = 2,
) -> list[dict[str, Any]]:
    """Find safe, grounded GET searches that only appear after the entry page."""
    remaining = max(0, max_queries - len(existing))
    if remaining == 0:
        return []
    capture_root = (
        root / "sites" / site / "static" / "external_cache" / "upstream"
    ).resolve()
    seen = {record["url"] for record in existing}
    discovered: list[dict[str, Any]] = []
    for page in capture.get("pages", []):
        if len(discovered) >= remaining:
            break
        local_path = page.get("local_path")
        final_url = page.get("final_url")
        if not isinstance(local_path, str) or not isinstance(final_url, str):
            continue
        source = (capture_root / local_path).resolve()
        if capture_root not in source.parents or not source.is_file():
            continue
        body = source.read_bytes()
        if hashlib.sha256(body).hexdigest() != page.get("sha256"):
            raise PipelineError(
                f"{site}: captured page hash changed before form discovery: {local_path}",
                stage="capture",
            )
        parser = DiscoveryParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        selected_kinds = {
            record.get("kind", "search") for record in [*existing, *discovered]
        }
        preferred_kind = None
        if "search" in selected_kinds and "filter" not in selected_kinds:
            preferred_kind = "filter"
        elif "filter" in selected_kinds and "search" not in selected_kinds:
            preferred_kind = "search"
        candidates = build_get_form_candidates(
            final_url,
            parser,
            max_candidates=remaining - len(discovered),
            preferred_kind=preferred_kind,
        )
        for candidate in candidates:
            if candidate["url"] in seen:
                continue
            candidate["discovered_after_capture"] = True
            seen.add(candidate["url"])
            discovered.append(candidate)
            if len(discovered) >= remaining:
                break
    return discovered


def extend_capture_spec_with_queries(
    spec: dict[str, Any],
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extend an intake spec with exact, bounded query URLs and host scope."""
    if not queries:
        return spec
    updated = json.loads(json.dumps(spec))
    start_urls = updated.setdefault("start_urls", [])
    generated = updated.setdefault("generated_get_queries", [])
    page_hosts = set(updated.setdefault("page_hosts", []))
    asset_hosts = set(updated.setdefault("asset_hosts", []))
    page_patterns = updated.setdefault("page_allow_patterns", [])
    asset_patterns = updated.setdefault("asset_allow_patterns", [])
    known_urls = {record["url"] for record in generated}
    for query in queries:
        if query["url"] in known_urls:
            continue
        generated.append(query)
        known_urls.add(query["url"])
        if query["url"] not in start_urls:
            start_urls.append(query["url"])
        host = urlsplit(query["url"]).hostname
        if host:
            page_hosts.add(host)
            asset_hosts.add(host)
            host_pattern = rf"^https?://{re.escape(host)}/"
            if host_pattern not in asset_patterns:
                asset_patterns.append(host_pattern)
        exact_pattern = "^" + re.escape(query["url"]) + "$"
        if exact_pattern not in page_patterns:
            page_patterns.append(exact_pattern)
    updated["page_hosts"] = sorted(page_hosts)
    updated["asset_hosts"] = sorted(asset_hosts)
    updated["max_pages"] = len(start_urls)
    updated["post_capture_query_budget"] = 2
    return updated


def discover_prerender_resources(
    spec: CaptureSpec,
    capture: dict[str, Any],
    *,
    max_resources: int = MAX_PRERENDER_RESOURCES,
) -> list[dict[str, str]]:
    """Promote blocked safe GET resources into a bounded second capture pass."""
    existing = {
        canonical_prerender_resource_url(record["url"])
        for record in spec.prerender_resources
    }
    existing.update(
        canonical_prerender_resource_url(value)
        for record in capture.get("assets", [])
        for key in ("url", "final_url")
        if isinstance((value := record.get(key)), str)
    )
    remaining = max(0, max_resources - len(spec.prerender_resources))
    discovered: list[dict[str, str]] = []
    prerender = capture.get("captured_dom_prerender", {})
    attempts = [
        *prerender.get("resource_attempts", []),
        *prerender.get("attempts", []),
    ]
    for attempt in attempts:
        source_page = attempt.get("url")
        if not isinstance(source_page, str):
            continue
        requests = sorted(
            attempt.get("blocked_requests", []),
            key=lambda request: (
                PRERENDER_RESOURCE_PRIORITIES.get(
                    inferred_prerender_resource_type(request), 99
                ),
                request.get("url", ""),
            ),
        )
        for request in requests:
            url = request.get("url")
            resource_type = inferred_prerender_resource_type(request)
            if (
                len(discovered) >= remaining
                or request.get("reason") != "uncaptured"
                or request.get("method") != "GET"
                or not isinstance(url, str)
                or not isinstance(resource_type, str)
                or resource_type not in PRERENDER_RESOURCE_PRIORITIES
                or canonical_prerender_resource_url(url) in existing
                or not prerender_resource_allowed(
                    url,
                    asset_hosts=spec.asset_hosts,
                    asset_patterns=spec.asset_patterns,
                )
            ):
                continue
            url = canonical_prerender_resource_url(url)
            existing.add(url)
            discovered.append({
                "url": url,
                "source_page": source_page,
                "method": "GET",
                "resource_type": resource_type,
                "source": "blocked_captured_dom_request",
            })
    return discovered


def discover_captured_runtime_resources(
    root: Path,
    site: str,
    spec: CaptureSpec,
    capture: dict[str, Any],
    *,
    max_resources: int = 32,
) -> list[dict[str, str]]:
    """Find bounded same-site runtime binaries in verified pages and scripts."""
    capture_root = (
        root / "sites" / site / "static" / "external_cache" / "upstream"
    ).resolve()
    captured_urls = {
        canonical_prerender_resource_url(value)
        for record in capture.get("assets", [])
        for key in ("url", "final_url")
        if isinstance((value := record.get(key)), str)
    }
    discovered: list[dict[str, str]] = []
    seen = set(captured_urls)

    def add(raw_url: str, base_url: str, source_page: str, source: str) -> None:
        normalized = normalize_url(base_url, raw_url)
        if normalized is None:
            return
        normalized = canonical_prerender_resource_url(normalized)
        host = (urlsplit(normalized).hostname or "").casefold()
        if (
            len(discovered) >= max_resources
            or normalized in seen
            or host not in spec.page_hosts
            or not spec.asset_allowed(normalized)
        ):
            return
        seen.add(normalized)
        discovered.append({
            "url": normalized,
            "source_page": source_page,
            "method": "GET",
            "resource_type": "fetch",
            "source": source,
        })

    for record in sorted(capture.get("pages", []), key=lambda item: item.get("url", "")):
        source_url = record.get("final_url") or record.get("url")
        local_path = record.get("local_path")
        expected_hash = record.get("sha256")
        if not all(isinstance(value, str) for value in (source_url, local_path, expected_hash)):
            continue
        path = (capture_root / local_path).resolve()
        if capture_root not in path.parents or not path.is_file():
            raise PipelineError(f"captured page is missing or outside scope: {local_path}")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != expected_hash:
            raise PipelineError(f"captured page hash changed: {local_path}")
        collector = LinkCollector()
        collector.feed(body.decode("utf-8", errors="replace"))
        for raw_url, _ in collector.prioritized_asset_candidates():
            if Path(urlsplit(raw_url).path).suffix.casefold() not in {".lottie", ".wasm"}:
                continue
            add(raw_url, source_url, source_url, "captured_page_runtime_literal")

    for record in sorted(capture.get("assets", []), key=lambda item: item.get("url", "")):
        content_type = str(record.get("content_type", "")).casefold()
        source_url = record.get("final_url") or record.get("url")
        local_path = record.get("local_path")
        expected_hash = record.get("sha256")
        if (
            "javascript" not in content_type
            or not isinstance(source_url, str)
            or not isinstance(local_path, str)
            or not isinstance(expected_hash, str)
        ):
            continue
        path = (capture_root / local_path).resolve()
        if capture_root not in path.parents or not path.is_file():
            raise PipelineError(f"captured script is missing or outside scope: {local_path}")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != expected_hash:
            raise PipelineError(f"captured script hash changed: {local_path}")
        text = body.decode("utf-8", errors="replace")
        for match in STATIC_SCRIPT_WASM_LITERAL_RE.finditer(text):
            add(
                match.group("url"),
                source_url,
                spec.upstream_url,
                "captured_script_literal",
            )
    return discovered


def inferred_prerender_resource_type(request: dict[str, Any]) -> str | None:
    """Recover Chromium's occasional `other` classification from URL suffixes."""
    resource_type = request.get("resource_type")
    if resource_type in PRERENDER_RESOURCE_PRIORITIES:
        return str(resource_type)
    url = request.get("url")
    if resource_type != "other" or not isinstance(url, str):
        return None
    suffix = Path(urlsplit(url).path).suffix.casefold()
    if suffix == ".css":
        return "stylesheet"
    if suffix in {".js", ".mjs"}:
        return "script"
    if suffix in {".eot", ".otf", ".ttf", ".woff", ".woff2"}:
        return "font"
    if suffix in {
        ".avif", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"
    }:
        return "image"
    if suffix in {".mp3", ".mp4", ".ogg", ".webm"}:
        return "media"
    return None


def extend_capture_spec_with_same_site_prerender_scope(
    spec: dict[str, Any], capture: dict[str, Any]
) -> dict[str, Any]:
    """Add exact safe blocked GET URLs to the bounded asset scope.

    The capture runner still performs an independent robots check for every new
    host. Same-site GET resources are eligible by type; external hosts are
    limited to presentation resources. Exact URL patterns keep API/CDN discovery
    bounded and prevent one observed request from becoming broad host access.
    """
    updated = json.loads(json.dumps(spec))
    page_hosts = {str(host).casefold() for host in updated.get("page_hosts", [])}
    asset_hosts = {str(host).casefold() for host in updated.get("asset_hosts", [])}
    patterns = list(updated.get("asset_allow_patterns", []))
    known_patterns = set(patterns)
    added_external_hosts: set[str] = set()
    prerender = capture.get("captured_dom_prerender", {})
    attempts = [
        *prerender.get("resource_attempts", []),
        *prerender.get("attempts", []),
    ]
    for attempt in attempts:
        source_page = attempt.get("url")
        if not isinstance(source_page, str) or (
            urlsplit(source_page).hostname or ""
        ).casefold() not in page_hosts:
            continue
        for request in attempt.get("blocked_requests", []):
            url = request.get("url")
            resource_type = inferred_prerender_resource_type(request)
            if (
                request.get("reason") != "uncaptured"
                or request.get("method") != "GET"
                or resource_type not in PRERENDER_RESOURCE_PRIORITIES
                or not isinstance(url, str)
            ):
                continue
            host = (urlsplit(url).hostname or "").casefold()
            already_scoped = host in asset_hosts
            same_site = same_site_host(source_page, url)
            presentation = resource_type in PRESENTATION_RESOURCE_TYPES
            if not (already_scoped or same_site or presentation):
                continue
            if (
                not already_scoped
                and not same_site
                and host not in added_external_hosts
                and len(added_external_hosts) >= MAX_AUTO_PRESENTATION_HOSTS_PER_CAPTURE
            ):
                continue
            exact_pattern = "^" + re.escape(canonical_prerender_resource_url(url)) + "$"
            if not host or not prerender_resource_allowed(
                canonical_prerender_resource_url(url),
                asset_hosts=frozenset({host}),
                asset_patterns=(re.compile(exact_pattern),),
            ):
                continue
            asset_hosts.add(host)
            if not already_scoped and not same_site:
                added_external_hosts.add(host)
            if exact_pattern not in known_patterns:
                patterns.append(exact_pattern)
                known_patterns.add(exact_pattern)
    updated["asset_hosts"] = sorted(asset_hosts)
    updated["asset_allow_patterns"] = patterns
    return updated


def extend_capture_spec_with_prerender_resources(
    spec: dict[str, Any], resources: list[dict[str, str]]
) -> dict[str, Any]:
    updated = json.loads(json.dumps(spec))
    records = updated.setdefault("prerender_resources", [])
    known = {record["url"] for record in records}
    added = 0
    for resource in resources:
        if resource["url"] not in known:
            records.append(resource)
            known.add(resource["url"])
            added += 1
    if added:
        current_limit = int(updated.get("max_assets", 200))
        updated["max_assets"] = min(
            MAX_AUTO_CAPTURE_ASSETS, current_limit + added
        )
    return updated


def wait_http(port: int, process: subprocess.Popen[Any], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PipelineError(f"local fidelity server exited with status {process.returncode}")
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/_health")
            response = connection.getresponse()
            response.read(1)
            if response.status == 200:
                return
        except (OSError, http.client.HTTPException):
            time.sleep(0.15)
        finally:
            if connection is not None:
                connection.close()
    raise PipelineError("local fidelity server did not become ready")


def meaningful_fidelity_candidate(
    root: Path,
    site: str,
    page: dict[str, Any],
) -> bool:
    """Return whether a supplemental capture is useful for static fidelity sampling."""
    local_path = page.get("local_path")
    if not isinstance(local_path, str):
        # Preserve compatibility with callers that provide a minimal capture record.
        return True
    capture_root = (
        root / "sites" / site / "static" / "external_cache" / "upstream"
    ).resolve()
    path = (capture_root / local_path).resolve()
    if capture_root not in path.parents or not path.is_file():
        raise PipelineError(f"captured fidelity candidate is missing or outside scope: {local_path}")
    body = path.read_bytes()
    expected_hash = page.get("sha256")
    if isinstance(expected_hash, str) and hashlib.sha256(body).hexdigest() != expected_hash:
        raise PipelineError(f"captured fidelity candidate hash changed: {local_path}")
    return bool(static_structure(body)["meaningful"])


def fidelity_spec(
    root: Path,
    *,
    site: str,
    port: int,
    capture: dict[str, Any],
    upstream_user_agent: str | None = None,
) -> Path:
    pages = effective_page_records(capture)
    scope = capture.get("scope", {})
    start_urls = scope.get("start_urls", [])
    entry_start_url = start_urls[0] if start_urls else None
    entry_page = next(
        (
            page
            for page in pages
            if page.get("url") == entry_start_url or page.get("final_url") == entry_start_url
        ),
        None,
    )
    selected_pages = []
    selected_local_routes: set[str] = set()
    if entry_page is not None:
        selected_pages.append((entry_page, True))
        entry_local_url = scope.get("upstream_url")
        if isinstance(entry_local_url, str):
            parsed_entry = urlsplit(entry_local_url)
            selected_local_routes.add(
                (parsed_entry.path or "/")
                + (("?" + parsed_entry.query) if parsed_entry.query else "")
            )
    primary_host = urlsplit(scope.get("upstream_url", "")).hostname
    start_order = {
        url: index for index, url in enumerate(scope.get("start_urls", []))
    }
    remaining_pages = sorted(
        enumerate(pages),
        key=lambda item: (
            urlsplit(item[1].get("final_url", "")).hostname != primary_host,
            start_order.get(item[1].get("url"), len(start_order)),
            item[0],
        ),
    )
    for _, page in remaining_pages:
        if page is entry_page:
            continue
        if not meaningful_fidelity_candidate(root, site, page):
            continue
        parsed_page = urlsplit(page["final_url"])
        local_route = (parsed_page.path or "/") + (
            ("?" + parsed_page.query) if parsed_page.query else ""
        )
        if local_route in selected_local_routes:
            continue
        selected_local_routes.add(local_route)
        selected_pages.append((page, False))
        if len(selected_pages) >= 3:
            break

    routes = []
    for index, (page, is_entry) in enumerate(selected_pages[:3]):
        upstream_route_url = page.get("url") or page["final_url"]
        local_route_url = scope.get("upstream_url") if is_entry else page["final_url"]
        if not isinstance(local_route_url, str):
            local_route_url = page["final_url"]
        parsed = urlsplit(local_route_url)
        local_path = parsed.path or "/"
        if parsed.query:
            local_path += "?" + parsed.query
        routes.append({
            "name": "home" if index == 0 else f"page-{index}",
            "upstream_url": upstream_route_url,
            "local_url": f"http://127.0.0.1:{port}{local_path}",
            "wait_for": "body",
            "required_landmarks": "match_upstream",
        })
    spec = {
        "site": site,
        "output_dir": f"sites/{site}/static/external_cache/fidelity",
        "viewports": [
            {"name": "desktop", "width": 1440, "height": 900},
        ],
        "minimum_pixel_similarity": 0.75,
        "minimum_nonblank_retention": 0.5,
        "require_complete_resources": True,
        "runtime_javascript_check": True,
        "upstream_user_agent": upstream_user_agent,
        "upstream_capture_replay": True,
        "routes": routes,
    }
    path = root / "mirror_factory" / "fidelity_specs" / f"{site}.json"
    atomic_json(path, spec)
    return path


class PipelineState:
    def __init__(
        self,
        path: Path,
        site: str,
        upstream_url: str,
        capture_url: str | None = None,
    ) -> None:
        capture_url = capture_url or upstream_url
        self.path = path
        if path.is_file():
            self.value = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.value = {
                "schema_version": 1,
                "tool": TOOL_VERSION,
                "phase": "automation",
                "site": site,
                "upstream_url": upstream_url,
                "capture_url": capture_url,
                "created_at": utc_now(),
                "status": "running",
                "stages": {},
            }
        if (
            self.value.get("site") != site
            or not same_origin(self.value.get("upstream_url", ""), upstream_url)
            or not same_origin(upstream_url, capture_url)
        ):
            raise PipelineError("automation evidence belongs to another target")
        self.value["tool"] = TOOL_VERSION
        self.value["upstream_url"] = upstream_url
        self.value["capture_url"] = capture_url

    def stage(self, name: str, status: str, **details: Any) -> None:
        if status in {"running", "passed"}:
            self.value["status"] = "running"
            for key in (
                "completed_at", "failed_stage", "failure_kind", "resumable",
                "result_tier", "quality_grade", "review_gate",
            ):
                self.value.pop(key, None)
        elif status == "rejected":
            self.value["status"] = "rejected"
        else:
            self.value["status"] = "failed"
        if status == "running":
            self.value["active_stage"] = name
        elif self.value.get("active_stage") == name:
            self.value.pop("active_stage", None)
        self.value["updated_at"] = utc_now()
        self.value["stages"][name] = {"status": status, "at": utc_now(), **details}
        atomic_json(self.path, self.value)

    def fail(
        self,
        stage: str,
        *,
        status: str,
        error: str,
        kind: str,
        resumable: bool,
    ) -> None:
        self.value["status"] = status
        self.value["updated_at"] = utc_now()
        self.value["failed_stage"] = stage
        self.value["failure_kind"] = kind
        self.value["resumable"] = resumable
        self.value.pop("active_stage", None)
        previous = self.value["stages"].get(stage, {})
        self.value["stages"][stage] = {
            **previous,
            "status": status,
            "at": utc_now(),
            "error": error,
            "failure_kind": kind,
            "resumable": resumable,
        }
        atomic_json(self.path, self.value)

    def complete(self, *, quality_grade: str | None = None) -> None:
        for key in ("active_stage", "failed_stage", "failure_kind", "resumable"):
            self.value.pop(key, None)
        self.value["status"] = "passed"
        self.value["completed_at"] = utc_now()
        self.value["result_tier"] = "candidate_reconstructed"
        if quality_grade is not None:
            self.value["quality_grade"] = quality_grade
        self.value["review_gate"] = (
            "Reviewer-owned deterministic verifiers and rubrics are still required before reviewed status."
        )
        atomic_json(self.path, self.value)


class BuildPipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = repository_root()
        self.url = normalize_entry_url(args.url)
        self.upstream_url = normalize_entry_url(args.represented_url or args.url)
        if not same_origin(self.url, self.upstream_url):
            raise PipelineError("--represented-url must have the same origin as the capture URL")
        self.site = resolve_site_id(self.root, self.upstream_url, args.site)
        self.evidence_dir = self.root / "mirror_factory" / "evidence" / self.site
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.state = PipelineState(
            self.evidence_dir / "automation.json",
            self.site,
            self.upstream_url,
            self.url,
        )
        self.spec_path = self.root / "mirror_factory" / "upstream_specs" / f"{self.site}.json"
        self.capture_path = self.evidence_dir / "upstream.json"
        self.manifest = self.root / "mirror_factory" / "targets.jsonl"
        self.result_path = self.evidence_dir / "result.json"

    def stop_after(self, stage: str) -> bool:
        return self.args.stop_after == stage

    def run(self) -> dict[str, Any]:
        try:
            self.verify_environment()
            if self.stop_after("environment"):
                return self.partial()
            intake_report, spec = self.intake()
            if self.stop_after("intake"):
                return self.partial()
            display_name = self.args.display_name or (
                catalog_display_name(self.root, self.site)
                if self.args.replace_existing
                else None
            ) or display_name_from_intake(intake_report, self.site)
            canonical_url = self.upstream_url
            self.state.stage("manifest", "running")
            target = ensure_target(
                self.manifest,
                site=self.site,
                display_name=display_name,
                upstream_url=canonical_url,
                task_count=1,
            )
            self.state.stage("manifest", "passed", path="mirror_factory/targets.jsonl")
            if self.stop_after("manifest"):
                return self.partial()

            capture = self.capture(spec)
            if self.stop_after("capture"):
                return self.partial()

            port = catalog_port(self.root, self.site)
            self.state.stage("reconstruction", "running")
            compiler = SnapshotCompiler(self.root, self.site, self.capture_path)
            reconstruction = compiler.compile(
                display_name=display_name,
                port=port,
                replace=self.args.replace_existing,
            )
            display_name = reconstruction["display_name"]
            reconstruction_path = self.evidence_dir / "reconstruction.json"
            atomic_json(reconstruction_path, reconstruction)
            self.state.stage(
                "reconstruction", "passed", pages=reconstruction["pages"],
                assets=reconstruction["assets"], tasks=reconstruction["tasks"],
                mirror_contract=reconstruction["mirror_contract"],
                evidence=str(reconstruction_path.relative_to(self.root)),
            )
            target = ensure_target(
                self.manifest,
                site=self.site,
                display_name=display_name,
                upstream_url=canonical_url,
                task_count=reconstruction["tasks"],
            )
            if self.stop_after("reconstruction"):
                return self.partial()

            self.state.stage("seed", "running")
            output = subprocess_stage([
                str(self.root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
                "seed", "--python", self.args.python, self.site,
            ], root=self.root, env=self.runtime_env(), timeout_seconds=self.args.stage_timeout)
            seed_path = self.root / "sites" / self.site / "instance_seed" / f"{self.site}.db"
            self.state.stage(
                "seed", "passed", output=output[-4000:],
                bytes=seed_path.stat().st_size, sha256=sha256_file(seed_path),
            )
            if self.stop_after("seed"):
                return self.partial()

            self.state.stage("catalog", "running")
            port = register_catalog(
                self.root,
                site=self.site,
                display_name=display_name,
                task_count=reconstruction["tasks"],
                seed_path=seed_path,
            )
            self.state.stage("catalog", "passed", port=port, tier="candidate_reconstructed")
            if self.stop_after("catalog"):
                return self.partial()

            self.state.stage("audit", "running")
            output = subprocess_stage([
                str(self.root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
                "audit", "--write-evidence", self.site,
            ], root=self.root, env=self.runtime_env(), timeout_seconds=self.args.stage_timeout)
            self.state.stage("audit", "passed", output=output[-4000:])
            if self.stop_after("audit"):
                return self.partial()

            self.state.stage("runtime_local", "running")
            command = [
                self.args.python,
                str(self.root / "scripts/verify_local_runtime_batches.py"),
                "--batch-size", "1", "--take-over-stale", self.site,
            ]
            if self.args.dependency_path:
                command[2:2] = ["--dependency-path", self.args.dependency_path]
            output = subprocess_stage(
                command,
                root=self.root,
                env=self.runtime_env(),
                timeout_seconds=self.args.stage_timeout,
            )
            self.state.stage("runtime_local", "passed", output=output[-6000:])
            if self.stop_after("runtime_local"):
                return self.partial()

            self.run_fidelity(capture, port)
            if self.stop_after("fidelity"):
                return self.partial()
            quality = self.run_quality(port)
            if self.stop_after("quality"):
                return self.partial()
            self.state.stage("package", "running")
            package = package_managed_assets(self.root, self.site)
            package_path = self.evidence_dir / "asset-package.json"
            atomic_json(package_path, package)
            self.state.stage(
                "package", "passed", artifact=package["artifact"],
                evidence=str(package_path.relative_to(self.root)),
            )
            self.state.complete(quality_grade=quality["grade"])
            result = {
                "site": self.site,
                "status": "passed",
                "tier": "candidate_reconstructed",
                "port": port,
                "pages": reconstruction["pages"],
                "assets": reconstruction["assets"],
                "tasks": reconstruction["tasks"],
                "quality_grade": quality["grade"],
                "quality_evidence": f"mirror_factory/evidence/{self.site}/quality.json",
                "mirror_contract": reconstruction["mirror_contract"]["path"],
                "review_status": "candidate",
                "review_required": True,
                "workflow_gaps": f"sites/{self.site}/workflow_gaps.json",
                "automation_evidence": str(self.state.path.relative_to(self.root)),
                "result_evidence": str(self.result_path.relative_to(self.root)),
            }
            atomic_json(self.result_path, result)
            return result
        except Exception as exc:
            failed_stage = (
                getattr(exc, "stage", None)
                or self.state.value.get("active_stage")
                or next(
                    (
                        name for name in reversed(STAGES)
                        if self.state.value["stages"].get(name, {}).get("status")
                        in {"running", "rejected", "failed"}
                    ),
                    "pipeline",
                )
            )
            previous_status = self.state.value["stages"].get(failed_stage, {}).get("status")
            terminal_status = "rejected" if previous_status == "rejected" else "failed"
            if isinstance(exc, PipelineError) and exc.kind != "pipeline_failure":
                kind, resumable = exc.kind, exc.resumable
            else:
                kind, resumable = classify_failure(
                    failed_stage, str(exc), rejected=terminal_status == "rejected"
                )
            self.state.fail(
                failed_stage,
                status=terminal_status,
                error=str(exc),
                kind=kind,
                resumable=resumable,
            )
            result = self.failure_result(
                status=terminal_status,
                stage=failed_stage,
                kind=kind,
                resumable=resumable,
                error=str(exc),
            )
            atomic_json(self.result_path, result)
            raise PipelineError(
                str(exc), stage=failed_stage, kind=kind, resumable=resumable
            ) from exc

    def runtime_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.args.dependency_path:
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = self.args.dependency_path + (
                os.pathsep + existing if existing else ""
            )
        return env

    def verify_environment(self) -> None:
        self.state.stage("environment", "running")
        stop_index = STAGES.index(self.args.stop_after) if self.args.stop_after else len(STAGES)
        details: dict[str, Any] = {
            "skill_python": sys.executable,
            "runtime_python": self.args.python,
            "dependency_path": self.args.dependency_path,
        }
        if stop_index >= STAGES.index("seed"):
            completed = subprocess.run(
                [self.args.python, "-c", "import flask, flask_sqlalchemy"],
                cwd=self.root,
                env=self.runtime_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if completed.returncode:
                raise PipelineError(
                    "runtime Python cannot import Flask and Flask-SQLAlchemy; install the "
                    "pinned Dockerfile dependencies or set WEBHARBOR_RUNTIME_DEPENDENCY_PATH"
                )
            details["runtime_imports"] = "passed"
        if stop_index >= STAGES.index("capture"):
            try:
                import playwright.sync_api  # noqa: F401
            except ImportError as exc:
                raise PipelineError(
                    "skill Python cannot import Playwright; set "
                    "WEBHARBOR_SKILL_PYTHON to the browser-verification environment"
                ) from exc
            details["capture_browser_imports"] = "passed"
        if stop_index >= STAGES.index("fidelity"):
            try:
                import PIL  # noqa: F401
            except ImportError as exc:
                raise PipelineError(
                    "skill Python cannot import Pillow; set WEBHARBOR_SKILL_PYTHON "
                    "to the browser-verification environment"
                ) from exc
            details["fidelity_image_imports"] = "passed"
        self.state.stage("environment", "passed", **details)

    def intake(self) -> tuple[dict[str, Any], dict[str, Any]]:
        intake_path = self.evidence_dir / "intake.json"
        if self.args.resume and intake_path.is_file() and self.spec_path.is_file():
            report = json.loads(intake_path.read_text(encoding="utf-8"))
            spec = json.loads(self.spec_path.read_text(encoding="utf-8"))
            previous_capture_entry = (
                spec.get("capture_entry_url")
                or report.get("capture_entry_url")
                or next(iter(spec.get("start_urls", [])), None)
            )
            if (
                report.get("status") == "passed"
                and report.get("tool") == INTAKE_TOOL_VERSION
                and same_origin(spec.get("upstream_url", ""), self.url)
                and previous_capture_entry == self.url
            ):
                requested_pages = spec.get("discovery_max_pages", spec.get("max_pages"))
                if self.args.max_pages == requested_pages:
                    current_assets = int(spec.get("max_assets", 0))
                    prerender_reserve = len(spec.get("prerender_resources", []))
                    requested_assets = max(
                        current_assets,
                        self.args.max_assets,
                        min(
                            MAX_AUTO_CAPTURE_ASSETS,
                            self.args.max_assets + prerender_reserve,
                        ),
                    )
                    limits_updated = (
                        requested_assets != current_assets
                        or "discovery_max_assets" not in spec
                    )
                    if limits_updated:
                        spec["max_assets"] = requested_assets
                        spec.setdefault("discovery_max_assets", self.args.max_assets)
                        report["capture_scope"]["max_assets"] = requested_assets
                    represented_updated = spec.get("upstream_url") != self.upstream_url
                    if represented_updated:
                        spec["upstream_url"] = self.upstream_url
                    spec["capture_entry_url"] = self.url
                    report["represented_upstream_url"] = self.upstream_url
                    report["capture_entry_url"] = self.url
                    if limits_updated or represented_updated:
                        report["scope_updated_at"] = utc_now()
                    atomic_json(self.spec_path, spec)
                    atomic_json(intake_path, report)
                    self.state.stage(
                        "intake", "passed", resumed=True,
                        capture_limits_updated=limits_updated,
                        represented_url_updated=represented_updated,
                    )
                    return report, spec
        self.state.stage("intake", "running")
        report, spec = run_intake(
            self.url,
            site=self.site,
            max_pages=self.args.max_pages,
            max_assets=self.args.max_assets,
            private_research_snapshot=self.args.private_research_snapshot,
        )
        atomic_json(intake_path, report)
        if spec is None:
            status = report.get("status", "rejected")
            self.state.stage(
                "intake",
                "rejected" if status == "rejected" else "failed",
                failures=report["failures"],
            )
            raise PipelineError(
                "upstream intake rejected: " + "; ".join(report["failures"]),
                stage="intake",
                kind=report.get("failure_kind", "upstream_policy_rejection"),
                resumable=bool(report.get("resumable", False)),
            )
        spec["upstream_url"] = self.upstream_url
        spec["capture_entry_url"] = self.url
        spec.setdefault("discovery_max_assets", spec["max_assets"])
        report["represented_upstream_url"] = self.upstream_url
        report["capture_entry_url"] = self.url
        atomic_json(intake_path, report)
        atomic_json(self.spec_path, spec)
        self.state.stage(
            "intake", "passed", pages=spec["max_pages"], assets=spec["max_assets"],
            evidence=str(intake_path.relative_to(self.root)),
            spec=str(self.spec_path.relative_to(self.root)),
        )
        return report, spec

    def capture(self, spec_value: dict[str, Any]) -> dict[str, Any]:
        with capture_stage_timeout(self.args.stage_timeout):
            return self._capture(spec_value)

    def _capture(self, spec_value: dict[str, Any]) -> dict[str, Any]:
        normalized_spec = CaptureSpec.load(self.spec_path)
        normalized_resources = list(normalized_spec.prerender_resources)
        if spec_value.get("prerender_resources", []) != normalized_resources:
            spec_value = json.loads(json.dumps(spec_value))
            spec_value["prerender_resources"] = normalized_resources
            atomic_json(self.spec_path, spec_value)
        capture: dict[str, Any] | None = None
        if self.args.resume and self.capture_path.is_file():
            capture = json.loads(self.capture_path.read_text(encoding="utf-8"))
            output_dir = self.root / spec_value.get(
                "output_dir", f"sites/{self.site}/static/external_cache/upstream"
            )
            if (
                capture.get("status") == "passed"
                and capture.get("tool") == CAPTURE_TOOL_VERSION
                and capture.get("spec", {}).get("sha256") == sha256_file(self.spec_path)
                and output_dir.is_dir()
            ):
                spec_value = json.loads(self.spec_path.read_text(encoding="utf-8"))
            else:
                capture = None
        self.state.stage("capture", "running")
        if capture is None:
            capture = self.run_capture_spec(reuse_evidence=None)

        existing_queries = list(spec_value.get("generated_get_queries", []))
        discovered_queries = discover_captured_get_queries(
            self.root,
            self.site,
            capture,
            existing_queries,
            max_queries=2,
        )
        if discovered_queries:
            spec_value = extend_capture_spec_with_queries(spec_value, discovered_queries)
            atomic_json(self.spec_path, spec_value)
            capture = self.run_capture_spec(reuse_evidence=capture)

        runtime_literal_resources = discover_captured_runtime_resources(
            self.root,
            self.site,
            CaptureSpec.load(self.spec_path),
            capture,
        )
        if runtime_literal_resources:
            spec_value = extend_capture_spec_with_prerender_resources(
                spec_value, runtime_literal_resources
            )
            atomic_json(self.spec_path, spec_value)
            capture = self.run_capture_spec(reuse_evidence=capture)

        discovery_rounds = 0
        for discovery_round in range(MAX_PRERENDER_DISCOVERY_ROUNDS + 1):
            self.ensure_captured_dom_prerender(capture, spec_value)
            if discovery_round >= MAX_PRERENDER_DISCOVERY_ROUNDS:
                break
            expanded_spec = extend_capture_spec_with_same_site_prerender_scope(
                spec_value, capture
            )
            if expanded_spec != spec_value:
                spec_value = expanded_spec
                atomic_json(self.spec_path, spec_value)
            spec = CaptureSpec.load(self.spec_path)
            discovered_resources = discover_prerender_resources(spec, capture)
            if not discovered_resources:
                break
            discovery_rounds += 1
            spec_value = extend_capture_spec_with_prerender_resources(
                spec_value, discovered_resources
            )
            atomic_json(self.spec_path, spec_value)
            capture = self.run_capture_spec(reuse_evidence=capture)

        planned_total = len(spec_value.get("generated_get_queries", []))
        captured_total = sum(
            1
            for query in spec_value.get("generated_get_queries", [])
            if any(page.get("url") == query["url"] for page in capture.get("pages", []))
        )
        capture["query_discovery"] = {
            "entry_page": len(existing_queries),
            "captured_pages": len(discovered_queries),
            "planned_total": planned_total,
            "captured_total": captured_total,
            "skipped_total": planned_total - captured_total,
            "planned_searches": sum(
                record.get("kind", "search") == "search"
                for record in spec_value.get("generated_get_queries", [])
            ),
            "planned_filters": sum(
                record.get("kind") == "filter"
                for record in spec_value.get("generated_get_queries", [])
            ),
        }
        capture["spec"] = {
            "path": self.spec_path.relative_to(self.root).as_posix(),
            "sha256": sha256_file(self.spec_path),
        }
        capture["manifest_sha256"] = sha256_file(self.manifest)
        capture["captured_dom_prerender"]["resource_discovery_rounds"] = discovery_rounds
        atomic_json(self.capture_path, capture)
        self.state.stage(
            "capture", "passed", pages=len(capture["pages"]),
            assets=len(capture["assets"]),
            rendered_pages=len(capture.get("rendered_pages", [])),
            prerender_tool=PRERENDER_TOOL_VERSION,
            prerender_resources=len(spec_value.get("prerender_resources", [])),
            prerender_resource_discovery_rounds=discovery_rounds,
            runtime_literal_resources=len(runtime_literal_resources),
            query_discovery=capture["query_discovery"],
            resumed=self.args.resume and not discovered_queries,
            evidence=str(self.capture_path.relative_to(self.root)),
        )
        return capture

    def ensure_captured_dom_prerender(
        self, capture: dict[str, Any], spec_value: dict[str, Any]
    ) -> None:
        capture_root = self.root / spec_value.get(
            "output_dir", f"sites/{self.site}/static/external_cache/upstream"
        )
        if prerender_evidence_valid(capture_root, capture):
            return
        if capture.get("pages"):
            prerender = self.run_prerender_worker(capture_root, capture)
        else:
            prerender = {
                "tool": PRERENDER_TOOL_VERSION,
                "mode": "hash_verified_captured_resources_only",
                "viewport": {"width": 1440, "height": 900},
                "network_guardrails": {
                    "http": "all requests routed through captured SHA256 resource map",
                    "non_get": "blocked",
                    "service_workers": "blocked",
                    "websockets": "blocked",
                    "uncaptured": "blocked",
                },
                "attempted": 0,
                "passed": 0,
                "failed": 0,
                "attempts": [],
                "resource_attempted": 0,
                "resource_failed": 0,
                "resource_attempts": [],
                "rendered_pages": [],
            }
        capture["rendered_pages"] = prerender.pop("rendered_pages")
        capture["captured_dom_prerender"] = prerender

    def run_prerender_worker(
        self, capture_root: Path, capture: dict[str, Any]
    ) -> dict[str, Any]:
        """Run Playwright outside the capture process and promote one valid batch."""
        capture_bytes = (
            json.dumps(capture, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        capture_sha256 = hashlib.sha256(capture_bytes).hexdigest()
        batch_id = hashlib.sha256(
            f"{PRERENDER_TOOL_VERSION}\0{capture_sha256}".encode("utf-8")
        ).hexdigest()[:20]
        relative_root = Path("rendered") / batch_id
        progress_path = self.evidence_dir / "prerender-progress.json"
        worker = Path(__file__).with_name("prerender_worker.py")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix=f"prerender-{batch_id}-", dir=self.evidence_dir
        ) as temporary_name:
            temporary = Path(temporary_name)
            capture_input = temporary / "capture.json"
            result_path = temporary / "result.json"
            staging_root = temporary / "rendered-stage"
            capture_input.write_bytes(capture_bytes)
            command = [
                sys.executable,
                str(worker),
                "--capture-evidence", str(capture_input),
                "--expected-capture-sha256", capture_sha256,
                "--capture-root", str(capture_root),
                "--staging-root", str(staging_root),
                "--result", str(result_path),
                "--progress", str(progress_path),
                "--batch-id", batch_id,
            ]
            supervised_process_group(
                command,
                root=self.root,
                env=os.environ.copy(),
                timeout_seconds=self.args.stage_timeout,
            )
            if not result_path.is_file():
                raise PipelineError(
                    "prerender worker exited without a result",
                    stage="capture",
                    kind="prerender_worker_invalid_result",
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if (
                result.get("batch_id") != batch_id
                or result.get("capture_sha256") != capture_sha256
                or result.get("rendered_relative_root") != relative_root.as_posix()
                or not isinstance(result.get("prerender"), dict)
                or result["prerender"].get("tool") != PRERENDER_TOOL_VERSION
            ):
                raise PipelineError(
                    "prerender worker result does not match its pinned input",
                    stage="capture",
                    kind="prerender_worker_invalid_result",
                )

            expected_files: set[str] = set()
            for record in result.get("files", []):
                relative = Path(str(record.get("path", "")))
                if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                    raise PipelineError(
                        "prerender worker returned an unsafe output path",
                        stage="capture",
                        kind="prerender_worker_invalid_result",
                    )
                candidate = staging_root / relative
                if (
                    not candidate.is_file()
                    or candidate.stat().st_size != record.get("bytes")
                    or sha256_file(candidate) != record.get("sha256")
                ):
                    raise PipelineError(
                        f"prerender worker output hash mismatch: {relative.as_posix()}",
                        stage="capture",
                        kind="prerender_worker_invalid_result",
                    )
                expected_files.add(relative.as_posix())
            actual_files = {
                path.relative_to(staging_root).as_posix()
                for path in staging_root.rglob("*")
                if path.is_file()
            }
            if actual_files != expected_files:
                raise PipelineError(
                    "prerender worker output inventory does not match its result",
                    stage="capture",
                    kind="prerender_worker_invalid_result",
                )
            for page in result["prerender"].get("rendered_pages", []):
                local_path = Path(str(page.get("local_path", "")))
                try:
                    staged_path = local_path.relative_to(relative_root).as_posix()
                except ValueError as exc:
                    raise PipelineError(
                        "prerender page path is outside its pinned generation",
                        stage="capture",
                        kind="prerender_worker_invalid_result",
                    ) from exc
                if staged_path not in expected_files:
                    raise PipelineError(
                        "prerender page is missing from the worker inventory",
                        stage="capture",
                        kind="prerender_worker_invalid_result",
                    )

            final_root = capture_root / relative_root
            final_root.parent.mkdir(parents=True, exist_ok=True)
            backup_root = final_root.with_name(final_root.name + f".old-{os.getpid()}")
            if backup_root.exists():
                shutil.rmtree(backup_root)
            if final_root.exists():
                os.replace(final_root, backup_root)
            try:
                os.replace(staging_root, final_root)
            except BaseException:
                if backup_root.exists() and not final_root.exists():
                    os.replace(backup_root, final_root)
                raise
            if backup_root.exists():
                shutil.rmtree(backup_root)
            atomic_json(progress_path, {
                "schema_version": 1,
                "batch_id": batch_id,
                "capture_sha256": capture_sha256,
                "status": "promoted",
                "rendered_relative_root": relative_root.as_posix(),
                "file_count": len(expected_files),
                "updated_at": utc_now(),
            })
            return result["prerender"]

    def run_capture_spec(
        self,
        *,
        reuse_evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        spec = CaptureSpec.load(self.spec_path)
        output_dir = self.root / spec.output_dir
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        if reuse_evidence is None and self.capture_path.is_file() and output_dir.is_dir():
            candidate = json.loads(self.capture_path.read_text(encoding="utf-8"))
            if candidate.get("site") == self.site:
                reuse_evidence = candidate
        runner = CaptureRunner(spec, output_dir, reuse_evidence=reuse_evidence)
        try:
            capture = runner.run()
        finally:
            runner.session.close()
        capture["spec"] = {
            "path": self.spec_path.relative_to(self.root).as_posix(),
            "sha256": sha256_file(self.spec_path),
        }
        capture["manifest_sha256"] = sha256_file(self.manifest)
        atomic_json(self.capture_path, capture)
        if capture["status"] != "passed":
            raise PipelineError(
                f"capture completed with {len(capture['failures'])} failures; see {self.capture_path}",
                stage="capture",
            )
        return capture

    def run_fidelity(self, capture: dict[str, Any], port: int) -> None:
        self.state.stage("fidelity", "running")
        if not wait_port_available(port):
            raise PipelineError(f"catalog port {port} is occupied; fidelity server was not started")
        env = self.runtime_env()
        log_path = self.evidence_dir / "fidelity-server.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    self.args.python,
                    str(self.root / "src/webanything/runtime.py"),
                    "serve",
                    "--site", self.site,
                    "--port", str(port),
                    "--host", "127.0.0.1",
                    "--sites-root", str(self.root / "sites"),
                ],
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                wait_http(port, process)
                spec_path = fidelity_spec(
                    self.root,
                    site=self.site,
                    port=port,
                    capture=capture,
                    upstream_user_agent=json.loads(
                        self.spec_path.read_text(encoding="utf-8")
                    ).get("user_agent"),
                )
                output = subprocess_stage([
                    str(self.root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
                    "verify-fidelity", str(spec_path),
                ], root=self.root, env=self.runtime_env(), timeout_seconds=self.args.stage_timeout)
                self.state.stage("fidelity", "passed", output=output[-5000:])
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

    def run_quality(self, port: int) -> dict[str, Any]:
        self.state.stage("quality", "running")
        if not wait_port_available(port):
            raise PipelineError(f"catalog port {port} is occupied; quality server was not started")
        env = self.runtime_env()
        log_path = self.evidence_dir / "quality-server.log"
        quality_path = self.evidence_dir / "quality.json"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    self.args.python,
                    str(self.root / "src/webanything/runtime.py"),
                    "serve",
                    "--site", self.site,
                    "--port", str(port),
                    "--host", "127.0.0.1",
                    "--sites-root", str(self.root / "sites"),
                ],
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                wait_http(port, process)
                output = subprocess_stage([
                    str(self.root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
                    "verify-quality",
                    "--site", self.site,
                    "--base-url", f"http://127.0.0.1:{port}/",
                ], root=self.root, env=self.runtime_env(), timeout_seconds=self.args.stage_timeout)
                quality = json.loads(quality_path.read_text(encoding="utf-8"))
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                reset = restore_runtime_database(self.root, self.site)
        quality["post_workflow_reset"] = reset
        atomic_json(quality_path, quality)
        self.state.stage(
            "quality",
            "passed",
            grade=quality["grade"],
            evidence=str(quality_path.relative_to(self.root)),
            reset=reset,
            output=output[-5000:],
        )
        return quality

    def partial(self) -> dict[str, Any]:
        result = {
            "site": self.site,
            "status": "stopped",
            "after_stage": self.args.stop_after,
            "automation_evidence": str(self.state.path.relative_to(self.root)),
            "result_evidence": str(self.result_path.relative_to(self.root)),
        }
        atomic_json(self.result_path, result)
        return result

    def failure_result(
        self,
        *,
        status: str,
        stage: str,
        kind: str,
        resumable: bool,
        error: str,
    ) -> dict[str, Any]:
        return {
            "site": self.site,
            "status": status,
            "failed_stage": stage,
            "failure_kind": kind,
            "resumable": resumable,
            "error": error,
            "automation_evidence": str(self.state.path.relative_to(self.root)),
            "result_evidence": str(self.result_path.relative_to(self.root)),
        }


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__)
    top.add_argument("url", help="public upstream URL; scheme may be omitted")
    top.add_argument(
        "--represented-url",
        help=(
            "canonical same-origin site URL represented by a public capture entry; "
            "used when the homepage is unavailable but a robots-allowed content route works"
        ),
    )
    top.add_argument("--site", help="optional stable site id; normally inferred from the URL")
    top.add_argument(
        "--display-name",
        help="explicit identity label; replacement queues use the frozen catalog name",
    )
    top.add_argument("--max-pages", type=int, default=12)
    top.add_argument("--max-assets", type=int, default=160)
    top.add_argument(
        "--private-research-snapshot",
        action="store_true",
        help="record an explicit user-directed local research basis; never bypasses robots or WAF",
    )
    top.add_argument(
        "--python",
        default=os.environ.get("WEBHARBOR_RUNTIME_PYTHON", "python3"),
        help="Python used by generated Flask apps; defaults to WEBHARBOR_RUNTIME_PYTHON or python3",
    )
    top.add_argument(
        "--dependency-path",
        default=os.environ.get("WEBHARBOR_RUNTIME_DEPENDENCY_PATH"),
        help="optional PYTHONPATH entry for runtime dependencies",
    )
    top.add_argument("--resume", action="store_true")
    top.add_argument(
        "--stage-timeout",
        type=float,
        default=float(
            os.environ.get("WEBHARBOR_STAGE_TIMEOUT_SECONDS", DEFAULT_STAGE_TIMEOUT_SECONDS)
        ),
        help="maximum seconds for each capture or subprocess-backed pipeline stage",
    )
    top.add_argument(
        "--replace-existing",
        action="store_true",
        help="allow replacing an existing hand-built app after capture; never implied by --resume",
    )
    top.add_argument("--stop-after", choices=STAGES[:-1])
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.max_pages < 1 or args.max_assets < 0:
        raise SystemExit("capture limits must be non-negative and max-pages must be positive")
    if args.stage_timeout <= 0:
        raise SystemExit("--stage-timeout must be positive")
    pipeline: BuildPipeline | None = None
    try:
        pipeline = BuildPipeline(args)
        result = pipeline.run()
    except PipelineError as exc:
        print(f"build-from-upstream: {exc}", file=sys.stderr)
        if pipeline is not None and pipeline.result_path.is_file():
            result = json.loads(pipeline.result_path.read_text(encoding="utf-8"))
        else:
            result = {
                "site": getattr(pipeline, "site", None),
                "status": "failed",
                "failed_stage": exc.stage or "input",
                "failure_kind": exc.kind,
                "resumable": exc.resumable,
                "error": str(exc),
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        return result_exit_code(result)
    except IntakeError as exc:
        result = {
            "site": None,
            "status": "rejected",
            "failed_stage": "input",
            "failure_kind": "upstream_policy_rejection",
            "resumable": False,
            "error": str(exc),
        }
        print(f"build-from-upstream: {exc}", file=sys.stderr)
        print(json.dumps(result, indent=2, sort_keys=True))
        return result_exit_code(result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
