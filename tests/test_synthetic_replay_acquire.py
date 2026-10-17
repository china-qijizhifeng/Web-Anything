from __future__ import annotations

import json
import shutil
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

import webanything.cli as cli
import webanything.synthetic_visual as synthetic_visual
from webanything.synthetic_replay_acquire import (
    COMMERCE_PROFILE,
    CONTENT_PROFILE,
    STATIC_LANDING_PROFILE,
    TRAVEL_PROFILE,
    VIDEO_PROFILE,
    CAPTURE_BROWSER_ARGS,
    ReplayAcquisitionError,
    SNAPSHOT_SCRIPT,
    _accepted_replay_quality_failure_codes,
    _browser_export_geometry,
    _browser_selector_xpath,
    _canonical_url,
    _freeze_synchronous_visual_reference,
    _inject_computed_style_freeze,
    _inject_captured_media_posters,
    _inject_dom_topology_restore,
    _interactive_challenge_marker,
    _localize_computed_style_backgrounds,
    _is_optional_discovered_route,
    _local_path,
    _page_has_capture_surface,
    _replay_local_path,
    _route_kind,
    _infer_profile,
    _replay_spec,
    _rewrite_css,
    _rewrite_markup,
    _safe_read_route,
    _safe_private_selectors,
    _seed_navigation_candidates,
    _same_site_url,
    _site_domain,
    _strip_html_comments,
    _write_seed,
    _write_tasks,
)
from webanything.synthetic_visual import load_visual_reference


ROOT = Path(__file__).resolve().parents[1]


def _route() -> dict:
    return {
        "id": "route_home",
        "source_url": "https://www.example.test/",
        "local_path": "/",
        "title": "Example",
        "html_path": "capture/pages/route_home.html",
        "metadata_path": "capture/pages/route_home.json",
        "actions": [
            {
                "id": "route_home_save",
                "selector": '[data-action-id="route_home_save"]',
                "action_type": "click",
                "value": None,
                "tag": "button",
                "label": "Save",
            }
        ],
    }


def test_url_canonicalization_rejects_credentials_and_sensitive_queries():
    assert _canonical_url("https://example.test?b=2&a=1") == "https://example.test/?a=1&b=2"
    assert _local_path("https://example.test/search?q=one") == "/search?q=one"
    with pytest.raises(ReplayAcquisitionError, match="credential"):
        _canonical_url("https://user:pass@example.test/")
    with pytest.raises(ReplayAcquisitionError, match="sensitive query"):
        _canonical_url("https://example.test/callback?access_token=secret")
    assert _safe_read_route("https://example.test/feed") is True
    assert _safe_read_route("https://example.test/account/logout") is False


def test_authorized_browser_export_uses_a_bounded_selector_and_geometry_contract():
    assert _browser_selector_xpath("html > body > main > div:nth-of-type(2)") == "/html/body/main/div[2]"
    assert _browser_selector_xpath("html > ") == "/html"
    with pytest.raises(ReplayAcquisitionError, match="unsupported selector"):
        _browser_selector_xpath("html > body .unsafe")

    snapshot = {
        "actions": [{"source_selector": "html > body > main > button"}],
        "dom_topology": [
            {"node_id": "route_home_node_1", "tag": "header"},
            {"node_id": "route_home_node_2", "tag": "main"},
            {"node_id": "route_home_node_3", "tag": "button"},
            {"node_id": "route_home_node_4", "tag": "img"},
        ],
        "computed_styles": [
            {"node_id": "route_home_node_1", "tag": "header", "source_selector": "html > body > header", "rect": {"x": 0, "y": 0, "width": 567, "height": 80}, "styles": {"background-color": "rgb(0, 0, 0)"}},
            {"node_id": "route_home_node_2", "tag": "main", "source_selector": "html > body > main", "rect": {"x": 0, "y": 80, "width": 567, "height": 783}, "styles": {}},
            {"node_id": "route_home_node_3", "tag": "button", "source_selector": "html > body > main > button", "rect": {"x": 24, "y": 120, "width": 120, "height": 44}, "styles": {"border-radius": "8px"}},
            {"node_id": "route_home_node_4", "tag": "img", "source_selector": "html > body > main > img", "rect": {"x": 180, "y": 120, "width": 320, "height": 240}, "styles": {}},
        ],
    }
    geometry = _browser_export_geometry(snapshot, width=567, height=863)
    assert geometry["viewport"] == {"width": 567, "height": 863}
    assert geometry["landmarks"]["header"]["height"] == 80
    assert {item["category"] for item in geometry["elements"]} >= {"container", "control", "media"}


