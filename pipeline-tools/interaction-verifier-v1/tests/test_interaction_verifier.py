from __future__ import annotations

import json
import sys
from pathlib import Path

COMPONENT = Path(__file__).parents[1]
sys.path.insert(0, str(COMPONENT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

import browser_verifier
import contract
import inventory
import pipeline_adapter
from fixture import prepare_workspace, running_fixture


def run_verifier(
    root: Path,
    base_url: str,
    *,
    mode: str = "passing",
    output_name: str = "evidence",
    max_pages: int | None = None,
):
    contract_path = prepare_workspace(root, mode=mode)
    return browser_verifier.verify(
        workspace=root,
        site="demo",
        base_url=base_url,
        contract_path=contract_path,
        evidence_dir=root / output_name,
        max_pages=max_pages,
        timeout_ms=5000,
    )


def failure_codes(closure: dict) -> set[str]:
    return {item["code"] for item in closure["failures"]}


def interaction_diff(root: Path, output_name: str = "evidence") -> dict:
    return json.loads((root / output_name / "interaction-diff.json").read_text())


def test_gateway_prefix_rewriting_preserves_logical_query():
    surface = contract.LocalSurface.from_base_url(
        "https://gateway.example.test/p/43123/"
    )
    assert surface.gateway_prefix == "/p/43123"
    assert surface.to_url("/search?q=hello") == (
        "https://gateway.example.test/p/43123/search?q=hello"
    )
    assert surface.logical_path(
        "https://gateway.example.test/p/43123/search?q=hello"
    ) == "/search?q=hello"
    assert surface.logical_path("https://gateway.example.test/search?q=hello") is None


def test_real_browser_accepts_menu_query_popup_and_local_state_deterministically(tmp_path: Path):
    with running_fixture() as base_url:
        _contract, closure, repair = run_verifier(tmp_path, base_url, output_name="first")
        assert closure["status"] == "passed"
        assert repair["status"] == "empty"
        assert closure["measured_rates"] == {
            "active_control_resolution_percent": 100.0,
            "critical_workflow_semantics_percent": 100.0,
        }
        menu_target = next(
            item for item in closure["control_inventory"]
            if item["accessible_name"] == "Featured detail"
        )
        assert menu_target["revealed_by"] == ["#menu"]
        query_proofs = next(
            item for item in closure["control_outcomes"]
            if item["id"] == "search-form"
        )["proof_steps"]
        assert any(step.get("actual_path") == "/search?q=hello" for step in query_proofs)
        popup = next(item for item in closure["control_outcomes"] if item["id"] == "popup-reader")
        assert popup["observed_outcome"] == "popup"
        local_state = next(item for item in closure["control_outcomes"] if item["id"] == "local-state")
        assert local_state["observed_outcome"] == "in_page_state_change"

        first_bytes = (tmp_path / "first" / "interaction-closure.json").read_bytes()
        first_inventory = (tmp_path / "first" / "code-interaction-inventory.json").read_bytes()
        first_diff = (tmp_path / "first" / "interaction-diff.json").read_bytes()
        _contract, second, _repair = browser_verifier.verify(
            workspace=tmp_path,
            site="demo",
            base_url=base_url,
            contract_path=tmp_path / "sites/demo/interaction_contract.json",
            evidence_dir=tmp_path / "first",
            timeout_ms=5000,
        )
        assert second == closure
        assert (tmp_path / "first" / "interaction-closure.json").read_bytes() == first_bytes
        assert (tmp_path / "first" / "code-interaction-inventory.json").read_bytes() == first_inventory
        assert (tmp_path / "first" / "interaction-diff.json").read_bytes() == first_diff
        assert closure["raw_counts"]["triangulation_hard_gaps"] == 0
        assert closure["candidate_fingerprint"]["analyzer"] == inventory.ANALYZER_TOOL


def test_real_browser_gateway_forward_path_passes(tmp_path: Path):
    with running_fixture(gateway=True) as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url)
    assert closure["status"] == "passed"
    assert closure["surface"]["gateway_prefix"] == "/p/43123"
    assert closure["raw_counts"]["triangulation_hard_gaps"] == 0
    assert all(
        item["actual_path"] in {"/", "/catalog", "/detail", "/search?q=hello", "/popup"}
        for item in closure["page_archetypes"]
    )


def test_dead_link_is_rejected(tmp_path: Path):
    with running_fixture(mode="dead") as base_url:
        _contract, closure, repair = run_verifier(tmp_path, base_url, mode="dead")
    assert closure["status"] == "failed"
    assert "dead_destination" in failure_codes(closure)
    assert repair["status"] == "repair_required"


def test_inert_button_is_rejected(tmp_path: Path):
    with running_fixture(mode="inert") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="inert")
    assert closure["status"] == "failed"
    assert "control_outcome_mismatch" in failure_codes(closure)


