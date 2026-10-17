from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_run_examples_validate_against_public_schema():
    schema = json.loads((ROOT / "schemas/run-config-v1.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    for path in sorted((ROOT / "examples").glob("*.run.json")):
        validator.validate(json.loads(path.read_text(encoding="utf-8")))


def test_skill_frontmatter_and_progressive_disclosure():
    path = ROOT / ".agents/skills/build-webharbor-mirrors/SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == "build-webharbor-mirrors"
    assert "isolated workspace" in metadata["description"]
    assert len(body.splitlines()) <= 150
    for reference in (
        "automation.md",
        "evidence-contract.md",
        "full-site-completeness.md",
        "interaction-closure-loop.md",
        "local-authenticated-state.md",
    ):
        assert reference in body
        assert (path.parent / "references" / reference).is_file()


def test_synthetic_skill_is_bounded_and_references_immutable_contract_rules():
    path = ROOT / ".agents/skills/build-synthetic-benchmarks/SKILL.md"
    text = path.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == "build-synthetic-benchmarks"
    assert "benchmark_synthetic" in metadata["description"]
    assert len(body.splitlines()) <= 150
    assert ".webanything/synthetic-input" in body
    assert "contract-runtime-and-evidence.md" in body
    assert "visual-reference-and-repair.md" in body
    assert (path.parent / "references/contract-runtime-and-evidence.md").is_file()
    assert (path.parent / "references/visual-reference-and-repair.md").is_file()
    agent = yaml.safe_load((path.parent / "agents/openai.yaml").read_text(encoding="utf-8"))
    assert "$build-synthetic-benchmarks" in agent["interface"]["default_prompt"]


def test_synthetic_replay_skill_declares_sqlite_auth_reset_and_isolation():
    path = ROOT / ".agents/skills/build-synthetic-replays/SKILL.md"
    text = path.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == "build-synthetic-replays"
    assert "benchmark_synthetic_replay" in metadata["description"]
    assert len(body.splitlines()) <= 150
    for phrase in (
        "SQLite", "local synthetic login", "byte-identical", "session_partitioned",
        "data-action-id", "/p/40000", "capture_bundle_sha256", "seed_sha256",
        "candidate_files", "HOST", "PORT",
        "Never use broad `pkill -f`",
        "Never call `verify_synthetic_replay_candidate`",
    ):
        assert phrase in body
    for reference in ("runtime-state-and-reset.md", "capture-and-release-boundaries.md"):
        assert reference in body
        assert (path.parent / "references" / reference).is_file()
    agent = yaml.safe_load((path.parent / "agents/openai.yaml").read_text(encoding="utf-8"))
    assert "$build-synthetic-replays" in agent["interface"]["default_prompt"]


def test_release_v2_schema_requires_passing_nested_gates():
    schema = json.loads((ROOT / "schemas/release-manifest-v2.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({
            "schema": "webanything-release-manifest-v2", "site": "demo", "profile": "immersive",
            "candidate_sha256": "a" * 64, "candidate_identity": {"sha256": "b" * 64, "tree": {}},
            "seed_reset": {}, "privacy_scan": {}, "verification": {},
            "release_gate": {"path": "release-gate-result.json", "sha256": "c" * 64},
            "deployment_binding": {"algorithm": "webanything-site-tree-v1", "candidate_sha256": "a" * 64, "candidate_identity_sha256": "b" * 64},
            "release_status": "frozen",
        })


def test_synthetic_visual_schemas_and_example_capture_spec_are_valid():
    capture_schema = json.loads((ROOT / "schemas/synthetic-visual-capture-spec-v1.schema.json").read_text())
    capture = json.loads((ROOT / "examples/synthetic/cloudpost/visual-capture-spec.local-test.json").read_text())
    jsonschema.Draft202012Validator(capture_schema, format_checker=jsonschema.FormatChecker()).validate(capture)
    reference_schema = json.loads((ROOT / "schemas/synthetic-visual-reference-v1.schema.json").read_text())
    reference = json.loads((ROOT / "examples/synthetic/cloudpost/contract/visual-reference/synthetic_visual_reference.json").read_text())
    jsonschema.Draft202012Validator(reference_schema, format_checker=jsonschema.FormatChecker()).validate(reference)
    verification_schema = json.loads((ROOT / "schemas/synthetic-visual-verification-v1.schema.json").read_text())
    jsonschema.Draft202012Validator(verification_schema).validate({
        "schema": "webanything-synthetic-visual-verification-v1",
        "status": "failed",
        "repairable": False,
        "site": "cloudpost",
        "build_mode": "benchmark_synthetic",
        "comparison_mode": "content_neutral",
        "contract_sha256": "a" * 64,
        "reference_manifest_sha256": "b" * 64,
        "visual_reference_sha256": "c" * 64,
        "candidate_identity_sha256": None,
        "candidate_sha256": "d" * 64,
        "candidate": {"pre_hash": {}, "post_hash": {}, "unchanged": False},
        "routes": [],
        "network": {"unexpected_external_requests": 1, "urls": ["https://outside.example/"]},
        "failures": [{"code": "visual_verification_error"}],
    })
    pipeline_schema = json.loads((ROOT / "schemas/synthetic-pipeline-spec-v1.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(pipeline_schema)


def test_url_first_replay_acquisition_schema_and_visual_privacy_extensions():
    acquisition_schema = json.loads((ROOT / "schemas/replay-acquisition-v1.schema.json").read_text())
    jsonschema.Draft202012Validator(acquisition_schema, format_checker=jsonschema.FormatChecker()).validate({
        "schema": "webanything-replay-acquisition-v1",
        "status": "completed",
        "site": "demo_replay",
        "seed_url": "https://example.test/",
        "source_origin": "https://example.test",
        "routes": 2,
        "actions": 5,
        "assets": 8,
        "asset_bytes": 1024,
        "tasks": 8,
        "skipped_discovered_routes": 2,
        "product_profile": "generic_web_app_v1",
        "coverage": {
            "profile": "generic_web_app_v1",
            "release_tier": "smoke",
            "required_route_kinds": ["home"],
            "required_workflows": ["login", "logout", "reset", "stateful_action"],
            "generic_fallback_allowed": False,
        },
        "auth_used": False,
        "source_auth_persisted": False,
        "private_fields_removed": 1,
        "capture_bundle_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "contract_path": "synthetic_replay_contract.json",
    })
    capture_schema = json.loads((ROOT / "schemas/synthetic-visual-capture-spec-v1.schema.json").read_text())
    capture = json.loads((ROOT / "examples/synthetic/cloudpost/visual-capture-spec.local-test.json").read_text())
    capture["routes"][0]["private_selectors"] = [{"selector": ".account", "replacement": "Demo User"}]
    capture["routes"][0]["allow_quality_failures"] = ["landmarks_missing"]
    jsonschema.Draft202012Validator(capture_schema, format_checker=jsonschema.FormatChecker()).validate(capture)