def test_authorized_route_origins_get_deterministic_local_paths():
    allowed = {"https://www.example.test", "https://space.example.test"}
    assert _canonical_url(
        "https://space.example.test/demo/favorites",
        expected_origins=allowed,
    ) == "https://space.example.test/demo/favorites"
    assert _replay_local_path(
        "https://space.example.test/demo/favorites",
        "https://www.example.test",
    ) == "/__webanything/source/space.example.test/demo/favorites"
    with pytest.raises(ReplayAcquisitionError, match="outside the seed origin"):
        _canonical_url("https://outside.test/", expected_origins=allowed)
    assert _route_kind("https://space.example.test/12345") == "profile"
    assert _route_kind("/__webanything/source/space.example.test/12345/favlist") == "favorites"


def test_navigation_timeout_can_reuse_an_already_rendered_document():
    class Page:
        def evaluate(self, _script):
            return True

    assert _page_has_capture_surface(Page()) is True

    class BrokenPage:
        def evaluate(self, _script):
            raise RuntimeError("browser closed")

    assert _page_has_capture_surface(BrokenPage()) is False


def test_robots_file_is_cached_per_origin(monkeypatch):
    reads = []

    class FakeResponse:
        status = 200

        def geturl(self):
            return "https://example.test/robots.txt"

        def read(self, _limit):
            return b"User-agent: *\nAllow: /\n"

        def close(self):
            pass

    def fake_urlopen(request, timeout):
        assert timeout == 15
        reads.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(synthetic_visual, "urlopen", fake_urlopen)
    monkeypatch.setattr(synthetic_visual, "_is_private_host", lambda _host: False)
    cache = {}
    for path in ("/", "/video/demo"):
        synthetic_visual._check_robots(
            f"https://example.test{path}",
            "WebAnythingReplayCapture/1.0",
            "respect",
            allow_private_network=False,
            cache=cache,
        )
    assert reads == ["https://example.test/robots.txt"]


def test_missing_robots_file_allows_capture_without_authentication(monkeypatch):
    class MissingResponse:
        status = 404

        def geturl(self):
            return "https://example.test/robots.txt"

        def read(self, _limit):
            return b""

        def close(self):
            pass

    monkeypatch.setattr(synthetic_visual, "urlopen", lambda *_args, **_kwargs: MissingResponse())
    monkeypatch.setattr(synthetic_visual, "_is_private_host", lambda _host: False)
    synthetic_visual._check_robots(
        "https://example.test/",
        "WebAnythingReplayCapture/1.0",
        "respect",
        allow_private_network=False,
    )


def test_capture_inventory_excludes_visible_but_occluded_controls():
    assert "const hitTestPoint = node =>" in SNAPSHOT_SCRIPT
    assert "elementFromPoint" in SNAPSHOT_SCRIPT
    assert "hit_point: hitPoint" in SNAPSHOT_SCRIPT
    assert "occluded_action_count: visibleCandidates.length - candidates.length" in SNAPSHOT_SCRIPT
    assert "template.setAttribute('shadowrootmode', 'open')" in SNAPSHOT_SCRIPT
    assert "media_surfaces: mediaSurfaces" in SNAPSHOT_SCRIPT
    assert "background_assets: backgroundAssets" in SNAPSHOT_SCRIPT
    assert "values.marginLeft = 'auto'" in SNAPSHOT_SCRIPT
    assert "Math.abs(leftGap - rightGap) <= 1" in SNAPSHOT_SCRIPT
    assert "node.pause()" in SNAPSHOT_SCRIPT


