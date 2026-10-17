from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import jsonschema

from webanything.release import (
    ReleaseError,
    candidate_identity,
    freeze_candidate,
    invalidate_downstream_evidence,
    release_candidate,
)


def _write(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def prepare(tmp_path: Path):
    site = tmp_path / "run/workspace/sites/demo"
    seed = site / "instance_seed/demo.db"
    runtime = site / "instance/demo.db"
    seed.parent.mkdir(parents=True)
    runtime.parent.mkdir(parents=True)
    seed.write_bytes(b"deterministic-seed")
    runtime.write_bytes(seed.read_bytes())
    (site / "app.py").write_text("app = object()\n", encoding="utf-8")
    contract = tmp_path / "run/contract/site-contract.json"
    _write(contract, {"schema": "contract-v1", "routes": ["/"]})
    identity = candidate_identity(site, contract_paths=[contract], configuration={"profile": "immersive", "site": "demo"})
    freeze = tmp_path / "run/candidate-freeze.json"
    freeze_candidate(freeze, identity)
    tree = identity["tree"]["sha256"]
    common = {"status": "passed", "candidate_identity_sha256": identity["sha256"], "candidate_sha256": tree}
    contract_sha = identity["contracts"][0]["sha256"]
    fidelity = _write(tmp_path / "run/verification/fidelity.json", {
        **common, "schema": "webharbor-fidelity-v19", "fidelity_contract_version": 3,
        "contract_sha256": contract_sha, "require_complete_resources": True,
        "runtime_javascript_check": True, "failures": [],
        "route_results": [{"route": "/", "status": "passed"}],
    })
    evidence = [
        fidelity,
        _write(tmp_path / "run/verification/quality.json", {
            **common, "schema": "webharbor-quality-verifier-v1", "quality_contract_version": 1,
            "contract_sha256": contract_sha, "fidelity_evidence_sha256": hashlib.sha256(fidelity.read_bytes()).hexdigest(),
            "grade": "full_fidelity", "failures": [],
            "components": {name: {"status": "passed"} for name in ("design", "data", "interaction")},
        }),
        _write(tmp_path / "run/verification/resource.json", {**common, "schema": "webanything-resource-privacy-audit-v1", "unexpected_external_requests": 0, "unresolved_external_requests": [], "private_evidence_paths": []}),
        _write(tmp_path / "run/verification/reset.json", {**common, "schema": "webanything-runtime-reset-v1", "seed_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(), "runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(), "byte_identical": True}),
        _write(tmp_path / "run/verification/terminal-result.json", {
            **common,
            "terminal_result": "webharbor-interaction-verifier-terminal-v1",
            "input_contract": {"path": str(contract), "sha256": identity["contracts"][0]["sha256"]},
            "candidate": {"pre_hash": identity["tree"], "post_hash": identity["tree"], "unchanged": True},
            "closure": {"status": "passed"},
            "exhaustive": {"status": "passed", "merge": {
                "schema": "webharbor-interaction-verification-merge-v1", "status": "passed",
                "expected_batch_count": 2, "present_batch_count": 2,
                "assigned_item_count": 5, "covered_item_count": 5,
            }},
        }),
    ]
    return site, seed, runtime, freeze, evidence, identity


def release(tmp_path: Path, **changes):
    site, seed, runtime, freeze, evidence, identity = prepare(tmp_path)
    options = dict(
        site="demo", profile="immersive", site_path=site, seed_db=seed, runtime_db=runtime,
        verification_paths=evidence, output_dir=tmp_path / "run/release", freeze_path=freeze,
    )
    options.update(changes)
    return release_candidate(**options), (site, seed, runtime, freeze, evidence, identity)


def test_fully_valid_authoritative_release_is_hash_bound_and_read_only(tmp_path: Path):
    manifest, (site, seed, runtime, freeze, evidence, identity) = release(tmp_path)
    before = site.joinpath("app.py").read_bytes()
    assert manifest["schema"] == "webanything-release-manifest-v2"
    assert manifest["candidate_identity"]["sha256"] == identity["sha256"]
    assert manifest["deployment_binding"]["candidate_sha256"] == identity["tree"]["sha256"]
    gate = json.loads((tmp_path / "run/release/release-gate-result.json").read_text())
    assert gate["status"] == "passed"
    assert gate["required_gates"]["candidate_immutability"]["status"] == "passed"
    assert site.joinpath("app.py").read_bytes() == before
    root = Path(__file__).parents[1]
    jsonschema.Draft202012Validator(json.loads((root / "schemas/release-gate-result-v1.schema.json").read_text())).validate(gate)
    jsonschema.Draft202012Validator(json.loads((root / "schemas/release-manifest-v2.schema.json").read_text())).validate(manifest)


@pytest.mark.parametrize("mode", ["missing", "bare", "stale", "incomplete", "missing_closure", "failed_reset"])
def test_release_rejects_missing_fabricated_stale_incomplete_or_failed_evidence(tmp_path: Path, mode: str):
    site, seed, runtime, freeze, evidence, identity = prepare(tmp_path)
    if mode == "missing":
        evidence = evidence[:-1]
    elif mode == "bare":
        _write(evidence[0], {"status": "passed"})
    elif mode == "stale":
        value = json.loads(evidence[0].read_text())
        value["candidate_identity_sha256"] = "0" * 64
        _write(evidence[0], value)
    elif mode == "incomplete":
        value = json.loads(evidence[-1].read_text())
        value["exhaustive"]["merge"]["present_batch_count"] = 1
        _write(evidence[-1], value)
    elif mode == "missing_closure":
        value = json.loads(evidence[-1].read_text())
        value.pop("closure")
        _write(evidence[-1], value)
    else:
        value = json.loads(evidence[3].read_text())
        value["byte_identical"] = False
        _write(evidence[3], value)
    with pytest.raises(ReleaseError, match="release gate failed"):
        release_candidate(
            site="demo", profile="immersive", site_path=site, seed_db=seed, runtime_db=runtime,
            verification_paths=evidence, output_dir=tmp_path / "run/release", freeze_path=freeze,
        )
    result = json.loads((tmp_path / "run/release/release-gate-result.json").read_text())
    assert result["status"] == "failed"
    assert result["reason_codes"]
    assert not (tmp_path / "run/release/release-manifest.json").exists()


def test_release_rejects_duplicate_gate_and_changed_freeze_configuration(tmp_path: Path):
    site, seed, runtime, freeze, evidence, identity = prepare(tmp_path)
    with pytest.raises(ReleaseError, match="release gate failed"):
        release_candidate(
            site="demo", profile="immersive", site_path=site, seed_db=seed, runtime_db=runtime,
            verification_paths=[*evidence, evidence[0]], output_dir=tmp_path / "run/release", freeze_path=freeze,
        )
    record = json.loads(freeze.read_text())
    record["identity"]["configuration"]["profile"] = "task-driven"
    inputs = {
        "tree_sha256": identity["tree"]["sha256"],
        "contract_sha256s": [item["sha256"] for item in identity["contracts"]],
        "configuration": record["identity"]["configuration"],
    }
    record["identity"]["sha256"] = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write(freeze, record)
    with pytest.raises(ReleaseError, match="release gate failed"):
        release_candidate(
            site="demo", profile="immersive", site_path=site, seed_db=seed, runtime_db=runtime,
            verification_paths=evidence, output_dir=tmp_path / "run/release", freeze_path=freeze,
        )


def test_release_rejects_changed_contract_bytes(tmp_path: Path):
    site, seed, runtime, freeze, evidence, _identity = prepare(tmp_path)
    contract = tmp_path / "run/contract/site-contract.json"
    contract.write_text('{"schema":"contract-v1","routes":["/changed"]}', encoding="utf-8")
    with pytest.raises(ReleaseError, match="contract_changed"):
        release_candidate(
            site="demo", profile="immersive", site_path=site, seed_db=seed, runtime_db=runtime,
            verification_paths=evidence, output_dir=tmp_path / "run/release", freeze_path=freeze,
        )


def test_candidate_mutation_invalidates_frozen_release(tmp_path: Path):
    site, seed, runtime, freeze, evidence, _identity = prepare(tmp_path)
    (site / "app.py").write_text("# changed after verification\n", encoding="utf-8")
    with pytest.raises(ReleaseError):
        release_candidate(
            site="demo", profile="immersive", site_path=site, seed_db=seed, runtime_db=runtime,
            verification_paths=evidence, output_dir=tmp_path / "run/release", freeze_path=freeze,
        )
    result = json.loads((tmp_path / "run/release/release-gate-result.json").read_text())
    assert "candidate_mutated" in {item["code"] for item in result["reason_codes"]}


def test_explicit_repair_invalidates_downstream_evidence_before_writes(tmp_path: Path):
    site, seed, runtime, freeze, evidence, identity = prepare(tmp_path)
    invalidated = invalidate_downstream_evidence(tmp_path / "run", reason="repair-r1")
    assert "verification/fidelity.json" in invalidated
    assert json.loads(evidence[0].read_text())["status"] == "invalidated"
