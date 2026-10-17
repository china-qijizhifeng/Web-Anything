from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import jsonschema
import pytest

from webanything.release import ReleaseError, candidate_identity, freeze_candidate, sha256_file, tree_identity
from webanything.synthetic import (
    INTERACTION_INVENTORY_SELECTOR,
    SyntheticContractError,
    compile_synthetic_contract,
    contract_artifact_paths,
    load_synthetic_contract,
)
from webanything.synthetic_release import release_synthetic_candidate
from webanything.synthetic_visual import (
    CONTENT_STRATEGY,
    GEOMETRY_SCRIPT,
    SyntheticVisualError,
    canonical_sha256,
    compare_geometry,
    geometry_metrics,
    geometry_mismatch_regions,
    geometry_quality,
    load_auth_state,
    load_capture_spec,
    load_visual_reference,
    seal_auth_state,
    validate_geometry,
)
from webanything.synthetic_visual_verify import verify_synthetic_visual
from webanything.synthetic_verify import _mulberry32_values, static_failures, verify_synthetic_candidate
from scripts.deploy_preview_gallery_e2b import (
    _is_asset_html_mismatch,
    _is_benign_request_failure,
    build_package,
    external_check_failures,
    health_site_identity,
    load_manifest,
)
from scripts.preview_gateway import GatewayHandler


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/synthetic/cloudpost"
REFERENCE = EXAMPLE / "contract/visual-reference/synthetic_visual_reference.json"