def test_capture_rewrite_localizes_assets_and_drops_external_runtime_urls():
    mapping = {
        "https://example.test/assets/logo.png": "capture/assets/logo.png",
        "https://cdn.example.test/font.woff2": "capture/assets/font.woff2",
    }
    markup = (
        '<style>.hero{background:url(https://example.test/assets/logo.png)}'
        '.tracker{background:url(https://tracker.test/pixel.png)}</style>'
        '<a href="https://example.test/watch?v=1">'
        '<img src="/assets/logo.png" srcset="/assets/logo.png 2x"></a>'
        '<img src="https://tracker.test/pixel.png">'
    )
    rewritten = _rewrite_markup(markup, "https://example.test", mapping)
    assert 'href="/watch?v=1"' in rewritten
    assert 'src="assets/logo.png"' in rewritten
    assert 'srcset="assets/logo.png 2x"' in rewritten
    assert 'background:url(../assets/logo.png)' in rewritten
    assert "tracker.test" not in rewritten
    css = "@font-face{src:url(https://cdn.example.test/font.woff2)} .x{background:url(https://tracker.test/p.png)}"
    rewritten_css = _rewrite_css(css, "https://example.test/app.css", mapping, Path("capture/assets/app.css"))
    assert "font.woff2" in rewritten_css
    assert "tracker.test" not in rewritten_css


def test_capture_rewrite_drops_session_blob_urls_and_binds_frozen_video_poster():
    markup = (
        '<video autoplay src="blob:https://example.test/session" '
        'data-webanything-node-id="route_watch_node_0007"></video>'
        '<style>.player{background-image:url(blob:https://example.test/background)}</style>'
    )
    rewritten = _rewrite_markup(markup, "https://example.test", {})
    assert "blob:" not in rewritten
    frozen = _inject_captured_media_posters(rewritten, {
        "media_posters": [{
            "node_id": "route_watch_node_0007",
            "path": "assets/0123456789abcdef01234567.png",
        }],
    })
    assert "autoplay" not in frozen
    assert 'poster="assets/0123456789abcdef01234567.png"' in frozen
    assert 'preload="metadata"' in frozen


def test_capture_rewrite_keeps_only_authorized_cross_origin_navigation():
    markup = (
        '<a href="https://space.example.test/demo/favorites">Favorites</a>'
        '<a href="https://outside.test/profile">Outside</a>'
    )
    rewritten = _rewrite_markup(
        markup,
        "https://www.example.test",
        {},
        allowed_route_origins={"https://www.example.test", "https://space.example.test"},
    )
    assert 'href="/__webanything/source/space.example.test/demo/favorites"' in rewritten
    assert "outside.test" not in rewritten


def test_capture_drops_inert_comments_that_look_executable():
    markup = '<main>Replay</main><!-- <script src="/legacy.js"></script> --><!-- private note -->'
    cleaned = _strip_html_comments(markup)
    assert cleaned == "<main>Replay</main>"
    assert "<script" not in cleaned


def test_seed_navigation_candidates_include_public_home_fallbacks():
    assert "--disable-http2" in CAPTURE_BROWSER_ARGS
    assert _seed_navigation_candidates("https://example.test/") == [
        "https://example.test/",
        "https://example.test/home",
        "https://www.example.test/",
        "https://www.example.test/home",
    ]
    assert _seed_navigation_candidates("https://www.example.test/products") == [
        "https://www.example.test/products",
    ]


def test_interactive_human_challenge_is_not_frozen_as_source_content():
    assert _interactive_challenge_marker("Let's confirm you are human") == "human_verification"
    assert _interactive_challenge_marker("Complete the security check") == "security_check"
    assert _interactive_challenge_marker("Browse all products and categories") is None


def test_route_redirect_scope_accepts_only_same_site_subdomains():
    assert _site_domain("www.stashmedia.tv") == "stashmedia.tv"
    assert _site_domain("www.audit.gov.cn") == "audit.gov.cn"
    assert _same_site_url(
        "https://www.stashmedia.tv/shop", "https://store.stashmedia.tv/products/demo"
    )
    assert not _same_site_url(
        "https://www.stashmedia.tv/shop", "https://unrelated-store.test/products/demo"
    )


