#!/usr/bin/env python3
"""Build a historical benchmark mirror through every local verification stage."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from build_from_upstream import (
    PipelineError,
    atomic_json,
    package_managed_assets,
    port_available,
    restore_runtime_database,
    subprocess_stage,
    wait_http,
)
from capture_historical import (
    TOOL_VERSION as CAPTURE_TOOL_VERSION,
    DEFAULT_CONFIG,
    DEFAULT_DATASET,
    DEFAULT_REVISION,
    DEFAULT_WEBLINX_DATASET,
    DEFAULT_WEBLINX_LICENSE,
    DEFAULT_WEBLINX_REVISION,
    DEFAULT_WEBUI_DOM_DATASET,
    DEFAULT_WEBUI_DOM_LICENSE,
    DEFAULT_WEBUI_DOM_REVISION,
)
from compile_historical import HistoricalCompiler
from compile_snapshot import catalog_port, repository_root, sha256_file, utc_now


TOOL_VERSION = "webharbor-build-from-historical-v6"
STAGES = (
    "environment", "historical_source", "reconstruction", "seed", "catalog",
    "audit", "runtime_local", "fidelity", "quality", "package",
)


class HistoricalPipelineError(RuntimeError):
    pass


class PipelineState:
    def __init__(self, path: Path, site: str, upstream_url: str) -> None:
        self.path = path
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
        else:
            existing = None
        if existing and existing.get("tool") != TOOL_VERSION:
            archive = path.with_name("automation-live.json")
            if not archive.exists():
                shutil.copy2(path, archive)
            existing = None
        self.value = existing or {
            "schema_version": 1,
            "tool": TOOL_VERSION,
            "phase": "automation",
            "site": site,
            "upstream_url": upstream_url,
            "created_at": utc_now(),
            "status": "running",
            "source_mode": "historical_benchmark_snapshot",
            "stages": {},
        }
        if self.value.get("site") != site or self.value.get("upstream_url") != upstream_url:
            raise HistoricalPipelineError("automation evidence belongs to another target")
        self.value["tool"] = TOOL_VERSION

    def stage(self, name: str, status: str, **details: Any) -> None:
        self.value["status"] = "running" if status in {"running", "passed"} else status
        self.value["updated_at"] = utc_now()
        self.value["stages"][name] = {"status": status, "at": utc_now(), **details}
        for key in (
            "completed_at", "failed_stage", "error", "resumable",
            "result_tier", "quality_grade", "review_gate",
        ):
            self.value.pop(key, None)
        atomic_json(self.path, self.value)

    def fail(self, stage: str, error: str) -> None:
        self.value.update({
            "status": "failed",
            "updated_at": utc_now(),
            "failed_stage": stage,
            "error": error,
            "resumable": True,
        })
        previous = self.value["stages"].get(stage, {})
        self.value["stages"][stage] = {
            **previous,
            "status": "failed",
            "at": utc_now(),
            "error": error,
            "resumable": True,
        }
        atomic_json(self.path, self.value)

    def complete(self, *, quality_grade: str) -> None:
        for key in ("failed_stage", "error", "resumable"):
            self.value.pop(key, None)
        self.value.update({
            "status": "passed",
            "completed_at": utc_now(),
            "result_tier": "candidate_reconstructed",
            "quality_grade": quality_grade,
            "review_gate": (
                "Reviewer-owned deterministic verifiers and rubrics remain required."
            ),
        })
        atomic_json(self.path, self.value)


def read_targets(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_targets(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def historical_descriptor(historical: dict[str, Any], *, site: str) -> dict[str, Any]:
    source = historical["source"]
    provider = source.get("provider", "mind2web")
    descriptor: dict[str, Any] = {
        "evidence": f"mirror_factory/evidence/{site}/historical-source.json",
        "tool": historical["tool"],
        "provider": provider,
        "dataset": source["dataset"],
        "revision": source["revision"],
        "publication": "not_authorized",
    }
    if provider == "mind2web":
        descriptor.update({
            "config": source["config"],
            "split": source["split"],
            "website": source["website"],
            "row_index": source["row_index"],
        })
    elif provider == "weblinx":
        files = source["files"]
        descriptor.update({
            "demonstration": source["demonstration"],
            "license": source["license"],
            "files": {
                name: {"path": files[name]["path"], "sha256": files[name]["sha256"]}
                for name in ("html", "bbox", "screenshot")
            },
        })
    elif provider == "webui_dom_snapshot":
        descriptor.update({
            "config": source["config"],
            "split": source["split"],
            "row_index": source["row_index"],
            "capture_url": source["capture_url"],
            "license": source["license"],
            "dom_snapshot_sha256": source["dom_snapshot"]["sha256"],
            "row_metadata_sha256": source["stable_row_metadata"]["sha256"],
            "screenshot_sha256": source["screenshot"]["sha256"],
        })
    else:
        raise HistoricalPipelineError(f"unsupported historical provider in evidence: {provider}")
    return descriptor


def register_manifest(
    root: Path,
    *,
    site: str,
    display_name: str,
    upstream_url: str,
    task_count: int,
    historical: dict[str, Any],
) -> None:
    path = root / "mirror_factory" / "targets.jsonl"
    rows = read_targets(path)
    record = next((row for row in rows if row.get("id") == site), None)
    if record is None:
        record = {
            "id": site,
            "source": {"kind": "greenfield"},
            "asset": {"kind": "generated"},
        }
        rows.append(record)
    record.update({
        "display_name": display_name,
        "upstream_url": upstream_url,
        "purpose": (
            f"Local mirror reconstructed from a pinned historical benchmark snapshot of {display_name}."
        ),
        "initial_stage": "discovered",
        "task_count": task_count,
        "historical_source": historical_descriptor(historical, site=site),
    })
    write_targets(path, rows)


def register_catalog(
    root: Path,
    *,
    site: str,
    display_name: str,
    task_count: int,
    seed_path: Path,
    historical: dict[str, Any],
) -> int:
    path = root / "site_catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    records = catalog["sites"]
    index = next((i for i, row in enumerate(records) if row["id"] == site), None)
    record = records[index] if index is not None else {"id": site}
    provenance = {
        "kind": "historical_benchmark_snapshot_reconstruction",
        "tool": TOOL_VERSION,
        "historical_source": historical_descriptor(historical, site=site),
        "derived_source": f"mirror_factory/evidence/{site}/historical-derived.json",
        "reconstruction": f"mirror_factory/evidence/{site}/reconstruction.json",
    }
    live_evidence = root / "mirror_factory" / "evidence" / site / "upstream.json"
    if live_evidence.is_file():
        provenance["live_access_evidence"] = {
            "evidence": str(live_evidence.relative_to(root)),
            "used_as_reconstruction_source": False,
        }
    record.update({
        "display_name": display_name,
        "tier": "candidate_reconstructed",
        "code": {"path": f"sites/{site}", "provenance": provenance},
        "assets": {
            "kind": "generated",
            "generator": "app_import",
            "source_mode": "historical_benchmark_snapshot",
            "sha256": sha256_file(seed_path),
            "bytes": seed_path.stat().st_size,
            "publication": "not_authorized",
        },
        "tasks": {
            "count": task_count,
            "deterministic_verifiers": False,
            "review_status": "candidate",
        },
    })
    if index is None:
        records.append(record)
        index = len(records) - 1
    atomic_json(path, catalog)
    return int(catalog["base_port"]) + index


def source_matches(root: Path, path: Path, args: argparse.Namespace) -> bool:
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        source = value["source"]
        scope = value["scope"]
    except (KeyError, json.JSONDecodeError, TypeError):
        return False
    provider = getattr(args, "provider", "mind2web")
    expected = {"dataset": args.dataset, "revision": args.revision}
    if provider in {"mind2web", "webui_dom_snapshot"}:
        expected.update({
            "config": args.config,
            "split": args.split,
            "row_index": args.row_index,
        })
        if provider == "mind2web":
            expected["website"] = args.website
        else:
            expected["capture_url"] = args.capture_url
    elif provider == "weblinx":
        expected["demonstration"] = args.demonstration
    if (
        value.get("site") != args.site
        or value.get("tool") != CAPTURE_TOOL_VERSION
        or value.get("status") != "passed"
        or value.get("source_mode") != "historical_benchmark_snapshot"
        or scope.get("upstream_url") != args.upstream_url
        or source.get("provider", "mind2web") != provider
        or any(source.get(key) != expected_value for key, expected_value in expected.items())
    ):
        return False
    if provider == "weblinx":
        files = source.get("files")
        expected_files = {
            "html": (f"demonstrations/{args.demonstration}/pages/{args.page}", args.html_sha256),
            "bbox": (f"demonstrations/{args.demonstration}/bboxes/{args.bbox}", args.bbox_sha256),
            "screenshot": (
                f"demonstrations/{args.demonstration}/screenshots/{args.screenshot}",
                args.screenshot_sha256,
            ),
        }
        if not isinstance(files, dict) or any(
            not isinstance(files.get(name), dict)
            or files[name].get("path") != expected_path
            or files[name].get("sha256") != expected_hash
            for name, (expected_path, expected_hash) in expected_files.items()
        ):
            return False
    elif provider == "webui_dom_snapshot":
        if (
            source.get("dom_snapshot", {}).get("sha256") != args.dom_sha256
            or source.get("stable_row_metadata", {}).get("sha256")
            != args.row_metadata_sha256
            or source.get("screenshot", {}).get("sha256") != args.screenshot_sha256
        ):
            return False
    try:
        compiler = HistoricalCompiler(root, args.site, path)
        compiler.verified_page()
        compiler.verified_screenshot().close()
    except Exception:
        return False
    return True


def historical_capture_command(root: Path, args: argparse.Namespace) -> list[str]:
    command = [
        str(root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
        "capture-historical",
        "--provider", args.provider,
        "--site", args.site,
        "--upstream-url", args.upstream_url,
        "--dataset", args.dataset,
        "--revision", args.revision,
        "--timeout", str(args.source_timeout),
    ]
    if args.provider == "mind2web":
        command.extend([
            "--website", args.website,
            "--split", args.split,
            "--row-index", str(args.row_index),
            "--config", args.config,
        ])
    elif args.provider == "weblinx":
        command.extend([
            "--demonstration", args.demonstration,
            "--page", args.page,
            "--bbox", args.bbox,
            "--screenshot", args.screenshot,
            "--html-sha256", args.html_sha256,
            "--bbox-sha256", args.bbox_sha256,
            "--screenshot-sha256", args.screenshot_sha256,
            "--license", args.license,
        ])
    else:
        command.extend([
            "--capture-url", args.capture_url,
            "--config", args.config,
            "--split", args.split,
            "--row-index", str(args.row_index),
            "--dom-sha256", args.dom_sha256,
            "--row-metadata-sha256", args.row_metadata_sha256,
            "--screenshot-sha256", args.screenshot_sha256,
            "--minimum-source-image-ssim", str(args.minimum_source_image_ssim),
            "--license", args.license,
        ])
    return command


def fidelity_spec(
    root: Path,
    *,
    site: str,
    port: int,
    historical: dict[str, Any],
    minimum_similarity: float,
) -> Path:
    width = int(historical["screenshot"]["width"])
    height = min(900, int(historical["screenshot"]["height"]))
    spec = {
        "site": site,
        "historical_source_evidence": f"mirror_factory/evidence/{site}/historical-source.json",
        "minimum_pixel_similarity": minimum_similarity,
        "minimum_nonblank_retention": 0.5,
        "require_complete_resources": True,
        "runtime_javascript_check": True,
        "output_dir": f"sites/{site}/static/external_cache/fidelity",
        "viewports": [{"name": "desktop", "width": width, "height": height}],
        "routes": [{
            "name": "home",
            "upstream_url": historical["scope"]["upstream_url"],
            "local_url": f"http://127.0.0.1:{port}/",
            "wait_for": "body",
            "required_landmarks": "match_upstream",
            "reference_crop": {"x": 0, "y": 0, "width": width, "height": height},
        }],
    }
    path = root / "mirror_factory" / "fidelity_specs" / f"{site}.json"
    atomic_json(path, spec)
    return path


class HistoricalPipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = repository_root()
        self.evidence_dir = self.root / "mirror_factory" / "evidence" / args.site
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.source_path = self.evidence_dir / "historical-source.json"
        self.result_path = self.evidence_dir / "result.json"
        self.state = PipelineState(
            self.evidence_dir / "automation.json", args.site, args.upstream_url
        )

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.args.dependency_path:
            dependency = str(Path(self.args.dependency_path).resolve())
            env["PYTHONPATH"] = dependency + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
        env.setdefault("PYTHONPYCACHEPREFIX", "/tmp/webharbor-pyc")
        return env

    def command(self, values: list[str]) -> str:
        return subprocess_stage(
            values,
            root=self.root,
            env=self.environment(),
            timeout_seconds=self.args.stage_timeout,
        )

    def run_fidelity(self, port: int, historical: dict[str, Any]) -> str:
        if not port_available(port):
            raise HistoricalPipelineError(f"catalog port {port} is occupied")
        env = self.environment()
        spec_path = fidelity_spec(
            self.root,
            site=self.args.site,
            port=port,
            historical=historical,
            minimum_similarity=self.args.minimum_pixel_similarity,
        )
        log_path = self.evidence_dir / "fidelity-server.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    self.args.python,
                    str(self.root / "src/webanything/runtime.py"),
                    "serve",
                    "--site", self.args.site,
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
                return self.command([
                    str(self.root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
                    "verify-fidelity", str(spec_path),
                ])
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

    def run_quality(self, port: int) -> tuple[str, dict[str, Any], dict[str, str]]:
        if not port_available(port):
            raise HistoricalPipelineError(f"catalog port {port} is occupied")
        env = self.environment()
        log_path = self.evidence_dir / "quality-server.log"
        quality_path = self.evidence_dir / "quality.json"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    self.args.python,
                    str(self.root / "src/webanything/runtime.py"),
                    "serve",
                    "--site", self.args.site,
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
                output = self.command([
                    str(self.root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
                    "verify-quality",
                    "--site", self.args.site,
                    "--base-url", f"http://127.0.0.1:{port}/",
                ])
                quality = json.loads(quality_path.read_text(encoding="utf-8"))
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                reset = restore_runtime_database(self.root, self.args.site)
        quality["post_workflow_reset"] = reset
        atomic_json(quality_path, quality)
        return output, quality, reset

    def run(self) -> dict[str, Any]:
        current_stage = "environment"
        try:
            self.state.stage(current_stage, "running")
            self.command([self.args.python, "-c", "import flask, flask_sqlalchemy, PIL"])
            self.command([self.args.python, "-c", "import playwright.sync_api"])
            self.state.stage(current_stage, "passed")

            current_stage = "historical_source"
            reused = self.args.resume and source_matches(self.root, self.source_path, self.args)
            self.state.stage(current_stage, "running", reused=reused)
            if not reused:
                self.command(historical_capture_command(self.root, self.args))
            historical = json.loads(self.source_path.read_text(encoding="utf-8"))
            source = historical["source"]
            source_details = {
                "provider": source.get("provider", "mind2web"),
                "dataset": source["dataset"],
                "revision": source["revision"],
            }
            if source_details["provider"] in {"mind2web", "webui_dom_snapshot"}:
                source_details.update({
                    "split": source["split"],
                    "row_index": source["row_index"],
                })
            else:
                source_details["demonstration"] = source["demonstration"]
            self.state.stage(
                current_stage,
                "passed",
                reused=reused,
                evidence=str(self.source_path.relative_to(self.root)),
                evidence_sha256=sha256_file(self.source_path),
                **source_details,
            )

            current_stage = "reconstruction"
            self.state.stage(current_stage, "running")
            port = catalog_port(self.root, self.args.site)
            reconstruction = HistoricalCompiler(
                self.root, self.args.site, self.source_path
            ).compile(
                display_name=self.args.display_name,
                port=port,
                replace=self.args.replace_existing,
            )
            reconstruction_path = self.evidence_dir / "reconstruction.json"
            atomic_json(reconstruction_path, reconstruction)
            self.state.stage(
                current_stage,
                "passed",
                pages=reconstruction["pages"],
                assets=reconstruction["assets"],
                tasks=reconstruction["tasks"],
                mirror_contract=reconstruction["mirror_contract"],
                evidence=str(reconstruction_path.relative_to(self.root)),
            )

            current_stage = "seed"
            self.state.stage(current_stage, "running")
            output = self.command([
                str(self.root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
                "seed", "--python", self.args.python, self.args.site,
            ])
            seed_path = self.root / "sites" / self.args.site / "instance_seed" / f"{self.args.site}.db"
            self.state.stage(
                current_stage, "passed", output=output[-4000:],
                bytes=seed_path.stat().st_size, sha256=sha256_file(seed_path),
            )

            current_stage = "catalog"
            self.state.stage(current_stage, "running")
            display_name = reconstruction["display_name"]
            register_manifest(
                self.root,
                site=self.args.site,
                display_name=display_name,
                upstream_url=self.args.upstream_url,
                task_count=reconstruction["tasks"],
                historical=historical,
            )
            port = register_catalog(
                self.root,
                site=self.args.site,
                display_name=display_name,
                task_count=reconstruction["tasks"],
                seed_path=seed_path,
                historical=historical,
            )
            self.state.stage(current_stage, "passed", port=port, tier="candidate_reconstructed")

            current_stage = "audit"
            self.state.stage(current_stage, "running")
            output = self.command([
                str(self.root / ".agents/skills/build-webharbor-mirrors/scripts/run.sh"),
                "audit", "--write-evidence", self.args.site,
            ])
            self.state.stage(current_stage, "passed", output=output[-4000:])

            current_stage = "runtime_local"
            self.state.stage(current_stage, "running")
            command = [
                self.args.python,
                str(self.root / "scripts/verify_local_runtime_batches.py"),
                "--batch-size", "1", "--take-over-stale", self.args.site,
            ]
            if self.args.dependency_path:
                command[2:2] = ["--dependency-path", str(Path(self.args.dependency_path).resolve())]
            output = self.command(command)
            self.state.stage(current_stage, "passed", output=output[-6000:])

            current_stage = "fidelity"
            self.state.stage(current_stage, "running")
            output = self.run_fidelity(port, historical)
            fidelity_path = self.evidence_dir / "fidelity.json"
            fidelity = json.loads(fidelity_path.read_text(encoding="utf-8"))
            similarity = min(result["pixel_similarity"] for result in fidelity["results"])
            self.state.stage(
                current_stage, "passed", output=output[-5000:],
                minimum_pixel_similarity=self.args.minimum_pixel_similarity,
                observed_pixel_similarity=similarity,
                evidence=str(fidelity_path.relative_to(self.root)),
            )

            current_stage = "quality"
            self.state.stage(current_stage, "running")
            output, quality, quality_reset = self.run_quality(port)
            quality_path = self.evidence_dir / "quality.json"
            self.state.stage(
                current_stage,
                "passed",
                grade=quality["grade"],
                output=output[-5000:],
                evidence=str(quality_path.relative_to(self.root)),
                reset=quality_reset,
            )

            current_stage = "package"
            self.state.stage(current_stage, "running")
            package = package_managed_assets(self.root, self.args.site)
            package.update({
                "tool": TOOL_VERSION,
                "source_mode": "historical_benchmark_snapshot",
                "historical_source_evidence_sha256": sha256_file(self.source_path),
                "publication": {
                    "status": "not_authorized",
                    "reason": "The pinned benchmark source does not grant publication rights.",
                },
            })
            package_path = self.evidence_dir / "asset-package.json"
            atomic_json(package_path, package)
            self.state.stage(
                current_stage, "passed", artifact=package["artifact"],
                evidence=str(package_path.relative_to(self.root)),
            )
            self.state.complete(quality_grade=quality["grade"])
            result = {
                "site": self.args.site,
                "status": "passed",
                "tier": "candidate_reconstructed",
                "source_mode": "historical_benchmark_snapshot",
                "publication": "not_authorized",
                "port": port,
                "pages": reconstruction["pages"],
                "assets": reconstruction["assets"],
                "tasks": reconstruction["tasks"],
                "quality_grade": quality["grade"],
                "quality_evidence": f"mirror_factory/evidence/{self.args.site}/quality.json",
                "mirror_contract": reconstruction["mirror_contract"]["path"],
                "pixel_similarity": similarity,
                "review_status": "candidate",
                "review_required": True,
                "workflow_gaps": f"sites/{self.args.site}/workflow_gaps.json",
                "automation_evidence": str(self.state.path.relative_to(self.root)),
                "result_evidence": str(self.result_path.relative_to(self.root)),
            }
            atomic_json(self.result_path, result)
            return result
        except Exception as exc:
            self.state.fail(current_stage, str(exc))
            result = {
                "site": self.args.site,
                "status": "failed",
                "failed_stage": current_stage,
                "resumable": True,
                "error": str(exc),
                "automation_evidence": str(self.state.path.relative_to(self.root)),
                "result_evidence": str(self.result_path.relative_to(self.root)),
            }
            atomic_json(self.result_path, result)
            return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--provider",
        choices=("mind2web", "weblinx", "webui_dom_snapshot"),
        default="mind2web",
    )
    value.add_argument("--site", required=True)
    value.add_argument("--upstream-url", required=True)
    value.add_argument("--website")
    value.add_argument("--split", choices=("train", "test_domain", "test_task", "test_website"))
    value.add_argument("--row-index", type=int)
    value.add_argument("--display-name")
    value.add_argument("--dataset")
    value.add_argument("--revision")
    value.add_argument("--config", default=DEFAULT_CONFIG)
    value.add_argument("--demonstration")
    value.add_argument("--page")
    value.add_argument("--bbox")
    value.add_argument("--screenshot")
    value.add_argument("--html-sha256")
    value.add_argument("--bbox-sha256")
    value.add_argument("--screenshot-sha256")
    value.add_argument("--capture-url")
    value.add_argument("--dom-sha256")
    value.add_argument("--row-metadata-sha256")
    value.add_argument("--minimum-source-image-ssim", type=float, default=0.7)
    value.add_argument("--license")
    value.add_argument("--python", default=os.environ.get("WEBHARBOR_RUNTIME_PYTHON", "python3"))
    value.add_argument("--dependency-path", default=os.environ.get("WEBHARBOR_RUNTIME_DEPENDENCY_PATH"))
    value.add_argument("--replace-existing", action="store_true")
    value.add_argument("--resume", action="store_true")
    value.add_argument("--source-timeout", type=float, default=180)
    value.add_argument("--stage-timeout", type=float, default=600)
    value.add_argument("--minimum-pixel-similarity", type=float, default=0.85)
    return value


def main(argv: list[str] | None = None) -> int:
    value = parser()
    args = value.parse_args(argv)
    if args.provider == "mind2web":
        args.dataset = args.dataset or DEFAULT_DATASET
        args.revision = args.revision or DEFAULT_REVISION
        if not args.website or not args.split or args.row_index is None:
            value.error("Mind2Web build requires --website, --split, and --row-index")
    elif args.provider == "weblinx":
        args.dataset = args.dataset or DEFAULT_WEBLINX_DATASET
        args.revision = args.revision or DEFAULT_WEBLINX_REVISION
        args.license = args.license or DEFAULT_WEBLINX_LICENSE
        required = {
            "demonstration": args.demonstration,
            "page": args.page,
            "bbox": args.bbox,
            "screenshot": args.screenshot,
            "html-sha256": args.html_sha256,
            "bbox-sha256": args.bbox_sha256,
            "screenshot-sha256": args.screenshot_sha256,
        }
        missing = [name for name, item in required.items() if not item]
        if missing:
            value.error("WebLINX build requires " + ", ".join(f"--{name}" for name in missing))
    else:
        args.dataset = args.dataset or DEFAULT_WEBUI_DOM_DATASET
        args.revision = args.revision or DEFAULT_WEBUI_DOM_REVISION
        args.license = args.license or DEFAULT_WEBUI_DOM_LICENSE
        args.split = args.split or "train"
        required = {
            "capture-url": args.capture_url,
            "row-index": args.row_index,
            "dom-sha256": args.dom_sha256,
            "row-metadata-sha256": args.row_metadata_sha256,
            "screenshot-sha256": args.screenshot_sha256,
        }
        missing = [name for name, item in required.items() if item is None]
        if missing:
            value.error(
                "WebUI DOM build requires "
                + ", ".join(f"--{name}" for name in missing)
            )
        if not 0 <= args.minimum_source_image_ssim <= 1:
            value.error("--minimum-source-image-ssim must be between zero and one")
    if not 0 <= args.minimum_pixel_similarity <= 1:
        raise SystemExit("--minimum-pixel-similarity must be between 0 and 1")
    result = HistoricalPipeline(args).run()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