def test_cloudpost_contract_compiles_deterministically_and_validates(tmp_path: Path):
    output = tmp_path / "contract/synthetic_contract.json"
    contract = compile_synthetic_contract(EXAMPLE / "contract-input/contract-draft.json", output)
    expected = EXAMPLE / "contract/synthetic_contract.json"
    assert output.read_bytes() == expected.read_bytes()
    assert (output.parent / "tasks.jsonl").read_bytes() == (EXAMPLE / "contract/tasks.jsonl").read_bytes()
    assert contract["tasks"]["count"] == 2
    assert contract["storage"]["namespace"] == "webanything.cloudpost.v1"
    assert contract["visual_reference"]["required"] is True
    assert contract["visual_reference"]["sha256"] == sha256_file(REFERENCE)
    schema = json.loads((ROOT / "schemas/synthetic-contract-v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(contract)
    assert load_synthetic_contract(output) == contract
    assert REFERENCE in set(contract_artifact_paths(expected))


def _source_fidelity_fixture(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "source-fidelity-contract"
    site = tmp_path / "source-fidelity-site"
    shutil.copytree(EXAMPLE / "contract", bundle)
    shutil.copytree(EXAMPLE / "site", site)
    contract_path = bundle / "synthetic_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["brand"] = {"name": "CloudPost", "fictional": False}
    contract["content_policy"] = {
        "mode": "source_fidelity",
        "synthetic_only": False,
        "private_user_data_allowed": False,
        "upstream_assets_allowed": True,
        "forbidden_terms": [],
    }
    payload = json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    contract_path.write_text(payload, encoding="utf-8")
    (site / "synthetic_contract.json").write_text(payload, encoding="utf-8")
    provenance_path = site / "content-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance.update({
        "synthetic": False,
        "source_reference_sha256": contract["visual_reference"]["reference_sha256"],
        "upstream_assets": ["app.js", "styles.css"],
    })
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return site, contract_path


def test_source_fidelity_contract_and_provenance_are_explicitly_supported(tmp_path: Path):
    site, contract_path = _source_fidelity_fixture(tmp_path)
    contract = load_synthetic_contract(contract_path)
    schema = json.loads((ROOT / "schemas/synthetic-contract-v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(contract)
    assert static_failures(site, contract, contract_path) == []

    contract["brand"]["fictional"] = True
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(SyntheticContractError, match="source_fidelity requires"):
        load_synthetic_contract(contract_path, verify_artifacts=False)


def test_contract_rejects_mutated_hash_bound_fixture(tmp_path: Path):
    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE / "contract", bundle)
    (bundle / "fixtures/initial-state.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SyntheticContractError, match="fixture hash mismatch"):
        load_synthetic_contract(bundle / "synthetic_contract.json")


def test_interaction_policy_requires_every_declared_action_to_have_task_coverage(tmp_path: Path):
    bundle = tmp_path / "contract-input"
    shutil.copytree(EXAMPLE / "contract-input", bundle)
    draft_path = bundle / "contract-draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["interaction_policy"] = {
        "mode": "all_visible_controls",
        "inventory_selector": INTERACTION_INVENTORY_SELECTOR,
        "action_attribute": "data-action-id",
        "coverage": "all_declared_and_no_extra",
    }
    for route in draft["routes"]:
        route["required_action_ids"] = ["reset_benchmark"]
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    task_path = bundle / "tasks.jsonl"
    tasks = [json.loads(line) for line in task_path.read_text(encoding="utf-8").splitlines() if line]
    tasks[0]["actions"][0]["selector"] = '[data-action-id="reset_benchmark"]'
    tasks[0]["covered_action_ids"] = ["reset_benchmark"]
    task_path.write_text("".join(json.dumps(task) + "\n" for task in tasks), encoding="utf-8")

    output = tmp_path / "compiled/synthetic_contract.json"
    contract = compile_synthetic_contract(draft_path, output)
    assert contract["interaction_policy"]["mode"] == "all_visible_controls"
    assert load_synthetic_contract(output) == contract

    tasks[0].pop("covered_action_ids")
    task_path.write_text("".join(json.dumps(task) + "\n" for task in tasks), encoding="utf-8")
    with pytest.raises(SyntheticContractError, match="covered_action_ids"):
        compile_synthetic_contract(draft_path, tmp_path / "rejected/synthetic_contract.json")


def test_cloudpost_static_candidate_matches_frozen_bundle_and_runtime():
    contract_path = EXAMPLE / "contract/synthetic_contract.json"
    contract = load_synthetic_contract(contract_path)
    assert static_failures(EXAMPLE / "site", contract, contract_path) == []


def test_visual_reference_is_frozen_sanitized_and_self_consistent():
    reference = load_visual_reference(REFERENCE)
    assert reference["state"] == "reference_frozen"
    assert reference["raw_material_persisted"] is True
    assert reference["content_strategy"] == CONTENT_STRATEGY == "source_visible"
    assert reference["auth"] == {"used": False, "profile_id": None}
    assert len(reference["routes"]) == 5
    geometry_path = REFERENCE.parent / reference["routes"][0]["geometry"]["path"]
    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    assert compare_geometry(geometry, geometry) == {
        "geometry": 1.0,
        "density": 1.0,
        "columns": 1.0,
        "spacing": 1.0,
    }
    assert "style.backgroundImage" in GEOMETRY_SCRIPT
    assert "'iframe','object','embed'" in GEOMETRY_SCRIPT
    assert "if (tag === 'picture') continue" in GEOMETRY_SCRIPT
    assert "boundedIdentity" in GEOMETRY_SCRIPT
    assert "rect.width <= viewport.width * 0.65" in GEOMETRY_SCRIPT


def test_capture_spec_accepts_a_deterministic_browser_profile(tmp_path: Path):
    source = ROOT / "examples/synthetic/cloudpost/visual-capture-spec.local-test.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["browser_profile"] = {
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
        "accept_language": "en-US,en;q=0.9",
    }
    path = tmp_path / "capture-spec.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_capture_spec(path, allow_private_network=True)
    assert loaded["browser_profile"] == payload["browser_profile"]

    payload["routes"][0]["comparison_mode"] = "content_neutral"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_capture_spec(path, allow_private_network=True)
    assert loaded["routes"][0]["comparison_mode"] == "content_neutral"

    payload["routes"][0]["comparison_mode"] = "unsafe"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SyntheticVisualError, match="comparison_mode"):
        load_capture_spec(path, allow_private_network=True)
    payload["routes"][0].pop("comparison_mode")

    payload["browser_profile"]["accept_language"] = "en-US\nX-Test: unsafe"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SyntheticVisualError, match="accept_language"):
        load_capture_spec(path, allow_private_network=True)
    assert "tag.includes('-')" not in GEOMETRY_SCRIPT


def test_visual_reference_supports_per_route_privacy_safe_comparison(tmp_path: Path):
    bundle = tmp_path / "visual-reference"
    shutil.copytree(REFERENCE.parent, bundle)
    path = bundle / REFERENCE.name
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["routes"][0]["comparison_mode"] = "content_neutral"
    payload["reference_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "reference_sha256"
    })
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    loaded = load_visual_reference(path)
    assert loaded["routes"][0]["comparison_mode"] == "content_neutral"

    payload["routes"][0]["comparison_mode"] = "unsafe"
    payload["reference_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "reference_sha256"
    })
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SyntheticVisualError, match=r"route\[0\]"):
        load_visual_reference(path)


def test_visual_reference_loader_honors_frozen_accepted_quality_failures(tmp_path: Path):
    bundle = tmp_path / "visual-reference"
    shutil.copytree(REFERENCE.parent, bundle)
    path = bundle / REFERENCE.name
    payload = json.loads(path.read_text(encoding="utf-8"))
    route = payload["routes"][0]
    geometry_path = bundle / route["geometry"]["path"]
    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    geometry["landmarks"] = {}
    geometry_path.write_text(json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected_quality = geometry_quality(validate_geometry(geometry))
    failure_codes = sorted(item["code"] for item in expected_quality["failures"])
    assert failure_codes == ["landmarks_missing"]
    route["geometry"]["sha256"] = sha256_file(geometry_path)
    route["accepted_quality_failure_codes"] = failure_codes
    route["quality"] = {**expected_quality, "status": "passed", "failures": []}
    payload["reference_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "reference_sha256"
    })
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    loaded = load_visual_reference(path)
    assert loaded["routes"][0]["accepted_quality_failure_codes"] == ["landmarks_missing"]

    payload["routes"][0]["accepted_quality_failure_codes"] = []
    payload["reference_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "reference_sha256"
    })
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(SyntheticVisualError, match="accepted quality failures"):
        load_visual_reference(path)


