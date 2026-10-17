from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from contract import (  # noqa: E402
    canonical_control_identity,
    canonical_control_identity_key,
    control_identity_compatible,
)
from inventory import TemplateInventory, canonical_control_families  # noqa: E402


def _controls(markup: str):
    parser = TemplateInventory("templates/page.html", markup)
    parser.feed(markup)
    parser.close()
    for item in parser.controls:
        item["source_routes"] = ["/"]
        item["active_surface"] = True
    return parser.controls


def test_literal_anchor_and_form_get_identity_selectors_with_css_escaping():
    controls = _controls(
        '<a href="/fr/">Français</a>'
        '<a href="/search?q=a&amp;b=2">Search</a>'
        '<form action="/submit?x=1&amp;y=2"><button>Send</button></form>'
    )
    assert controls[0]["selector"] == 'a[href="/fr/"]'
    assert controls[1]["selector"] == 'a[href="/search?q=a&b=2"]'
    assert controls[2]["selector"] == 'form[action="/submit?x=1&y=2"]'
    assert all(item["identity_selector"] == item["selector"] for item in controls)


def test_dynamic_jinja_href_stays_unresolved_and_has_no_selector():
    controls = _controls('<a href="{{ destination }}">Dynamic</a>')
    assert controls[0]["selector"] is None
    assert controls[0]["identity_selector"] is None


def test_query_parameter_normalization_and_runtime_selector_are_not_identity():
    source = {
        "source_path": "/",
        "tag": "a",
        "control_type": "link",
        "accessible_name": "Search",
        "href": "/search?b=2&a=1",
        "selector": 'a[href="/search?b=2&a=1"]',
    }
    browser = {
        "source_path": "/",
        "tag": "a",
        "control_type": "link",
        "accessible_name": " search ",
        "normalized_target": "/search?a=1&b=2",
        "selector": '[data-webharbor-audit-id="7"]',
        "runtime_selector": '[data-webharbor-audit-id="7"]',
    }
    assert canonical_control_identity(source)["normalized_target"] == "/search?a=1&b=2"
    assert canonical_control_identity_key(source) == canonical_control_identity_key(browser)
    assert control_identity_compatible(source, browser)


def test_duplicate_target_links_are_separate_by_name_but_true_duplicates_are_ambiguous():
    different_names = canonical_control_families(_controls(
        '<a href="/fr/">Français</a><a href="/fr/">French</a>'
    ))
    assert len(different_names) == 2
    same_name = canonical_control_families(_controls(
        '<a href="/fr/">French</a><a href="/fr/">French</a>'
    ))
    assert len(same_name) == 1
    assert same_name[0]["raw_observation_count"] == 2
