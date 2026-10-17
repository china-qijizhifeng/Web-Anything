"""Independent structural visual verifier for synthetic benchmark candidates."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .release import atomic_json, load_candidate_freeze, sha256_file, tree_identity
from .synthetic import contract_content_mode, load_synthetic_contract
from .synthetic_replay import load_synthetic_replay_contract
from .synthetic_visual import (
    GEOMETRY_SCRIPT,
    SyntheticVisualError,
    compare_geometry,
    geometry_density_deltas,
    geometry_mismatch_regions,
    load_visual_reference,
    masked_image_similarity,
    sanitize_screenshot,
    validate_geometry,
)


VISUAL_VERIFICATION_SCHEMA = "webanything-synthetic-visual-verification-v1"
SOURCE_FIDELITY_MINIMUMS = {"masked_region": 0.94, "composite": 0.85}
SOURCE_FIDELITY_WEIGHTS = {
    "masked_region": 0.80,
    "geometry": 0.08,
    "density": 0.04,
    "columns": 0.04,
    "spacing": 0.04,
}
SOURCE_FIDELITY_REQUIRED_METRICS = frozenset(SOURCE_FIDELITY_MINIMUMS)
CONTENT_NEUTRAL_WEIGHTS = {
    "masked_region": 0.55,
    "columns": 0.20,
    "spacing": 0.15,
    "geometry": 0.05,
    "density": 0.05,
}
CONTENT_NEUTRAL_REQUIRED_METRICS = frozenset({
    "masked_region",
    "columns",
    "spacing",
    "composite",
})
LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SyntheticVisualVerificationError(RuntimeError):
    """Synthetic visual verification cannot be completed safely."""


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _wait_ready(url: str, process: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SyntheticVisualVerificationError(f"static server exited with {process.returncode}")
        try:
            with LOOPBACK_OPENER.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last = exc
        time.sleep(0.1)
    raise SyntheticVisualVerificationError(f"static server did not become ready: {last}")


def _route_url(base_url: str, route: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", route.lstrip("/"))


def _hints(
    scores: dict[str, float],
    thresholds: dict[str, float],
    *,
    comparison_mode: str,
) -> list[str]:
    hints = []
    if scores["geometry"] < thresholds["geometry"]:
        hints.append("Align major component bounds and landmark placement with the sanitized geometry reference.")
    if scores["density"] < thresholds["density"]:
        if comparison_mode == "source_visible":
            hints.append("Match the visible card, control, text-block, and media density from the frozen source screenshot.")
        else:
            hints.append("Adjust visible card, control, text-block, and media-placeholder density without copying source content.")
    if scores["columns"] < thresholds["columns"]:
        hints.append("Correct column widths, sidebar/main proportions, and horizontal occupancy.")
    if scores["spacing"] < thresholds["spacing"]:
        hints.append("Correct vertical rhythm, section gaps, and component padding.")
    if scores["masked_region"] < thresholds["masked_region"]:
        if comparison_mode == "source_visible":
            hints.append("Reproduce the frozen screenshot's visible brand, text, media, chrome, colors, and large-region placement more closely.")
        else:
            hints.append("Adjust page chrome, surfaces, borders, and large region placement; keep all text, identity, and media synthetic.")
    return hints


def _repair_package(
    *, site: str,
    candidate: dict[str, Any],
    route_results: list[dict[str, Any]],
    reference_sha256: str,
) -> dict[str, Any]:
    failing = [item for item in route_results if item["status"] == "failed"]
    groups = []
    for item in failing:
        failed_metrics = sorted(name for name, value in item["metric_status"].items() if value == "failed")
        groups.append({
            "route_family": item["synthetic_route"],
            "page_archetype": "synthetic_visual_surface",
            "control_type": "layout",
            "failure_codes": [f"synthetic_visual_{name}_below_threshold" for name in failed_metrics],
            "items": [{
                "route": item["synthetic_route"],
                "viewport": item["viewport"],
                "scores": item["scores"],
                "thresholds": item["thresholds"],
                "hints": item["repair_hints"],
                "mismatch_regions": item["mismatch_regions"],
                "density_deltas": item["density_deltas"],
                "visual_reference_sha256": reference_sha256,
                "source_content_available": item["comparison_mode"] == "source_visible",
            }],
        })
    return {
        "schema_version": 1,
        "repair_package": "webharbor-builder-repair-package-v1",
        "site": site,
        "status": "repair_required" if groups else "empty",
        "candidate_fingerprint": {
            "algorithm": candidate["algorithm"],
            "sha256": candidate["sha256"],
        },
        "failure_count": len(groups),
        "group_count": len(groups),
        "groups": groups,
    }


def verify_synthetic_visual(
    *,
    site_path: Path,
    contract_path: Path,
    reference_path: Path,
    freeze_path: Path,
    output_path: Path,
    repair_package_path: Path | None = None,
    base_url: str | None = None,
    browser_path: Path | None = None,
    dependency_path: Path | None = None,
    timeout_ms: int = 10_000,
) -> dict[str, Any]:
    site_path = site_path.resolve()
    contract_path = contract_path.resolve()
    reference_path = reference_path.resolve()
    freeze_path = freeze_path.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    contract_header = json.loads(contract_path.read_text(encoding="utf-8"))
    replay_mode = contract_header.get("schema") == "webanything-synthetic-replay-contract-v1"
    contract = (
        load_synthetic_replay_contract(contract_path)
        if replay_mode
        else load_synthetic_contract(contract_path)
    )
    build_mode = "benchmark_synthetic_replay" if replay_mode else "benchmark_synthetic"
    content_mode = "source_fidelity" if replay_mode else contract_content_mode(contract)
    comparison_mode = "source_visible" if content_mode == "source_fidelity" else "content_neutral"
    reference = load_visual_reference(reference_path)
    freeze = load_candidate_freeze(freeze_path)
    identity = freeze.get("identity") or {}
    candidate_before = tree_identity(site_path)
    failures: list[dict[str, Any]] = []
    if freeze.get("state") != "frozen" or identity.get("tree") != candidate_before:
        failures.append({"code": "candidate_identity_mismatch", "message": "candidate is not the frozen tree"})
    visual_contract = contract.get("visual_reference") or {}
    reference_manifest_sha256 = sha256_file(reference_path)
    if (
        (not replay_mode and visual_contract.get("required") is not True)
        or visual_contract.get("sha256") != reference_manifest_sha256
        or reference["reference_sha256"] != visual_contract.get("reference_sha256")
        or reference.get("site") != contract.get("site_id")
    ):
        failures.append({"code": "visual_reference_contract_mismatch", "message": "visual reference is not bound by the synthetic contract"})
    if reference_manifest_sha256 not in {item.get("sha256") for item in identity.get("references") or []}:
        failures.append({"code": "visual_reference_identity_mismatch", "message": "visual reference is not bound by the candidate freeze"})
    if sha256_file(contract_path) not in {item.get("sha256") for item in identity.get("contracts") or []}:
        failures.append({"code": "contract_identity_mismatch", "message": "synthetic contract is not bound by the candidate freeze"})
    declared_routes = {item["path"] for item in contract["routes"]}
    if any(item["synthetic_route"] not in declared_routes for item in reference["routes"]):
        failures.append({"code": "visual_reference_route_mismatch", "message": "visual reference names an undeclared synthetic route"})

    server: subprocess.Popen[bytes] | None = None
    browser = None
    route_results: list[dict[str, Any]] = []
    external_requests: set[str] = set()
    artifact_parent = output_path.parent / f"{output_path.stem}-artifacts"
    if artifact_parent.exists():
        raise SyntheticVisualVerificationError(f"visual verification artifacts already exist: {artifact_parent}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{artifact_parent.name}.", dir=output_path.parent))
    stage = "initialization"
    try:
        if dependency_path:
            sys.path.insert(0, str(dependency_path.resolve()))
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SyntheticVisualVerificationError("Playwright is required for synthetic visual verification") from exc
        if base_url is None:
            port = _free_port()
            if replay_mode:
                entrypoint = (site_path / contract["runtime"]["entrypoint"]).resolve()
                environment = dict(os.environ)
                environment.update({"PORT": str(port), "HOST": "127.0.0.1", "WEBANYTHING_VERIFY": "1"})
                server = subprocess.Popen(
                    [sys.executable, str(entrypoint)],
                    cwd=site_path,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                base_url = f"http://127.0.0.1:{port}/"
                _wait_ready(_route_url(base_url, contract["runtime"]["health_path"]), server)
            else:
                server_script = Path(__file__).resolve().parents[2] / "scripts/static_site_server.py"
                server = subprocess.Popen(
                    [sys.executable, str(server_script), "--root", str(site_path), "--site-id", contract["site_id"], "--port", str(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                base_url = f"http://127.0.0.1:{port}/"
                _wait_ready(base_url + "_health", server)
        base_url = base_url.rstrip("/") + "/"
        base_origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if browser_path:
                launch["executable_path"] = str(browser_path.resolve())
            browser = playwright.chromium.launch(**launch)
            for route_reference in reference["routes"]:
                stage = f"route:{route_reference['synthetic_route']}"
                route_comparison_mode = route_reference.get("comparison_mode", comparison_mode)
                if comparison_mode == "content_neutral" and route_comparison_mode != "content_neutral":
                    raise SyntheticVisualVerificationError(
                        "content-neutral contracts cannot opt a route into source-visible comparison"
                    )
                context = browser.new_context(viewport=reference["viewport"], device_scale_factor=1)
                page = context.new_page()
                page.set_default_timeout(timeout_ms)

                def inspect_request(request: Any) -> None:
                    parsed = urlsplit(request.url)
                    if parsed.scheme in {"http", "https"} and f"{parsed.scheme}://{parsed.netloc}" != base_origin:
                        external_requests.add(request.url)

                page.on("request", inspect_request)
                response = page.goto(_route_url(base_url, route_reference["synthetic_route"]), wait_until="networkidle")
                if response is None or response.status >= 400:
                    raise SyntheticVisualVerificationError(f"candidate route failed: {route_reference['synthetic_route']}")
                if not replay_mode:
                    page.wait_for_function("() => Boolean(window.__WEBANYTHING__)")
                page.wait_for_timeout(350)
                geometry = validate_geometry(page.evaluate(GEOMETRY_SCRIPT))
                raw_png = page.screenshot(type="png", full_page=False, animations="disabled")
                stem = route_reference["id"]
                if route_comparison_mode == "source_visible":
                    comparison_png = raw_png
                    image_path = temporary / f"{stem}.candidate.source-visible.png"
                    reference_image = reference_path.parent / route_reference["screenshot"]["path"]
                else:
                    comparison_png = sanitize_screenshot(raw_png, geometry)
                    image_path = temporary / f"{stem}.candidate.comparison.png"
                    reference_image = reference_path.parent / route_reference["comparison_screenshot"]["path"]
                geometry_path = temporary / f"{stem}.candidate.geometry.json"
                image_path.write_bytes(comparison_png)
                atomic_json(geometry_path, geometry)
                reference_geometry = json.loads((reference_path.parent / route_reference["geometry"]["path"]).read_text(encoding="utf-8"))
                scores = {
                    "masked_region": masked_image_similarity(reference_image, image_path),
                    **compare_geometry(reference_geometry, geometry),
                }
                weights = (
                    SOURCE_FIDELITY_WEIGHTS
                    if route_comparison_mode == "source_visible"
                    else CONTENT_NEUTRAL_WEIGHTS
                )
                scores["composite"] = round(sum(scores[name] * weights[name] for name in weights), 6)
                thresholds = dict(reference["thresholds"])
                if route_comparison_mode == "source_visible":
                    for name, minimum in SOURCE_FIDELITY_MINIMUMS.items():
                        thresholds[name] = max(thresholds[name], minimum)
                    required_metrics = SOURCE_FIDELITY_REQUIRED_METRICS
                else:
                    required_metrics = CONTENT_NEUTRAL_REQUIRED_METRICS
                metric_status = {
                    name: "passed" if scores[name] >= thresholds[name] else "failed"
                    for name in thresholds
                }
                status = "passed" if all(metric_status[name] == "passed" for name in required_metrics) else "failed"
                route_results.append({
                    "route_id": stem,
                    "synthetic_route": route_reference["synthetic_route"],
                    "comparison_mode": route_comparison_mode,
                    "viewport": reference["viewport"],
                    "status": status,
                    "scores": scores,
                    "weights": weights,
                    "thresholds": thresholds,
                    "required_metrics": sorted(required_metrics),
                    "metric_status": metric_status,
                    "repair_hints": _hints(scores, thresholds, comparison_mode=route_comparison_mode),
                    "mismatch_regions": geometry_mismatch_regions(reference_geometry, geometry),
                    "density_deltas": geometry_density_deltas(reference_geometry, geometry),
                    "artifacts": {
                        "comparison_screenshot": {"path": image_path.name, "sha256": sha256_file(image_path)},
                        "geometry": {"path": geometry_path.name, "sha256": sha256_file(geometry_path)},
                    },
                })
                if status == "failed":
                    failures.append({
                        "code": "visual_route_below_threshold",
                        "message": f"synthetic visual route failed: {route_reference['synthetic_route']}",
                        "route_id": stem,
                        "failed_metrics": sorted(
                            name for name in required_metrics if metric_status[name] == "failed"
                        ),
                    })
                context.close()
            browser.close()
            browser = None
        os.replace(temporary, artifact_parent)
    except Exception as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        failures.append({"code": "visual_verification_error", "message": f"{stage}: {type(exc).__name__}: {exc}"})
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    if external_requests:
        failures.append({"code": "external_request", "message": "candidate made external requests during visual verification", "urls": sorted(external_requests)[:20]})
    candidate_after = tree_identity(site_path)
    immutable = candidate_before == candidate_after == (identity.get("tree") or {})
    if not immutable:
        failures.append({"code": "candidate_mutated", "message": "visual verification changed the frozen candidate"})
    result = {
        "schema": VISUAL_VERIFICATION_SCHEMA,
        "status": "passed" if not failures and route_results and all(item["status"] == "passed" for item in route_results) else "failed",
        "repairable": bool(route_results) and bool(failures) and all(
            item.get("code") == "visual_route_below_threshold" for item in failures
        ),
        "site": contract["site_id"],
        "build_mode": build_mode,
        "comparison_mode": comparison_mode,
        "contract_sha256": sha256_file(contract_path),
        "reference_manifest_sha256": reference_manifest_sha256,
        "visual_reference_sha256": reference["reference_sha256"],
        "candidate_identity_sha256": identity.get("sha256"),
        "candidate_sha256": candidate_before["sha256"],
        "candidate": {"pre_hash": candidate_before, "post_hash": candidate_after, "unchanged": immutable},
        "routes": route_results,
        "network": {"unexpected_external_requests": len(external_requests), "urls": sorted(external_requests)},
        "failures": failures,
    }
    atomic_json(output_path, result)
    if repair_package_path is not None:
        atomic_json(
            repair_package_path.resolve(),
            _repair_package(
                site=contract["site_id"],
                candidate=candidate_before,
                route_results=route_results,
                reference_sha256=reference["reference_sha256"],
            ),
        )
    return result