def _geometry(elements, *, landmarks=None):
    return validate_geometry({
        "schema": "webanything-sanitized-dom-geometry-v1",
        "viewport": {"width": 1440, "height": 1000},
        "elements": [{
            "position": "flow",
            "radius": 0,
            "surface": False,
            **item,
        } for item in elements],
        "landmarks": landmarks or {},
        "truncated": False,
    })


def test_geometry_normalization_and_union_metrics_remove_nested_inflation():
    geometry = _geometry([
        {"category": "container", "x": 0, "y": 0, "width": 1440, "height": 1000},
        {"category": "media", "x": 100, "y": 100, "width": 300, "height": 200, "surface": True},
        {"category": "media", "x": 101, "y": 101, "width": 299, "height": 199, "surface": True},
        {"category": "media", "x": 120, "y": 120, "width": 40, "height": 40, "surface": True},
        {"category": "text", "x": 100, "y": 320, "width": 240, "height": 24},
        {"category": "control", "x": 100, "y": 360, "width": 80, "height": 32, "surface": True},
        {"category": "container", "x": 80, "y": 80, "width": 360, "height": 340, "surface": True},
    ], landmarks={"main": {"x": 80, "y": 80, "width": 360, "height": 340}})
    metrics = geometry_metrics(geometry)
    assert geometry["normalization"]["dropped_page_ancestors"] == 1
    assert geometry["normalization"]["dropped_overlaps"] == 1
    assert geometry["normalization"]["dropped_nested_media"] == 1
    assert metrics["counts"]["media"] == 1
    assert 0 < metrics["coverage"]["media"] < 1
    assert max(metrics["columns"]) <= 1
    assert geometry_quality(geometry)["status"] == "passed"


def test_geometry_quality_rejects_page_covering_media_and_missing_landmarks():
    geometry = _geometry([
        {"category": "media", "x": 0, "y": 0, "width": 1440, "height": 1000, "surface": True},
        {"category": "text", "x": 20, "y": 20, "width": 100, "height": 20},
        {"category": "control", "x": 20, "y": 60, "width": 80, "height": 30, "surface": True},
        {"category": "identity", "x": 20, "y": 100, "width": 40, "height": 40},
    ])
    quality = geometry_quality(geometry)
    assert quality["status"] == "failed"
    assert {item["code"] for item in quality["failures"]} >= {"page_covering_rectangle", "column_saturation", "landmarks_missing"}


def test_geometry_quality_allows_a_dense_structured_grid():
    geometry = _geometry([
        {
            "category": "container",
            "x": column * 60,
            "y": 0,
            "width": 60,
            "height": 1000,
            "surface": True,
        }
        for column in range(20)
    ], landmarks={"main": {"x": 0, "y": 0, "width": 1200, "height": 1000}})
    quality = geometry_quality(geometry)
    assert quality["metrics"]["saturated_columns"] == 20
    assert quality["status"] == "passed"


def test_geometry_mismatch_regions_expose_bounded_coordinates_without_content():
    reference = _geometry([
        {"category": "media", "x": 100, "y": 100, "width": 400, "height": 220, "surface": True},
        {"category": "text", "x": 100, "y": 340, "width": 300, "height": 30},
        {"category": "control", "x": 100, "y": 390, "width": 90, "height": 30, "surface": True},
        {"category": "container", "x": 80, "y": 80, "width": 440, "height": 360, "surface": True},
    ], landmarks={"main": {"x": 80, "y": 80, "width": 440, "height": 360}})
    candidate = _geometry([
        {"category": "media", "x": 700, "y": 100, "width": 300, "height": 180, "surface": True},
        {"category": "text", "x": 700, "y": 300, "width": 240, "height": 30},
        {"category": "control", "x": 700, "y": 350, "width": 90, "height": 30, "surface": True},
        {"category": "container", "x": 680, "y": 80, "width": 340, "height": 320, "surface": True},
    ], landmarks={"main": {"x": 680, "y": 80, "width": 340, "height": 320}})
    regions = geometry_mismatch_regions(reference, candidate)
    assert regions
    assert len(regions) <= 24
    assert {item["kind"] for item in regions} == {"missing", "extra"}
    assert all(set(item) == {"kind", "category", "x", "y", "width", "height", "grid_cells"} for item in regions)