def test_defensive_page_is_rejected(tmp_path: Path):
    with running_fixture(mode="defensive") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="defensive")
    assert closure["status"] == "failed"
    assert "defensive_page" in failure_codes(closure)
    assert closure["raw_counts"]["defensive_pages"] >= 1


def test_generic_fallback_is_rejected(tmp_path: Path):
    with running_fixture(mode="generic") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="generic")
    assert closure["status"] == "failed"
    assert "generic_fallback_duplicate" in failure_codes(closure)


def test_unexpected_external_request_is_blocked_and_rejected(tmp_path: Path):
    with running_fixture(mode="external") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="external")
    assert closure["status"] == "failed"
    assert "unexpected_network" in failure_codes(closure)
    assert any(
        item["kind"] == "unexpected_external_request"
        for item in closure["unexpected_requests"]
    )


def test_unexpected_upstream_navigation_is_blocked_and_rejected(tmp_path: Path):
    with running_fixture(mode="upstream_nav") as base_url:
        _contract, closure, _repair = run_verifier(
            tmp_path,
            base_url,
            mode="upstream_nav",
        )
    assert closure["status"] == "failed"
    assert any(
        item["kind"] == "unexpected_upstream_navigation"
        for item in closure["unexpected_requests"]
    )


def test_budget_exhaustion_fails_as_unaudited_limit(tmp_path: Path):
    with running_fixture() as base_url:
        _contract, closure, _repair = run_verifier(
            tmp_path,
            base_url,
            max_pages=1,
        )
    assert closure["status"] == "failed"
    assert "unaudited_limit" in failure_codes(closure)
    assert closure["crawl"]["inventory_complete"] is False


def test_candidate_fingerprint_invalidates_after_site_change(tmp_path: Path):
    contract_path = prepare_workspace(tmp_path)
    contract_data = contract.load_contract(contract_path, expected_site="demo")
    site = tmp_path / "sites/demo"
    first = contract.candidate_fingerprint(
        site,
        contract_data,
        code_inventory_sha256="1" * 64,
        interaction_diff_sha256="2" * 64,
        analyzer=inventory.ANALYZER_TOOL,
    )
    closure = {
        "closure": contract.CLOSURE_SCHEMA,
        "tool": contract.VERIFIER_TOOL,
        "status": "passed",
        "candidate_fingerprint": first,
        "code_inventory": {
            "payload_sha256": "1" * 64,
            "analyzer": inventory.ANALYZER_TOOL,
        },
        "interaction_diff": {"payload_sha256": "2" * 64},
    }
    assert contract.closure_is_current(closure, site, contract_data)
    (site / "app.py").write_text("# changed implementation\n", encoding="utf-8")
    assert not contract.closure_is_current(closure, site, contract_data)
    changed = contract.candidate_fingerprint(
        site,
        contract_data,
        code_inventory_sha256="1" * 64,
        interaction_diff_sha256="2" * 64,
        analyzer=inventory.ANALYZER_TOOL,
    )
    assert changed != first


def test_candidate_fingerprint_invalidates_after_analyzer_or_diff_change(tmp_path: Path):
    contract_path = prepare_workspace(tmp_path)
    contract_data = contract.load_contract(contract_path, expected_site="demo")
    site = tmp_path / "sites/demo"
    first = contract.candidate_fingerprint(
        site,
        contract_data,
        code_inventory_sha256="1" * 64,
        interaction_diff_sha256="2" * 64,
        analyzer=inventory.ANALYZER_TOOL,
    )
    analyzer_changed = contract.candidate_fingerprint(
        site,
        contract_data,
        code_inventory_sha256="1" * 64,
        interaction_diff_sha256="2" * 64,
        analyzer="webharbor-immersive-code-analyzer-v2",
    )
    diff_changed = contract.candidate_fingerprint(
        site,
        contract_data,
        code_inventory_sha256="1" * 64,
        interaction_diff_sha256="3" * 64,
        analyzer=inventory.ANALYZER_TOOL,
    )
    assert first != analyzer_changed
    assert first != diff_changed


def test_flask_public_get_route_omitted_from_contract_is_rejected(tmp_path: Path):
    with running_fixture(mode="orphan_route") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="orphan_route")
    assert closure["status"] == "failed"
    diff = interaction_diff(tmp_path)
    gaps = diff["gap_categories"]["actual_public_get_route_omitted_from_declared_or_reachable_graph"]
    assert any(item["route"] == "/orphan" for item in gaps)