def test_replay_accepts_healthy_full_bleed_geometry_but_not_page_cover_corruption():
    geometry = {
        "truncated": False,
        "viewport": {"width": 1440, "height": 1000},
        "elements": [
            {"category": "media", "width": 1440, "height": 1000},
            {"category": "container", "width": 400, "height": 300},
            {"category": "text", "width": 300, "height": 40},
            {"category": "control", "width": 120, "height": 40},
        ],
    }
    full_bleed = {
        "failures": [{"code": "page_covering_rectangle"}, {"code": "column_saturation"}],
        "metrics": {
            "landmark_count": 2,
            "max_rectangle_coverage": 1.0,
            "max_overlap_ratio": 1.1,
        },
    }
    assert _accepted_replay_quality_failure_codes(geometry, full_bleed) == [
        "column_saturation",
        "page_covering_rectangle",
    ]
    structured_geometry = {
        "truncated": False,
        "viewport": {"width": 1440, "height": 1000},
        "elements": [{"category": "container", "width": 60, "height": 1000} for _ in range(12)],
    }
    structured_saturation = {
        "failures": [{"code": "column_saturation"}],
        "metrics": {
            "landmark_count": 2,
            "max_rectangle_coverage": 0.2,
            "max_overlap_ratio": 1.1,
        },
    }
    assert _accepted_replay_quality_failure_codes(
        structured_geometry, structured_saturation
    ) == ["column_saturation"]
    structurally_empty = {
        "truncated": False,
        "viewport": {"width": 1440, "height": 1000},
        "elements": [{"category": "media", "width": 1440, "height": 1000}],
    }
    corrupted = {
        "failures": [{"code": "column_saturation"}, {"code": "page_covering_rectangle"}],
        "metrics": {
            "landmark_count": 0,
            "max_rectangle_coverage": 1.0,
            "max_overlap_ratio": 1.1,
        },
    }
    assert _accepted_replay_quality_failure_codes(structurally_empty, corrupted) == []


def test_discovered_visual_failures_are_skippable_but_required_routes_remain_authoritative():
    required = {"https://example.test/"}
    assert _is_optional_discovered_route("https://example.test/campaign", required) is True
    assert _is_optional_discovered_route("https://example.test/", required) is False


def test_computed_style_freeze_is_injected_without_live_background_urls():
    markup = '<html><head></head><body><div data-webanything-node-id="route_home_node_0001"></div></body></html>'
    metadata = {
        "computed_styles": [
            {
                "node_id": "route_home_node_0001",
                "style": {
                    "boxSizing": "border-box",
                    "width": "320px",
                    "backgroundImage": "captured-local-asset",
                },
            }
        ]
    }
    frozen = _inject_computed_style_freeze(markup, metadata)
    assert 'data-webanything-computed-style-freeze="v1"' in frozen
    assert '[data-webanything-node-id="route_home_node_0001"]' in frozen
    assert "box-sizing:border-box!important" in frozen
    assert "width:320px!important" in frozen
    assert "captured-local-asset" not in frozen


def test_computed_style_backgrounds_are_bound_to_local_capture_assets():
    metadata = {
        "computed_styles": [
            {
                "node_id": "route_home_node_0001",
                "style": {
                    "backgroundImage": 'url("https://cdn.example.test/hero.png")',
                },
            }
        ]
    }
    _localize_computed_style_backgrounds(
        metadata,
        "https://example.test/home",
        {"https://cdn.example.test/hero.png": "capture/assets/hero.png"},
    )
    background = metadata["computed_styles"][0]["style"]["backgroundImage"]
    assert background == 'url("../assets/hero.png")'
    frozen = _inject_computed_style_freeze(
        '<html><head></head><body><div data-webanything-node-id="route_home_node_0001"></div></body></html>',
        metadata,
    )
    assert 'background-image:url("../assets/hero.png")!important' in frozen
    assert "cdn.example.test" not in frozen


def test_dom_topology_restore_reparents_dynamically_constructed_markup():
    markup = (
        '<html data-webanything-node-id="root"><body data-webanything-node-id="body">'
        '<div data-webanything-node-id="parent"></div><li data-webanything-node-id="child">Item</li>'
        '</body></html>'
    )
    restored = _inject_dom_topology_restore(markup, {
        "dom_topology": [{"node_id": "child", "parent_node_id": "parent"}],
    })
    assert 'data-webanything-dom-topology="v1"' in restored
    assert 'data-webanything-topology-restored' in restored
    assert '"node_id":"child","parent_node_id":"parent"' in restored
    assert "'top','right','bottom','left','inset','transform','transformOrigin'" in SNAPSHOT_SCRIPT