def test_authorized_storage_state_is_encrypted_and_origin_bound(tmp_path: Path):
    storage = tmp_path / "storage-state.json"
    storage.write_text(json.dumps({
        "cookies": [{"name": "session", "value": "private-cookie-value", "domain": ".example.test", "path": "/"}],
        "origins": [{"origin": "https://app.example.test", "localStorage": [{"name": "session", "value": "private-local-value"}]}],
    }), encoding="utf-8")
    key = tmp_path / "auth.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    profile = tmp_path / "cloudpost.auth-profile.enc.json"
    public = seal_auth_state(
        storage_state_path=storage,
        output_path=profile,
        key_path=key,
        site="cloudpost",
        account="demo-account",
        allowed_origins=["https://app.example.test"],
    )
    assert public["site"] == "cloudpost"
    assert "private-cookie-value" not in profile.read_text(encoding="utf-8")
    restored, metadata = load_auth_state(profile_path=profile, key_path=key, site="cloudpost")
    assert restored == json.loads(storage.read_text(encoding="utf-8"))
    assert metadata["profile_id"] == public["profile_id"]


def test_auth_profile_rejects_cookie_for_undeclared_child_origin(tmp_path: Path):
    storage = tmp_path / "storage-state.json"
    storage.write_text(json.dumps({
        "cookies": [{"name": "session", "value": "private", "domain": "evil.app.example.test", "path": "/"}],
        "origins": [],
    }), encoding="utf-8")
    key = tmp_path / "auth.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    with pytest.raises(RuntimeError, match="outside allowed origins"):
        seal_auth_state(
            storage_state_path=storage,
            output_path=tmp_path / "auth.enc.json",
            key_path=key,
            site="cloudpost",
            account="demo-account",
            allowed_origins=["https://app.example.test"],
        )


def test_mulberry32_reference_sequence_is_stable():
    assert _mulberry32_values(1515, 3) == [
        0.16343899886123836,
        0.9485675001051277,
        0.7789451458957046,
    ]


def test_content_provenance_must_enumerate_every_candidate_file(tmp_path: Path):
    site = tmp_path / "site"
    shutil.copytree(EXAMPLE / "site", site)
    provenance_path = site / "content-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["assets"].remove("app.js")
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    contract_path = EXAMPLE / "contract/synthetic_contract.json"
    failures = static_failures(site, load_synthetic_contract(contract_path), contract_path)
    assert any(item["code"] == "content_provenance_incomplete" for item in failures)


def _freeze_example(tmp_path: Path) -> tuple[Path, Path, dict]:
    site = tmp_path / "site"
    shutil.copytree(EXAMPLE / "site", site)
    contract = EXAMPLE / "contract/synthetic_contract.json"
    identity = candidate_identity(
        site,
        contract_paths=[contract],
        reference_paths=[REFERENCE],
        configuration={
            "site": "cloudpost",
            "profile": "benchmark_synthetic",
            "build_mode": "benchmark_synthetic",
        },
    )
    freeze = tmp_path / "candidate-freeze.json"
    freeze_candidate(freeze, identity)
    return site, freeze, identity