def test_rendered_local_link_without_actual_flask_route_is_rejected(tmp_path: Path):
    with running_fixture(mode="missing_route") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="missing_route")
    assert closure["status"] == "failed"
    gaps = interaction_diff(tmp_path)["gap_categories"]["local_target_without_actual_route"]
    assert any(item.get("evidence", {}).get("target") == "/ghost" for item in gaps)


def test_source_control_removed_from_rendered_dom_is_rejected(tmp_path: Path):
    with running_fixture(mode="source_removed") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="source_removed")
    assert closure["status"] == "failed"
    gaps = interaction_diff(tmp_path)["gap_categories"]["source_control_hidden_disabled_unhit_testable_or_absent"]
    removed = next(item for item in gaps if item["selector"] == "#removed")
    assert removed["source_location"]["file"] == "templates/home.html"
    assert removed["evidence"]["states"][0]["count"] == 0


def test_active_source_control_omitted_from_contract_is_rejected(tmp_path: Path):
    with running_fixture(mode="source_omitted") as base_url:
        _contract, closure, repair = run_verifier(tmp_path, base_url, mode="source_omitted")
    assert closure["status"] == "failed"
    gaps = interaction_diff(tmp_path)["gap_categories"]["source_active_control_missing_from_contract"]
    source_only = next(item for item in gaps if item["selector"] == "#source-only")
    assert source_only["source_location"]["line"] > 0
    assert any(
        item.get("source_location", {}).get("file") == "templates/home.html"
        for group in repair["groups"] for item in group["items"]
        if item["code"] == "source_active_control_missing_from_contract"
    )


def test_browser_only_injected_control_without_source_or_contract_is_rejected(tmp_path: Path):
    with running_fixture(mode="browser_injected") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="browser_injected")
    assert closure["status"] == "failed"
    assert closure["raw_counts"]["source_declared_controls"] < closure["raw_counts"]["declared_controls"]
    assert closure["raw_counts"]["derived_control_contracts"] >= 1
    assert closure["raw_counts"]["derived_declared_controls"] >= 1
    assert "derived_control_unproved" in failure_codes(closure)
    assert any(item.get("derived") for item in closure["control_outcomes"])
    gaps = interaction_diff(tmp_path)["gap_categories"]["rendered_active_control_missing_from_source_or_contract"]
    assert any(item["selector"] == "#injected" for item in gaps)
    diff = interaction_diff(tmp_path)
    assert diff["contract_identity"]["derived_controls"] >= 1


def test_javascript_external_navigation_is_source_rejected(tmp_path: Path):
    with running_fixture(mode="js_external") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="js_external")
    assert closure["status"] == "failed"
    gaps = interaction_diff(tmp_path)["gap_categories"]["unexpected_external_target"]
    assert any(
        item.get("evidence", {}).get("target") == "https://outside.example.invalid/path"
        and item["source_location"]["syntax_kind"].endswith("location_navigation")
        for item in gaps
    )


def test_dynamic_jinja_target_without_declaration_and_proof_fails_closed(tmp_path: Path):
    with running_fixture(mode="dynamic_target") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="dynamic_target")
    assert closure["status"] == "failed"
    gaps = interaction_diff(tmp_path)["gap_categories"]["unresolved_dynamic_action_or_target"]
    assert any(item["selector"] == "#dynamic" for item in gaps)


def test_dynamic_jinja_target_with_exact_contract_and_browser_proof_is_closed(tmp_path: Path):
    with running_fixture(mode="dynamic_proved") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="dynamic_proved")
    assert closure["status"] == "passed"
    diff = interaction_diff(tmp_path)
    assert diff["hard_gap_count"] == 0
    assert any(
        item["control_ids"] == ["catalog-card"]
        for item in diff["joins"]["proved_dynamic_targets"]
    )


def test_source_contract_query_mismatch_is_rejected(tmp_path: Path):
    with running_fixture(mode="query_mismatch") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="query_mismatch")
    assert closure["status"] == "failed"
    gaps = interaction_diff(tmp_path)["gap_categories"]["code_browser_route_query_or_method_mismatch"]
    assert any(item["selector"] == "#catalog" for item in gaps)


def test_source_route_method_mismatch_is_rejected(tmp_path: Path):
    with running_fixture(mode="method_mismatch") as base_url:
        _contract, closure, _repair = run_verifier(tmp_path, base_url, mode="method_mismatch")
    assert closure["status"] == "failed"
    gaps = interaction_diff(tmp_path)["gap_categories"]["code_browser_route_query_or_method_mismatch"]
    assert any(item.get("evidence", {}).get("expected_method") == "POST" for item in gaps)


