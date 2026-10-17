from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import jsonschema

from webanything.discovery import DiscoveryError, canonical_url, emit_contract_artifacts


ROOT = Path(__file__).parents[1]
VERIFIER_SCRIPTS = ROOT / "pipeline-tools/interaction-verifier-v1/scripts"
if str(VERIFIER_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(VERIFIER_SCRIPTS))

from contract import load_contract  # noqa: E402


HOME = """<!doctype html><html><head><title>Example</title></head><body>
<h1><a href="/">Example</a></h1><main><p>Captured home content.</p>
<a href="/detail/">Read detail</a><a href="/login">Login</a></main></body></html>"""
DETAIL = """<!doctype html><html><head><title>Detail</title></head><body>
<h1><a href="/">Example</a></h1><main><p>Captured detail content.</p></main></body></html>"""


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    site = workspace / "sites/example"
    raw = site / "static/external_cache/upstream/pages"
    evidence = workspace / "mirror_factory/evidence/example"
    raw.mkdir(parents=True)
    evidence.mkdir(parents=True)
    (raw / "home.html").write_text(HOME, encoding="utf-8")
    (raw / "detail.html").write_text(DETAIL, encoding="utf-8")
    upstream = {
        "generated_at": "2026-08-27T00:00:00Z",
        "scope": {"upstream_url": "https://example.com/"},
        "pages": [
            {"url": "https://example.com/", "final_url": "https://example.com/", "local_path": "pages/home.html", "status": 200, "sha256": _sha(HOME)},
            {"url": "https://example.com/detail/", "final_url": "http://example.com/detail/", "local_path": "pages/detail.html", "status": 200, "sha256": _sha(DETAIL)},
        ],
        "assets": [], "failures": [], "asset_failures": [],
    }
    (evidence / "upstream.json").write_text(json.dumps(upstream), encoding="utf-8")
    (site / "mirror_contract.json").write_text(json.dumps({
        "schema_version": 1,
        "interaction": {"blocking_gaps": [], "disclosed_nonblocking_gaps": []},
    }), encoding="utf-8")
    return workspace


def test_emitter_uses_raw_capture_and_is_deterministic(tmp_path: Path):
    workspace = _workspace(tmp_path)
    discovery_output = tmp_path / "run/discovery/discovery-package.json"
    contract_output = tmp_path / "run/contract/site-contract.json"
    first = emit_contract_artifacts(
        workspace,
        site="example",
        entry_url="https://example.com/",
        profile="task-driven",
        discovery_output=discovery_output,
        contract_output=contract_output,
    )
    first_hashes = (first["discovery_package"]["sha256"], first["site_contract"]["sha256"])
    second = emit_contract_artifacts(
        workspace,
        site="example",
        entry_url="https://example.com/",
        profile="task-driven",
        discovery_output=discovery_output,
        contract_output=contract_output,
    )
    assert first_hashes == (second["discovery_package"]["sha256"], second["site_contract"]["sha256"])

    discovery = json.loads(discovery_output.read_text())
    contract_payload = json.loads(contract_output.read_text())
    jsonschema.Draft202012Validator(
        json.loads((ROOT / "schemas/discovery-package-v1.schema.json").read_text()),
        format_checker=jsonschema.FormatChecker(),
    ).validate(discovery)
    jsonschema.Draft202012Validator(
        json.loads((ROOT / "schemas/site-contract-v1.schema.json").read_text()),
        format_checker=jsonschema.FormatChecker(),
    ).validate(contract_payload)
    assert [page["url"] for page in discovery["pages"]] == [
        "https://example.com/", "https://example.com/detail/"
    ]
    assert all(
        source["path"].startswith("sites/example/static/external_cache/upstream/pages/")
        for page in discovery["pages"]
        for source in page["provenance"]["sources"]
    )
    assert not any("app.py" in json.dumps(page) for page in discovery["pages"])
    assert {item["expected_path"] for item in discovery["controls"] if item["required"]} == {"/", "/detail/"}

    contract = load_contract(contract_output, expected_site="example")
    assert [item["path"] for item in contract["route_inventory"]] == ["/", "/detail/"]
    assert contract["critical_workflows"]


def test_scheme_normalization_rejects_cross_origin():
    assert canonical_url("https://example.com/", "http://example.com/detail") == "https://example.com/detail"
    with pytest.raises(DiscoveryError, match="canonical host"):
        canonical_url("https://example.com/", "https://other.example/detail")


def test_snapshot_task_url_is_pinned_to_canonical_scheme():
    path = ROOT / ".agents/skills/build-webharbor-mirrors/scripts/compile_snapshot.py"
    scripts = path.parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("webanything_compile_snapshot_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.canonical_same_host_url(
        "https://example.com/", "http://example.com/detail/?b=2"
    ) == "https://example.com/detail/?b=2"
    with pytest.raises(module.CompileError, match="canonical host"):
        module.canonical_same_host_url("https://example.com/", "https://other.example/")