def test_synthetic_release_is_hash_bound_and_schema_valid(tmp_path: Path):
    site, freeze, identity = _freeze_example(tmp_path)
    contract = EXAMPLE / "contract/synthetic_contract.json"
    state_hash = "a" * 64
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({
        "schema": "webanything-synthetic-verification-v1",
        "status": "passed",
        "site": "cloudpost",
        "build_mode": "benchmark_synthetic",
        "candidate_identity_sha256": identity["sha256"],
        "candidate_sha256": identity["tree"]["sha256"],
        "contract_path": str(contract),
        "contract_sha256": sha256_file(contract),
        "checks": {name: "passed" for name in (
            "contract", "routes", "tasks", "reset", "persistence", "determinism",
            "resource_privacy", "content_policy", "candidate_immutability",
        )},
        "reset": {"first_sha256": state_hash, "second_sha256": state_hash, "identical": True},
        "tasks": [
            {"task_id": "cloudpost_001", "status": "passed", "failures": []},
            {"task_id": "cloudpost_002", "status": "passed", "failures": []},
        ],
        "network": {"unexpected_external_requests": 0, "urls": []},
        "candidate": {"pre_hash": identity["tree"], "post_hash": identity["tree"], "unchanged": True},
        "failures": [],
    }, indent=2), encoding="utf-8")
    visual_verification = tmp_path / "visual-verification.json"
    visual_verification.write_text(json.dumps({
        "schema": "webanything-synthetic-visual-verification-v1",
        "status": "passed",
        "repairable": False,
        "site": "cloudpost",
        "build_mode": "benchmark_synthetic",
        "comparison_mode": "content_neutral",
        "contract_sha256": sha256_file(contract),
        "reference_manifest_sha256": sha256_file(REFERENCE),
        "visual_reference_sha256": load_visual_reference(REFERENCE)["reference_sha256"],
        "candidate_identity_sha256": identity["sha256"],
        "candidate_sha256": identity["tree"]["sha256"],
        "candidate": {"pre_hash": identity["tree"], "post_hash": identity["tree"], "unchanged": True},
        "routes": [{"route_id": "route_inbox", "status": "passed"}],
        "network": {"unexpected_external_requests": 0, "urls": []},
        "failures": [],
    }, indent=2), encoding="utf-8")
    manifest = release_synthetic_candidate(
        site="cloudpost",
        site_path=site,
        contract_path=contract,
        verification_path=verification,
        visual_reference_path=REFERENCE,
        visual_verification_path=visual_verification,
        freeze_path=freeze,
        output_dir=tmp_path / "release",
    )
    gate = json.loads((tmp_path / "release/release-gate-result.json").read_text(encoding="utf-8"))
    assert manifest["build_mode"] == "benchmark_synthetic"
    assert manifest["deployment_binding"]["candidate_identity_sha256"] == identity["sha256"]
    assert gate["status"] == "passed"
    for filename, value in (
        ("release-gate-result-v1.schema.json", gate),
        ("release-manifest-v2.schema.json", manifest),
    ):
        schema = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(value)
    stale_visual = tmp_path / "stale-visual-verification.json"
    stale_payload = json.loads(visual_verification.read_text(encoding="utf-8"))
    stale_payload["visual_reference_sha256"] = "0" * 64
    stale_visual.write_text(json.dumps(stale_payload), encoding="utf-8")
    with pytest.raises(ReleaseError, match="synthetic release gate failed"):
        release_synthetic_candidate(
            site="cloudpost",
            site_path=site,
            contract_path=contract,
            verification_path=verification,
            visual_reference_path=REFERENCE,
            visual_verification_path=stale_visual,
            freeze_path=freeze,
            output_dir=tmp_path / "release-stale-visual",
        )
    wrong_mode_visual = tmp_path / "wrong-mode-visual-verification.json"
    wrong_mode_payload = json.loads(visual_verification.read_text(encoding="utf-8"))
    wrong_mode_payload["comparison_mode"] = "source_visible"
    wrong_mode_visual.write_text(json.dumps(wrong_mode_payload), encoding="utf-8")
    with pytest.raises(ReleaseError, match="synthetic release gate failed"):
        release_synthetic_candidate(
            site="cloudpost",
            site_path=site,
            contract_path=contract,
            verification_path=verification,
            visual_reference_path=REFERENCE,
            visual_verification_path=wrong_mode_visual,
            freeze_path=freeze,
            output_dir=tmp_path / "release-wrong-visual-mode",
        )
    unsafe_visual = tmp_path / "unsafe-visual-verification.json"
    unsafe_payload = json.loads(visual_verification.read_text(encoding="utf-8"))
    unsafe_payload["network"] = {"unexpected_external_requests": 1, "urls": ["https://outside.example/"]}
    unsafe_visual.write_text(json.dumps(unsafe_payload), encoding="utf-8")
    with pytest.raises(ReleaseError, match="synthetic release gate failed"):
        release_synthetic_candidate(
            site="cloudpost",
            site_path=site,
            contract_path=contract,
            verification_path=verification,
            visual_reference_path=REFERENCE,
            visual_verification_path=unsafe_visual,
            freeze_path=freeze,
            output_dir=tmp_path / "release-unsafe-visual",
        )
    (site / "app.js").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="synthetic release gate failed"):
        release_synthetic_candidate(
            site="cloudpost",
            site_path=site,
            contract_path=contract,
            verification_path=verification,
            visual_reference_path=REFERENCE,
            visual_verification_path=visual_verification,
            freeze_path=freeze,
            output_dir=tmp_path / "release-after-mutation",
        )


def test_static_gallery_package_declares_static_runtime(tmp_path: Path):
    workspace = tmp_path / "workspace"
    site = workspace / "sites/cloudpost"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>CloudPost</h1>\n", encoding="utf-8")
    manifest_path = tmp_path / "gallery.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 2,
        "sites": [{
            "id": "cloudpost",
            "source_workspace": str(workspace),
            "source_site": "cloudpost",
            "runtime": "static",
            "port": 41001,
            "expected_site": "cloudpost",
        }],
    }), encoding="utf-8")
    manifest = load_manifest(manifest_path)
    archive = tmp_path / "gallery.tar.gz"
    build_package(archive, manifest, ROOT / "scripts/preview_gateway.py")
    with tarfile.open(archive, "r:gz") as package:
        members = set(package.getnames())
        catalog = json.loads(package.extractfile("preview_manifest.json").read())
    assert "sites/cloudpost/index.html" in members
    assert "scripts/static_site_server.py" in members
    assert catalog["sites"][0]["runtime"] == "static"