def test_isolated_route_map_leaves_source_and_seed_hashes_unchanged(tmp_path: Path):
    prepare_workspace(tmp_path)
    site = tmp_path / "sites/demo"
    app_before = contract.sha256_file(site / "app.py")
    inventory_result = inventory.inventory_site(site, site="demo", timeout_seconds=10)
    assert inventory_result["actual_route_map"]["status"] == "passed"
    assert inventory_result["actual_route_map"]["source_hashes"]["unchanged"] is True
    assert inventory_result["actual_route_map"]["seed_hashes"]["unchanged"] is True
    assert inventory_result["actual_route_map"]["isolated_copy_hashes"]["unchanged"] is True
    assert contract.sha256_file(site / "app.py") == app_before


def test_code_inventory_records_python_jinja_html_and_javascript_kinds(tmp_path: Path):
    prepare_workspace(tmp_path)
    site = tmp_path / "sites/demo"
    (site / "syntax_samples.py").write_text(
        """from flask import Blueprint, redirect, url_for
bp = Blueprint('sample', __name__)
@bp.route('/sample', methods=['GET', 'POST'])
def sample():
    return redirect(url_for('home'))
""",
        encoding="utf-8",
    )
    (site / "templates/include.html").write_text(
        "<button id='included' role='button' data-action='toggle' aria-controls='panel'>Toggle</button>",
        encoding="utf-8",
    )
    (site / "templates/child.html").write_text(
        "{% extends 'home.html' %}{% include 'include.html' %}",
        encoding="utf-8",
    )
    scripts = site / "static"
    scripts.mkdir()
    (scripts / "sample.js").write_text(
        """document.addEventListener('click', event => {
  const target = event.target.closest('#delegated');
  if (target) fetch('/api/items', {method: 'POST'});
});
window.open('/popup');
""",
        encoding="utf-8",
    )
    inventory_result = inventory.inventory_site(site, site="demo", timeout_seconds=10)
    kinds = {
        item["syntax_kind"]
        for field in ("server_declarations", "template_relationships", "controls", "interactions")
        for item in inventory_result[field]
    }
    assert "python_flask_route_decorator" in kinds
    assert "python_url_for" in kinds
    assert "python_redirect" in kinds
    assert "jinja_extends" in kinds
    assert "jinja_include" in kinds
    assert "html_button_control" in kinds
    assert "javascript_event_handler" in kinds
    assert "javascript_fetch" in kinds
    assert "javascript_window_open" in kinds


def test_repair_package_is_machine_grouped(tmp_path: Path):
    fingerprint = {"algorithm": "test", "sha256": "0" * 64}
    failures = [
        {"code": "dead_destination", "route": "/one", "route_family": "catalog", "page_archetype": "detail", "control_type": "link"},
        {"code": "empty_destination", "route": "/two", "route_family": "catalog", "page_archetype": "detail", "control_type": "link"},
    ]
    package = contract.build_repair_package(
        site="demo",
        fingerprint=fingerprint,
        failures=failures,
    )
    assert package["repair_package"] == "webharbor-builder-repair-package-v1"
    assert package["group_count"] == 1
    assert package["groups"][0]["failure_codes"] == [
        "dead_destination",
        "empty_destination",
    ]
    # Round-trip proves the package contains JSON data, not prose fragments.
    assert json.loads(json.dumps(package, sort_keys=True)) == package


def test_immersive_sdk_brief_consumes_repair_groups_without_changing_old_profiles():
    coverage = {
        "clickthrough": {
            "meaningful_local_percent": 75.0,
            "requested_min_percent": 100.0,
        },
        "unimplemented_links": [],
        "resource_issues": [],
        "builder_repair_package": {
            "schema_version": 1,
            "repair_package": "webharbor-builder-repair-package-v1",
            "candidate_fingerprint": {"sha256": "0" * 64},
            "failure_count": 1,
            "group_count": 1,
            "groups": [{
                "route_family": "catalog",
                "page_archetype": "detail",
                "control_type": "button",
                "failure_codes": ["control_outcome_mismatch"],
                "items": [{"code": "control_outcome_mismatch", "route": "/detail"}],
            }],
        },
    }
    immersive = pipeline_adapter.repair_brief(
        1,
        coverage,
        [],
        acceptance_profile="immersive",
    )
    assert "webharbor-builder-repair-package-v1" in immersive
    assert "exactly 100% active-control resolution" in immersive
    full_site = pipeline_adapter.repair_brief(1, coverage, [], acceptance_profile="full-site")
    reviewable = pipeline_adapter.repair_brief(
        1,
        coverage,
        [],
        acceptance_profile="reviewable-workflow",
    )
    assert "forced full-site interaction-closure loop" in full_site
    assert "exactly one source-grounded browser workflow" in reviewable
