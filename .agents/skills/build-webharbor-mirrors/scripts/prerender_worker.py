#!/usr/bin/env python3
"""Run captured-resource prerendering in an independently killable process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from prerender_snapshot import prerender_captured_pages


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--capture-evidence", type=Path, required=True)
    value.add_argument("--expected-capture-sha256", required=True)
    value.add_argument("--capture-root", type=Path, required=True)
    value.add_argument("--staging-root", type=Path, required=True)
    value.add_argument("--result", type=Path, required=True)
    value.add_argument("--progress", type=Path, required=True)
    value.add_argument("--batch-id", required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    capture_bytes = args.capture_evidence.read_bytes()
    capture_sha256 = sha256_bytes(capture_bytes)
    if capture_sha256 != args.expected_capture_sha256:
        raise RuntimeError("capture input hash changed before prerender worker startup")
    capture = json.loads(capture_bytes)
    args.staging_root.mkdir(parents=True, exist_ok=False)
    relative_root = Path("rendered") / args.batch_id
    history: list[dict[str, Any]] = []

    def record(event: dict[str, Any]) -> None:
        history.append({**event, "updated_at": utc_now()})
        atomic_json(args.progress, {
            "schema_version": 1,
            "run_id": f"prerender-{os.getpid()}",
            "batch_id": args.batch_id,
            "capture_sha256": capture_sha256,
            "status": "running",
            "current": history[-1],
            "completed": history[:-1],
            "updated_at": utc_now(),
        })

    record({"phase": "starting", "event": "started"})
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE 127.0.0.1",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ],
        )
        try:
            prerender = prerender_captured_pages(
                browser,
                capture_root=args.capture_root,
                capture=capture,
                rendered_output_root=args.staging_root,
                rendered_relative_root=relative_root,
                progress=record,
            )
        finally:
            browser.close()

    files = []
    for path in sorted(candidate for candidate in args.staging_root.rglob("*") if candidate.is_file()):
        files.append({
            "path": path.relative_to(args.staging_root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_bytes(path.read_bytes()),
        })
    result = {
        "schema_version": 1,
        "batch_id": args.batch_id,
        "capture_sha256": capture_sha256,
        "rendered_relative_root": relative_root.as_posix(),
        "files": files,
        "prerender": prerender,
    }
    atomic_json(args.result, result)
    atomic_json(args.progress, {
        "schema_version": 1,
        "run_id": f"prerender-{os.getpid()}",
        "batch_id": args.batch_id,
        "capture_sha256": capture_sha256,
        "status": "completed",
        "current": {"phase": "complete", "event": "completed"},
        "completed": history,
        "updated_at": utc_now(),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