def test_seed_tasks_and_contract_draft_are_deterministic(tmp_path: Path):
    route = _route()
    seed = tmp_path / "fixtures/seed.sqlite3"
    _write_seed(seed, [route])
    with sqlite3.connect(seed) as connection:
        assert connection.execute("SELECT id FROM users").fetchone() == ("usr_demo_001",)
        assert connection.execute("SELECT id FROM captured_actions").fetchone() == ("route_home_save",)
    tasks_path = tmp_path / "tasks.jsonl"
    count = _write_tasks(tasks_path, [route])
    tasks = [json.loads(line) for line in tasks_path.read_text().splitlines()]
    assert count == 4
    assert {task["workflow"] for task in tasks} == {"login", "logout", "reset", "stateful_action"}
    reference = {"reference_sha256": "a" * 64}
    spec = _replay_spec(site="example", seed_url="https://example.test/", routes=[route], reference=reference)
    assert spec["schema"] == "webanything-synthetic-replay-spec-v1"
    assert spec["routes"][0]["required_action_ids"] == ["route_home_save"]
    assert spec["routes"][0]["kind"] == "home"
    assert spec["coverage"]["release_tier"] == "smoke"
    assert spec["storage"]["engine"] == "SQLite"


def test_seed_allows_the_same_stable_action_on_multiple_routes(tmp_path: Path):
    first = _route()
    second = _route()
    second.update({"id": "route_news", "local_path": "/news"})
    seed = tmp_path / "fixtures/seed.sqlite3"
    _write_seed(seed, [first, second])
    with sqlite3.connect(seed) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM captured_actions WHERE id = ?", ("route_home_save",)
        ).fetchone() == (2,)


def test_video_profile_adds_product_routes_domain_state_and_semantic_tasks(tmp_path: Path):
    route = _route()
    route["source_url"] = "https://www.bilibili.com/"
    assert _infer_profile(route["source_url"], [route]) == VIDEO_PROFILE
    seed = tmp_path / "fixtures/seed.sqlite3"
    _write_seed(seed, [route], profile=VIDEO_PROFILE)
    with sqlite3.connect(seed) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"videos", "comments", "favorites", "history", "playback_state"} <= tables
    tasks_path = tmp_path / "tasks.jsonl"
    _write_tasks(tasks_path, [route], profile=VIDEO_PROFILE)
    tasks = [json.loads(line) for line in tasks_path.read_text().splitlines()]
    assert {"search", "playback", "favorite", "comment", "history", "profile"} <= {
        task["workflow"] for task in tasks
    }
    assert all(task["success"] for task in tasks)
    spec = _replay_spec(
        site="bilibili",
        seed_url="https://www.bilibili.com/",
        routes=[route],
        reference={"reference_sha256": "a" * 64},
        profile=VIDEO_PROFILE,
    )
    assert spec["coverage"]["release_tier"] == "product_complete"
    assert "route_home_save" in spec["routes"][0]["required_action_ids"]
    assert {item["kind"] for item in spec["routes"]} >= {
        "home", "browse", "search", "detail", "favorites", "history", "profile"
    }
    assert "comment" in spec["coverage"]["required_workflows"]


def test_commerce_profile_adds_product_routes_domain_state_and_semantic_tasks(tmp_path: Path):
    route = _route()
    route["source_url"] = "https://www.shein.com/"
    assert _infer_profile(route["source_url"], [route]) == COMMERCE_PROFILE
    seed = tmp_path / "fixtures/seed.sqlite3"
    _write_seed(seed, [route], profile=COMMERCE_PROFILE)
    with sqlite3.connect(seed) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert connection.execute("SELECT id FROM products").fetchone() == ("prd_demo_001",)
    assert {"products", "cart_items", "orders"} <= tables
    tasks_path = tmp_path / "tasks.jsonl"
    _write_tasks(tasks_path, [route], profile=COMMERCE_PROFILE)
    tasks = [json.loads(line) for line in tasks_path.read_text().splitlines()]
    assert {"search", "add_to_cart", "checkout", "orders", "profile"} <= {
        task["workflow"] for task in tasks
    }
    spec = _replay_spec(
        site="shein",
        seed_url="https://www.shein.com/",
        routes=[route],
        reference={"reference_sha256": "a" * 64},
        profile=COMMERCE_PROFILE,
    )
    assert spec["coverage"]["release_tier"] == "product_complete"
    assert {item["kind"] for item in spec["routes"]} >= {
        "home", "browse", "search", "detail", "cart", "orders", "profile"
    }
    assert "add_to_cart" in spec["coverage"]["required_workflows"]


