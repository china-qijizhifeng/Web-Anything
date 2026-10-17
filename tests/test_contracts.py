from __future__ import annotations

from webanything.contracts import ContractCompileError, compile_contract


def discovery():
    provenance = {
        "kind": "captured",
        "sources": [{"path": "evidence/upstream.json", "sha256": "1" * 64}],
    }
    fields = {
        "title": {"selector": "h1", "min_chars": 1},
        "context": {"selector": "body", "min_chars": 1},
        "main": {"selector": "main", "min_chars": 1},
        "return": {"selector": "a[href='/']", "expected_path": "/", "min_chars": 1},
    }
    return {
        "schema": "webanything-discovery-package-v1",
        "source": {"entry_url": "https://example.com/", "allowed_origins": ["https://example.com"]},
        "captured_at": "2026-08-27T00:00:00Z",
        "provenance": provenance,
        "pages": [
            {"url": "https://example.com/", "title": "Home", "archetype": "landing", "minimum_fields": fields, "provenance": provenance},
            {"url": "https://example.com/search?q=test", "title": "Search", "archetype": "search_results", "minimum_fields": fields, "provenance": provenance},
        ],
        "workflows": [{"id": "search", "routes": ["/", "/search?q=test"], "controls": ["search-link"]}],
        "controls": [{
            "id": "search-link", "route": "/", "type": "link", "label": "Search",
            "selector": "a[href='/search?q=test']", "outcome": {"kind": "navigation", "expected_path": "/search?q=test"},
        }],
        "resources": [],
        "limitations": [],
    }


def test_compile_contract_preserves_routes_and_profile():
    contract = compile_contract(
        discovery(), site="example", profile="immersive", viewport_width=1440, viewport_height=1000
    )
    assert [item["route"] for item in contract["routes"]] == ["/", "/search?q=test"]
    assert contract["release_gates"]["active_control_resolution_percent"] == 100
    assert contract["interaction_contract"]["controls"][0]["id"] == "search-link"
    assert contract["control_inventory"][0]["outcome"] == {
        "kind": "navigation", "expected_path": "/search?q=test"
    }
    assert contract["namespaces"][0]["provenance"]["sources"][0]["sha256"] == "1" * 64


def test_compile_contract_requires_homepage():
    payload = discovery()
    payload["pages"] = [{**payload["pages"][0], "url": "https://example.com/other"}]
    try:
        compile_contract(payload, site="example", profile="task-driven", viewport_width=1440, viewport_height=1000)
    except ContractCompileError as exc:
        assert "entry route" in str(exc)
    else:
        raise AssertionError("missing homepage was accepted")
