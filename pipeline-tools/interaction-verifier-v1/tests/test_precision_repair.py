from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import contract
import inventory
import triangulation
from browser_verifier import canonicalize_edges


def _factory_site(root: Path) -> Path:
    site = root / "sites" / "factory"
    site.mkdir(parents=True)
    (site / "instance_seed").mkdir()
    (site / "wsgi.py").write_text(
        """from flask import Flask

def create_app(config_name=None):
    app = Flask(__name__)
    @app.get('/items/<int:item_id>/')
    def item(item_id):
        return f'item {item_id}'
    @app.get('/files/<path:name>')
    def file(name):
        return name
    return app
""",
        encoding="utf-8",
    )
    return site


def test_archived_external_cache_is_excluded_and_loaded_asset_is_included(tmp_path: Path):
    site = tmp_path / "sites" / "demo"
    (site / "templates").mkdir(parents=True)
    (site / "static").mkdir()
    (site / "static" / "external_cache" / "upstream").mkdir(parents=True)
    (site / "static" / "external_cache").mkdir(exist_ok=True)
    (site / "app.py").write_text(
        """from flask import Flask, render_template
app = Flask(__name__)
@app.get('/')
def home():
    return render_template('home.html')
""",
        encoding="utf-8",
    )
    (site / "templates" / "home.html").write_text(
        '<script src="/static/loaded.js"></script><a href="/">Home</a>',
        encoding="utf-8",
    )
    (site / "static" / "loaded.js").write_text("window.loaded = true;", encoding="utf-8")
    (site / "static" / "external_cache" / "unused.js").write_text("alert('unused')", encoding="utf-8")
    (site / "static" / "external_cache" / "upstream" / "page.html").write_text(
        "<button id='archived'>Archived control</button>", encoding="utf-8"
    )
    bundle = site / "static" / "external_cache" / "unused.json.gz"
    with gzip.open(bundle, "wb") as handle:
        handle.write(json.dumps({"pages": []}).encode("utf-8"))

    closure = inventory.source_closure(site)
    included = {item["path"]: item for item in closure["included"]}
    excluded = {item["path"]: item for item in closure["excluded"]}
    assert "static/external_cache/upstream/page.html" in excluded
    assert excluded["static/external_cache/upstream/page.html"]["reason"] == "archived_upstream_capture"
    assert excluded["static/external_cache/unused.json.gz"]["reason"] == "unreferenced_external_cache"
    assert included["static/loaded.js"]["reason"] == "actually_loaded_candidate_asset"

    result = inventory.inventory_site(site, site="demo", timeout_seconds=10)
    assert all("archived" not in item["file"] for item in result["controls"])
    assert "static/loaded.js" in {item["path"] for item in result["files"]}


def test_repeated_card_family_keeps_broken_member_separate():
    records = [
        {
            "source_path": "/catalog",
            "selector": "[data-webharbor-audit-id='1']",
            "control_type": "card",
            "accessible_name": "MacBook Pro",
            "href": "/macbook-pro/",
        },
        {
            "source_path": "/catalog",
            "selector": "[data-webharbor-audit-id='2']",
            "control_type": "card",
            "accessible_name": "MacBook Pro",
            "href": "/macbook-pro/",
        },
        {
            "source_path": "/catalog",
            "selector": "[data-webharbor-audit-id='3']",
            "control_type": "card",
            "accessible_name": "MacBook Pro",
            "href": "/missing/",
        },
    ]
    families = inventory.canonical_control_families(records, route_key="source_path")
    assert sorted(item["raw_observation_count"] for item in families) == [1, 2]
    assert len({tuple(item["unique_targets"]) for item in families}) == 2
    assert sum(len(item["members"]) for item in families) == 3


def test_repeated_crawl_states_are_deduplicated_with_raw_counts():
    edges = [
        {"from": "/", "to": "/detail/", "kind": "navigation", "control_type": "card", "selector": "[data-webharbor-audit-id='1']", "audit_id": "1"},
        {"from": "/", "to": "/detail", "kind": "navigation", "control_type": "card", "selector": "[data-webharbor-audit-id='9']", "audit_id": "9"},
    ]
    canonical = canonicalize_edges(edges)
    assert len(canonical) == 1
    assert canonical[0]["raw_observation_count"] == 2
    assert len(canonical[0]["raw_provenance"]) == 2


def test_trailing_slash_and_variable_flask_rules_match_browser_routes():
    assert triangulation.route_equivalent("/items/<int:item_id>/", "/items/42")
    assert triangulation.route_equivalent("/files/<path:name>", "/files/a/b/")
    assert triangulation.route_equivalent("/catalog/", "/catalog")


def test_factory_route_map_supports_wsgi_import_and_browser_reachable_variable_route(tmp_path: Path):
    site = _factory_site(tmp_path)
    result = inventory.inventory_site(site, site="factory", timeout_seconds=10)
    route_map = result["actual_route_map"]
    assert route_map["status"] == "passed"
    assert any(item["rule"] == "/items/<int:item_id>/" for item in route_map["routes"])
    actual = triangulation._actual_route("/items/42", route_map)
    assert actual is not None
    assert actual["endpoint"] == "item"


def test_run_local_contract_derivation_covers_all_families_and_archetypes():
    source_contract = {
        "route_inventory": [
            {"path": "/", "family": "entry", "archetype": "landing"},
            {"path": "/catalog/", "family": "catalog", "archetype": "listing"},
        ],
        "control_inventory": [],
        "critical_workflows": [{"id": "critical"}],
    }
    surface = contract.derive_site_contract_surface(
        source_contract,
        route_results=[
            {"requested_path": "/", "archetype": "landing", "passed": True},
            {"requested_path": "/catalog", "archetype": "listing", "passed": True},
        ],
        control_families=[
            {
                "family_id": "control-family-a",
                "route": "/catalog",
                "control_type": "card",
                "accessible_name": "Good",
                "raw_observation_count": 4,
                "unique_targets": ["/detail"],
                "representative": {"selector": ".card"},
            }
        ],
    )
    assert surface["coverage"]["route_family_count"] == 2
    assert surface["coverage"]["control_family_count"] == 1
    assert {item["path"] for item in surface["route_archetypes"]} == {"/", "/catalog"}


def test_derived_browser_families_are_explicit_fail_closed_contracts():
    source_contract = {
        "control_inventory": [
            {
                "id": "declared",
                "source_path": "/",
                "selector": "#declared",
            }
        ]
    }
    families = [
        {
            "family_id": "control-family-declared",
            "route": "/",
            "raw_observation_count": 2,
            "raw_provenance": [{"id": "audit-1"}, {"id": "audit-2"}],
            "representative": {
                "source_path": "/",
                "selector": "#declared",
                "accessible_name": "Declared",
                "control_type": "link",
                "href": "/detail",
            },
        },
        {
            "family_id": "control-family-unproved",
            "route": "/",
            "raw_observation_count": 1,
            "representative": {
                "source_path": "/",
                "selector": "#unproved",
                "accessible_name": "Unproved",
                "control_type": "button",
            },
        },
    ]
    derived = contract.derive_browser_control_contracts(
        source_contract,
        families,
        [{"from": "/", "to": "/detail", "selector": "#declared", "kind": "navigation"}],
    )
    assert len(derived) == 2
    declared, unproved = derived
    assert declared["source_basis_ids"] == ["declared"]
    assert declared["outcome"] == {"kind": "navigation", "expected_path": "/detail"}
    assert unproved["source_basis_ids"] == []
    assert unproved["outcome"]["kind"] == "state_change"
    assert unproved["outcome"]["assertions"][0]["name"] == "data-webharbor-derived-proof"