def test_travel_profile_adds_product_routes_domain_state_and_semantic_tasks(tmp_path: Path):
    route = _route()
    route["source_url"] = "https://www.ctrip.com/"
    assert _infer_profile(route["source_url"], [route]) == TRAVEL_PROFILE
    seed = tmp_path / "fixtures/seed.sqlite3"
    _write_seed(seed, [route], profile=TRAVEL_PROFILE)
    with sqlite3.connect(seed) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert connection.execute("SELECT id FROM flights").fetchone() == ("flt_demo_001",)
    assert {"flights", "flight_searches", "bookings"} <= tables
    tasks_path = tmp_path / "tasks.jsonl"
    _write_tasks(tasks_path, [route], profile=TRAVEL_PROFILE)
    tasks = [json.loads(line) for line in tasks_path.read_text().splitlines()]
    assert {"search", "navigate", "book", "bookings", "profile"} <= {
        task["workflow"] for task in tasks
    }
    search_task = next(task for task in tasks if task["workflow"] == "search")
    assert {item["surface"] for item in search_task["success"]} == {"url", "dom", "sqlite"}
    spec = _replay_spec(
        site="ctrip",
        seed_url="https://www.ctrip.com/",
        routes=[route],
        reference={"reference_sha256": "a" * 64},
        profile=TRAVEL_PROFILE,
    )
    assert spec["coverage"]["release_tier"] == "product_complete"
    assert {item["kind"] for item in spec["routes"]} >= {
        "home", "browse", "search", "detail", "bookings", "profile"
    }
    assert {"travel_search_submit", "open_bookings"} <= set(
        spec["routes"][0]["required_action_ids"]
    )


def test_content_profile_uses_captured_routes_and_persistent_bookmarks(tmp_path: Path):
    home = _route()
    home["source_url"] = "https://www.accaglobal.com/"
    content = _route()
    content.update({
        "id": "route_guidance",
        "source_url": "https://www.accaglobal.com/gb/en/help.html",
        "local_path": "/gb/en/help.html",
        "actions": [],
    })
    routes = [home, content]
    assert _infer_profile(home["source_url"], routes) == CONTENT_PROFILE
    seed = tmp_path / "fixtures/seed.sqlite3"
    _write_seed(seed, routes, profile=CONTENT_PROFILE)
    with sqlite3.connect(seed) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert connection.execute("SELECT id FROM content_items").fetchone() == ("cnt_demo_001",)
    assert {"content_items", "bookmarks"} <= tables
    tasks_path = tmp_path / "tasks.jsonl"
    _write_tasks(tasks_path, routes, profile=CONTENT_PROFILE)
    tasks = [json.loads(line) for line in tasks_path.read_text().splitlines()]
    assert {task["workflow"] for task in tasks} == {
        "login", "logout", "reset", "navigate", "stateful_action"
    }
    bookmark_task = next(task for task in tasks if task["workflow"] == "stateful_action")
    assert {item["surface"] for item in bookmark_task["success"]} == {"dom", "sqlite"}
    spec = _replay_spec(
        site="acca",
        seed_url=home["source_url"],
        routes=routes,
        reference={"reference_sha256": "a" * 64},
        profile=CONTENT_PROFILE,
    )
    assert spec["coverage"]["release_tier"] == "product_complete"
    assert spec["coverage"]["required_route_kinds"] == ["browse", "home"]
    assert {"open_content_route_001", "bookmark_content"} <= set(
        spec["routes"][0]["required_action_ids"]
    )
    assert all(route["source"] != "synthetic" for route in spec["routes"])


def test_public_service_and_single_page_sources_get_complete_profiles():
    public_route = _route()
    public_route["source_url"] = "https://audit.gov.cn/"
    assert _infer_profile(public_route["source_url"], [public_route]) == CONTENT_PROFILE

    landing_route = _route()
    landing_route["source_url"] = "https://fictional-brand.test/"
    assert _infer_profile(landing_route["source_url"], [landing_route]) == STATIC_LANDING_PROFILE