def test_gallery_health_accepts_static_and_replay_site_identity_fields():
    assert health_site_identity({"site": "cloudpost"}) == "cloudpost"
    assert health_site_identity({"site_id": "video_replay"}) == "video_replay"


def test_gateway_does_not_duplicate_an_existing_site_prefix_on_redirect():
    assert GatewayHandler.rewrite_location(None, "/p/40000/", 40000) == "/p/40000/"
    assert GatewayHandler.rewrite_location(None, "/login", 40000) == "/p/40000/login"


def test_replay_gallery_manifest_enables_all_public_browser_routes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    site = workspace / "sites/video_replay"
    site.mkdir(parents=True)
    (site / "app.py").write_text("app = None\n", encoding="utf-8")
    (site / "synthetic_replay_contract.json").write_text(json.dumps({
        "routes": [{"path": "/"}, {"path": "/feed/shorts"}, {"path": "/feed/shorts"}],
        "tasks": {"path": "tasks.jsonl"},
        "evaluator": {"endpoint_template": "/api/evaluators/{evaluator_id}", "result_field": "passed"},
        "reset": {"endpoint": "/api/reset"},
    }), encoding="utf-8")
    (site / "tasks.jsonl").write_text(json.dumps({
        "task_id": "login_task",
        "workflow": "login",
        "start_url": "/login",
        "actions": [{"type": "click", "selector": "[data-action-id=login_submit]"}],
        "evaluator_id": "eval_login",
    }) + "\n", encoding="utf-8")
    manifest_path = tmp_path / "gallery.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 2,
        "sites": [{
            "id": "video_replay",
            "source_workspace": str(workspace),
            "source_site": "video_replay",
            "runtime": "flask",
            "port": 41001,
        }],
    }), encoding="utf-8")

    manifest = load_manifest(manifest_path)

    assert manifest["sites"][0]["_browser_paths"] == ["/", "/feed/shorts"]
    assert manifest["sites"][0]["_browser_auth"]["login_task"]["start_url"] == "/login"


def test_public_replay_browser_failure_blocks_deployment():
    manifest = {"sites": [{"id": "video_replay", "_browser_paths": ["/"]}]}
    result = {
        "external_checks": {
            "gateway_health": {"status": 200},
            "gallery": {"status": 200, "missing_entries": []},
            "video_replay": {
                "status": 200,
                "contains_expected": True,
                "browser": {"passed": False},
            },
        },
    }

    assert external_check_failures(result, manifest) == [
        "video_replay public replay browser verification failed"
    ]


def test_only_aborted_image_fallbacks_are_benign_request_failures():
    assert _is_benign_request_failure("image", "net::ERR_ABORTED") is True
    assert _is_benign_request_failure("script", "net::ERR_ABORTED") is False
    assert _is_benign_request_failure("image", "net::ERR_FAILED") is False


def test_asset_html_mismatch_ignores_html_navigation_hints():
    assert _is_asset_html_mismatch("stylesheet", "text/html; charset=utf-8") is True
    assert _is_asset_html_mismatch("script", "text/html") is True
    assert _is_asset_html_mismatch("other", "text/html; charset=utf-8") is False
    assert _is_asset_html_mismatch("document", "text/html") is False
    assert _is_asset_html_mismatch("image", "image/webp") is False


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


@pytest.mark.skipif(not os.environ.get("WEBANYTHING_TEST_BROWSER"), reason="test browser is not configured")
def test_static_gallery_gateway_preserves_prefixed_routes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    shutil.copytree(EXAMPLE / "site", workspace / "sites/cloudpost")
    gateway_port = _free_port()
    site_port = _free_port()
    manifest_path = tmp_path / "gallery.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 2,
        "gateway_port": gateway_port,
        "sites": [{
            "id": "cloudpost",
            "source_workspace": str(workspace),
            "source_site": "cloudpost",
            "runtime": "static",
            "port": site_port,
            "expected_site": "cloudpost",
        }],
    }), encoding="utf-8")
    manifest = load_manifest(manifest_path)
    archive = tmp_path / "gallery.tar.gz"
    build_package(archive, manifest, ROOT / "scripts/preview_gateway.py")
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    with tarfile.open(archive, "r:gz") as package:
        package.extractall(deployed, filter="data")
    supervisor = subprocess.Popen([
        sys.executable,
        str(deployed / "scripts/preview_gallery_supervisor.py"),
        "--root", str(deployed),
        "--gateway-port", str(gateway_port),
        "--site", f"cloudpost={site_port}=static",
        "--log-dir", str(tmp_path / "logs"),
        "--pid-file", str(tmp_path / "supervisor.pid"),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        health = f"http://127.0.0.1:{gateway_port}/__health"
        while True:
            try:
                with urllib.request.urlopen(health, timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                if supervisor.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        dependency = os.environ.get("WEBHARBOR_BUILDER_DEPENDENCY_PATH")
        if dependency:
            sys.path.insert(0, dependency)
        from playwright.sync_api import sync_playwright

        external: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ["WEBANYTHING_TEST_BROWSER"], headless=True)
            page = browser.new_page()
            page.on("request", lambda request: external.append(request.url) if not request.url.startswith(f"http://127.0.0.1:{gateway_port}/") else None)
            page.goto(f"http://127.0.0.1:{gateway_port}/p/{site_port}/message/msg_004", wait_until="networkidle")
            page.wait_for_function("() => Boolean(window.__WEBANYTHING__)")
            assert page.locator("[data-route=message][data-object-id=msg_004]").count() == 1
            page.goto(f"http://127.0.0.1:{gateway_port}/p/{site_port}/inbox", wait_until="networkidle")
            page.locator('[data-task-action="star-msg_004"]').click()
            assert page.evaluate("() => window.__WEBANYTHING__.getState().messages.msg_004.starred") is True
            assert external == []
            browser.close()
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=8)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)