def test_static_landing_profile_is_complete_for_its_bounded_home_product(tmp_path: Path):
    tasks_path = tmp_path / "tasks.jsonl"
    _write_tasks(tasks_path, [_route()], profile=STATIC_LANDING_PROFILE)
    state_task = next(
        item for item in map(json.loads, tasks_path.read_text().splitlines())
        if item["workflow"] == "stateful_action"
    )
    assert {item["surface"] for item in state_task["success"]} == {"dom", "sqlite"}
    spec = _replay_spec(
        site="djicdn",
        seed_url="https://djicdn.com/",
        routes=[_route()],
        reference={"reference_sha256": "a" * 64},
        profile=STATIC_LANDING_PROFILE,
    )
    assert spec["coverage"] == {
        "profile": STATIC_LANDING_PROFILE,
        "release_tier": "product_complete",
        "required_route_kinds": ["home"],
        "required_workflows": ["login", "logout", "reset", "stateful_action"],
        "generic_fallback_allowed": False,
    }


def test_private_selector_defaults_are_narrow_and_deduplicated():
    selectors = _safe_private_selectors(["[data-webanything-private]", ".account-email"])
    assert len([item for item in selectors if item["selector"] == "[data-webanything-private]"]) == 1
    assert any(item["selector"] == ".account-email" for item in selectors)


def test_synchronous_visual_reference_freezes_existing_route_artifacts(tmp_path: Path):
    source = ROOT / "examples/synthetic/cloudpost/contract/visual-reference"
    output = tmp_path / "visual-reference"
    shutil.copytree(source, output)
    manifest_path = output / "synthetic_visual_reference.json"
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.unlink()
    spec_path = tmp_path / "visual-capture-spec.json"
    spec_path.write_text("{}\n", encoding="utf-8")
    spec = {
        "site": "cloudpost",
        "source_origin": "https://mail.example.test",
        "viewport": source_manifest["viewport"],
        "browser_profile": {
            "locale": "en-US",
            "timezone_id": "Etc/UTC",
            "accept_language": "en-US,en;q=0.9",
        },
    }

    frozen = _freeze_synchronous_visual_reference(
        output_dir=output,
        spec_path=spec_path,
        spec=spec,
        route_records=source_manifest["routes"],
        auth={"used": False, "profile_id": None},
        private_fields_replaced=0,
        blocked_origins=set(),
    )

    assert frozen == load_visual_reference(manifest_path)
    assert frozen["routes"][0]["screenshot"] == source_manifest["routes"][0]["screenshot"]


def test_replay_build_runs_capture_before_dispatch(tmp_path: Path, monkeypatch):
    observed: dict[str, object] = {}
    contract = tmp_path / "run/replay-source/synthetic_replay_contract.json"

    def acquire(_args, output_dir):
        observed["output_dir"] = output_dir
        contract.parent.mkdir(parents=True)
        contract.write_text("{}")
        return {"status": "completed", "contract_path": str(contract)}

    def build(args):
        observed["build_mode"] = args.build_mode
        observed["profile"] = args.profile
        observed["contract"] = args.synthetic_contract
        return 0

    monkeypatch.setattr(cli, "_capture_replay", acquire)
    monkeypatch.setattr(cli, "command_build", build)
    args = Namespace(
        url="https://example.test", site="example", run_dir=str(tmp_path / "run"), route=[],
        max_pages=4, max_assets=20, max_asset_bytes=1000, max_actions_per_route=20,
        viewport_width=1440, viewport_height=1000, auth_profile=None, key_file=None,
        private_selector=[], authenticated_selector=None, browser_path=None, dependency_path=None, capture_timeout_ms=1000,
        timeout_minutes=1, agent_config=None, model=None, reasoning_effort=None,
        requirements_file=None, no_reflection=True, skip_artifact=True, allow_private_network=False,
    )
    assert cli.command_replay_build(args) == 0
    assert observed["output_dir"] == tmp_path / "run/replay-source"
    assert observed["build_mode"] == "benchmark_synthetic_replay"
    assert observed["profile"] == "benchmark_synthetic_replay"
    assert observed["contract"] == str(contract)
    evidence = json.loads((tmp_path / "run/replay-acquisition.json").read_text())
    assert evidence["output_dir"] == "replay-source"
    assert evidence["contract_path"] == "replay-source/synthetic_replay_contract.json"