@pytest.mark.skipif(not os.environ.get("WEBANYTHING_TEST_BROWSER"), reason="test browser is not configured")
def test_cloudpost_full_browser_verification(tmp_path: Path):
    site, freeze, _identity = _freeze_example(tmp_path)
    result = verify_synthetic_candidate(
        site_path=site,
        contract_path=EXAMPLE / "contract/synthetic_contract.json",
        freeze_path=freeze,
        output_path=tmp_path / "verification.json",
        browser_path=Path(os.environ["WEBANYTHING_TEST_BROWSER"]),
        dependency_path=Path(os.environ["WEBHARBOR_BUILDER_DEPENDENCY_PATH"]),
    )
    assert result["status"] == "passed", result["failures"]
    assert result["reset"]["identical"] is True
    assert [item["status"] for item in result["tasks"]] == ["passed", "passed"]
    assert result["candidate"]["pre_hash"] == result["candidate"]["post_hash"] == tree_identity(site)


@pytest.mark.skipif(not os.environ.get("WEBANYTHING_TEST_BROWSER"), reason="test browser is not configured")
def test_cloudpost_full_structural_visual_verification(tmp_path: Path):
    site, freeze, _identity = _freeze_example(tmp_path)
    output = tmp_path / "synthetic-visual.json"
    repair = tmp_path / "visual-repair-package.json"
    result = verify_synthetic_visual(
        site_path=site,
        contract_path=EXAMPLE / "contract/synthetic_contract.json",
        reference_path=REFERENCE,
        freeze_path=freeze,
        output_path=output,
        repair_package_path=repair,
        browser_path=Path(os.environ["WEBANYTHING_TEST_BROWSER"]),
        dependency_path=Path(os.environ["WEBHARBOR_BUILDER_DEPENDENCY_PATH"]),
    )
    assert result["status"] == "passed", result["failures"]
    assert result["comparison_mode"] == "content_neutral"
    assert result["repairable"] is False
    assert all(route["status"] == "passed" for route in result["routes"])
    assert all(route["weights"]["masked_region"] == 0.55 for route in result["routes"])
    assert all(sum(route["weights"].values()) == pytest.approx(1.0) for route in result["routes"])
    assert all(
        route["required_metrics"] == ["columns", "composite", "masked_region", "spacing"]
        for route in result["routes"]
    )
    assert json.loads(repair.read_text(encoding="utf-8"))["status"] == "empty"
    schema = json.loads((ROOT / "schemas/synthetic-visual-verification-v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(result)


@pytest.mark.skipif(not os.environ.get("WEBANYTHING_TEST_BROWSER"), reason="test browser is not configured")
def test_source_fidelity_visual_verification_uses_unmasked_reference(tmp_path: Path):
    site, contract = _source_fidelity_fixture(tmp_path)
    identity = candidate_identity(
        site,
        contract_paths=[contract],
        reference_paths=[REFERENCE],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic", "build_mode": "benchmark_synthetic"},
    )
    freeze = tmp_path / "source-fidelity-freeze.json"
    freeze_candidate(freeze, identity)
    repair = tmp_path / "source-fidelity-repair.json"
    result = verify_synthetic_visual(
        site_path=site,
        contract_path=contract,
        reference_path=REFERENCE,
        freeze_path=freeze,
        output_path=tmp_path / "source-fidelity-visual.json",
        repair_package_path=repair,
        browser_path=Path(os.environ["WEBANYTHING_TEST_BROWSER"]),
        dependency_path=Path(os.environ["WEBHARBOR_BUILDER_DEPENDENCY_PATH"]),
    )
    assert result["status"] == "passed", result["failures"]
    assert result["comparison_mode"] == "source_visible"
    assert all(route["comparison_mode"] == "source_visible" for route in result["routes"])
    assert all(route["thresholds"]["masked_region"] >= 0.94 for route in result["routes"])
    assert all(route["thresholds"]["composite"] >= 0.85 for route in result["routes"])
    assert all(route["weights"]["masked_region"] == 0.80 for route in result["routes"])
    assert all(sum(route["weights"].values()) == pytest.approx(1.0) for route in result["routes"])
    assert all(route["required_metrics"] == ["composite", "masked_region"] for route in result["routes"])
    package = json.loads(repair.read_text(encoding="utf-8"))
    assert all(
        item["source_content_available"] is True
        for group in package["groups"]
        for item in group["items"]
    )
    schema = json.loads((ROOT / "schemas/synthetic-visual-verification-v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(result)


@pytest.mark.skipif(not os.environ.get("WEBANYTHING_TEST_BROWSER"), reason="test browser is not configured")
def test_source_fidelity_visual_verification_allows_private_routes_to_be_content_neutral(tmp_path: Path):
    site, contract_path = _source_fidelity_fixture(tmp_path)
    reference_path = contract_path.parent / "visual-reference/synthetic_visual_reference.json"
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    reference["routes"][0]["comparison_mode"] = "content_neutral"
    reference["reference_sha256"] = canonical_sha256({
        key: value for key, value in reference.items() if key != "reference_sha256"
    })
    reference_path.write_text(
        json.dumps(reference, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["visual_reference"]["sha256"] = sha256_file(reference_path)
    contract["visual_reference"]["reference_sha256"] = reference["reference_sha256"]
    payload = json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    contract_path.write_text(payload, encoding="utf-8")
    (site / "synthetic_contract.json").write_text(payload, encoding="utf-8")
    provenance_path = site / "content-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["source_reference_sha256"] = reference["reference_sha256"]
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    identity = candidate_identity(
        site,
        contract_paths=[contract_path],
        reference_paths=[reference_path],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic", "build_mode": "benchmark_synthetic"},
    )
    freeze = tmp_path / "mixed-comparison-freeze.json"
    freeze_candidate(freeze, identity)
    result = verify_synthetic_visual(
        site_path=site,
        contract_path=contract_path,
        reference_path=reference_path,
        freeze_path=freeze,
        output_path=tmp_path / "mixed-comparison-visual.json",
        browser_path=Path(os.environ["WEBANYTHING_TEST_BROWSER"]),
        dependency_path=Path(os.environ["WEBHARBOR_BUILDER_DEPENDENCY_PATH"]),
    )
    assert result["status"] == "passed", result["failures"]
    assert result["comparison_mode"] == "source_visible"
    assert result["routes"][0]["comparison_mode"] == "content_neutral"
    assert result["routes"][0]["weights"]["masked_region"] == 0.55
    assert result["routes"][0]["required_metrics"] == [
        "columns",
        "composite",
        "masked_region",
        "spacing",
    ]
    assert all(
        route["comparison_mode"] == "source_visible"
        for route in result["routes"][1:]
    )


@pytest.mark.skipif(not os.environ.get("WEBANYTHING_TEST_BROWSER"), reason="test browser is not configured")
def test_visual_failure_emits_sanitized_bounded_repair_package(tmp_path: Path):
    site = tmp_path / "site"
    shutil.copytree(EXAMPLE / "site", site)
    with (site / "styles.css").open("a", encoding="utf-8") as handle:
        handle.write("\naside.sidebar { display: none !important; } .workspace { grid-template-columns: 1fr !important; }\n")
    contract = EXAMPLE / "contract/synthetic_contract.json"
    identity = candidate_identity(
        site,
        contract_paths=[contract],
        reference_paths=[REFERENCE],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic", "build_mode": "benchmark_synthetic"},
    )
    freeze = tmp_path / "candidate-freeze.json"
    freeze_candidate(freeze, identity)
    repair = tmp_path / "visual-repair-package.json"
    result = verify_synthetic_visual(
        site_path=site,
        contract_path=contract,
        reference_path=REFERENCE,
        freeze_path=freeze,
        output_path=tmp_path / "synthetic-visual.json",
        repair_package_path=repair,
        browser_path=Path(os.environ["WEBANYTHING_TEST_BROWSER"]),
        dependency_path=Path(os.environ["WEBHARBOR_BUILDER_DEPENDENCY_PATH"]),
    )
    package = json.loads(repair.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["repairable"] is True
    assert package["status"] == "repair_required"
    assert package["failure_count"] == package["group_count"] > 0
    assert all(item["source_content_available"] is False for group in package["groups"] for item in group["items"])
    assert all(item["mismatch_regions"] for group in package["groups"] for item in group["items"])
    assert all(item["density_deltas"] for group in package["groups"] for item in group["items"])
    schema = json.loads((ROOT / "pipeline-tools/interaction-verifier-v1/schemas/builder-repair-package-v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(package)
