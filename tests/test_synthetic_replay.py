from __future__ import annotations

import json
import shutil
import sqlite3
import importlib.util
import subprocess
from pathlib import Path

import jsonschema
import pytest

from webanything.release import ReleaseError, candidate_identity, freeze_candidate, sha256_file
from webanything.synthetic_replay import (
    SyntheticReplayContractError,
    canonical_sha256,
    compile_synthetic_replay_contract,
    load_synthetic_replay_contract,
)
from webanything.synthetic_replay_release import REPLAY_REQUIRED_GATES, release_synthetic_replay_candidate
from webanything.synthetic_replay_verify import (
    _captured_dom_requirements,
    _captured_target_kind,
    _local_redirect_target,
    verify_synthetic_replay_candidate,
)
import webanything.synthetic_replay_verify as replay_verify
from webanything.synthetic_replay_capture import (
    SyntheticReplayCaptureError,
    freeze_replay_capture,
    trusted_dom_topology_script,
)


ROOT = Path(__file__).resolve().parents[1]
STATIC_EXAMPLE = ROOT / "examples/synthetic/cloudpost/contract/visual-reference"


def _runtime_module():
    path = ROOT / "runtime/webanything_replay_runtime.py"
    spec = importlib.util.spec_from_file_location("webanything_replay_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _make_bundle(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / "capture").mkdir(parents=True)
    (source / "fixtures").mkdir()
    shutil.copytree(STATIC_EXAMPLE, source / "visual-reference")
    dom = source / "capture/home.html"
    css = source / "capture/app.css"
    dom.write_text("<main data-page='home'><button data-action-id='login_submit'>Login</button></main>\n", encoding="utf-8")
    css.write_text("main { display: grid; }\n", encoding="utf-8")
    files = [
        {"path": "capture/home.html", "sha256": sha256_file(dom), "kind": "dom", "bytes": dom.stat().st_size},
        {"path": "capture/app.css", "sha256": sha256_file(css), "kind": "css", "bytes": css.stat().st_size},
    ]
    _write_json(source / "capture/manifest.json", {
        "schema": "webanything-replay-capture-manifest-v1",
        "source_origin": "https://www.example.test",
        "external_javascript_execution": False,
        "source_auth_persisted": False,
        "privacy": {"status": "passed", "source_credentials_removed": True, "private_fields_removed": 1, "report_sha256": "a" * 64},
        "routes": [{"source_url": "https://www.example.test/", "local_path": "/"}],
        "files": files,
    })
    seed = source / "fixtures/seed.sqlite3"
    with sqlite3.connect(seed) as connection:
        connection.executescript("CREATE TABLE users(id TEXT PRIMARY KEY, signed_in INTEGER); INSERT INTO users VALUES('usr_demo', 0);")
    tasks = [
        {"task_id": "login_task", "workflow": "login", "initial_state": "anonymous", "start_url": "/login", "instruction": "Log in", "actions": [{"type": "click", "selector": "[data-action-id=login_submit]"}], "evaluator_id": "eval_login", "success": [{"surface": "dom", "selector": "[data-webanything-auth-state=authenticated]", "visible": True}, {"surface": "sqlite", "table": "users", "where": {"id": "usr_demo", "signed_in": 1}, "count_equals": 1}]},
        {"task_id": "logout_task", "workflow": "logout", "initial_state": "authenticated", "start_url": "/", "instruction": "Log out", "actions": [{"type": "click", "selector": "[data-action-id=logout]"}], "evaluator_id": "eval_logout", "success": [{"surface": "dom", "selector": "[data-webanything-auth-entry]", "visible": True}, {"surface": "sqlite", "table": "users", "where": {"id": "usr_demo", "signed_in": 0}, "count_equals": 1}]},
        {"task_id": "reset_task", "workflow": "reset", "initial_state": "anonymous", "start_url": "/reset.html", "instruction": "Reset", "actions": [], "evaluator_id": "eval_reset", "success": [{"surface": "dom", "selector": "[data-page=reset]", "visible": True}, {"surface": "sqlite", "table": "users", "where": {"id": "usr_demo", "signed_in": 0}, "count_equals": 1}]},
        {"task_id": "state_task", "workflow": "stateful_action", "initial_state": "anonymous", "start_url": "/", "instruction": "Save an item", "actions": [{"type": "click", "selector": "[data-action-id=save_item]"}], "evaluator_id": "eval_save", "success": [{"surface": "dom", "selector": "[data-saved=true]", "visible": True}, {"surface": "sqlite", "table": "actions", "where": {"id": "saved", "value": 1}, "count_equals": 1}]},
    ]
    (source / "tasks.jsonl").write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in tasks), encoding="utf-8")
    reference = json.loads((source / "visual-reference/synthetic_visual_reference.json").read_text(encoding="utf-8"))
    spec = {
        "schema": "webanything-synthetic-replay-spec-v1",
        "seed_url": "https://www.example.test/",
        "site_id": "cloudpost",
        "product_archetype": "test_app_v1",
        "coverage": {"profile": "test_app_v1", "release_tier": "product_complete", "required_route_kinds": ["home", "auth", "reset"], "required_workflows": ["login", "logout", "reset", "stateful_action"], "generic_fallback_allowed": False},
        "clock": {"fixed_time": "2026-06-15T10:00:00Z", "timezone": "UTC"},
        "runtime": {"kind": "flask_sqlite", "entrypoint": "app.py", "health_path": "/_health", "proxy_prefix_aware": True},
        "storage": {"engine": "SQLite", "seed_path": "fixtures/seed.sqlite3", "runtime_path": "instance/runtime.sqlite3", "reset_mode": "atomic_seed_replace"},
        "auth": {"mode": "local_synthetic", "initial_state": "explicit_login", "state_scope": "instance", "demo_user_ids": ["usr_demo"], "source_credentials_persisted": False},
        "routes": [
            {"id": "route_home", "path": "/", "kind": "home", "source": "captured", "required_selectors": ["[data-page=home]"], "required_action_ids": ["logout", "save_item"]},
            {"id": "route_login", "path": "/login", "kind": "auth", "source": "system", "required_selectors": ["[data-page=login]"], "required_action_ids": ["login_submit"]},
            {"id": "route_reset", "path": "/reset.html", "kind": "reset", "source": "system", "required_selectors": ["[data-page=reset]"], "required_action_ids": []},
        ],
        "capture_manifest": {"path": "capture/manifest.json"},
        "visual_reference": {"path": "visual-reference/synthetic_visual_reference.json", "reference_sha256": reference["reference_sha256"]},
        "tasks": {"path": "tasks.jsonl"},
        "reset": {"supported": True, "entrypoint": "/reset.html", "endpoint": "/api/reset", "method": "POST"},
        "evaluator": {"endpoint_template": "/api/evaluators/{evaluator_id}", "result_field": "passed", "authoritative_surfaces": ["dom", "url", "SQLite", "network"]},
        "network_policy": {"external_requests": 0, "allowed_origins": [], "upstream_javascript_execution": False},
        "content_policy": {"mode": "source_fidelity", "private_user_data_allowed": False, "source_assets_local_only": True},
        "concurrency": {"isolation": "isolated_instance", "max_parallel_sessions": 1},
    }
    spec_path = source / "replay-spec.json"
    _write_json(spec_path, spec)
    output = tmp_path / "compiled/synthetic_replay_contract.json"
    return spec_path, output


def test_replay_contract_compiles_copies_inputs_and_validates_schema(tmp_path: Path):
    spec_path, output = _make_bundle(tmp_path)
    contract = compile_synthetic_replay_contract(spec_path, output)
    assert contract["build_mode"] == "benchmark_synthetic_replay"
    assert contract["tasks"]["count"] == 4
    assert contract["capture_manifest"]["file_count"] == 2
    assert (output.parent / "capture/home.html").is_file()
    assert (output.parent / "fixtures/seed.sqlite3").is_file()
    assert (output.parent / "visual-reference/routes/route_inbox.source-visible.png").is_file()
    assert load_synthetic_replay_contract(output) == contract
    schema = json.loads((ROOT / "schemas/synthetic-replay-contract-v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(contract)


def test_verifier_requires_actual_computed_style_dom_replay(tmp_path: Path):
    contract_path = tmp_path / "synthetic_replay_contract.json"
    page_dir = tmp_path / "capture/pages"
    page_dir.mkdir(parents=True)
    (page_dir / "route_home.html").write_text(
        '<html><head><style data-webanything-computed-style-freeze="v1"></style></head></html>',
        encoding="utf-8",
    )
    _write_json(
        page_dir / "route_home.json",
        {
            "computed_styles": [
                {"node_id": f"route_home_node_{index:04d}", "style": {"display": "block"}}
                for index in range(100)
            ]
        },
    )
    contract = {
        "routes": [
            {"id": "route_home", "path": "/", "source": "captured"},
            {"id": "route_login", "path": "/login", "source": "system"},
        ]
    }
    requirements = _captured_dom_requirements(contract_path, contract)
    assert requirements == {
        "/": {"expected_visible_nodes": 100, "minimum_visible_nodes": 60}
    }


def test_captured_action_targets_keep_product_route_semantics():
    assert _captured_target_kind("/video/BV123") == "detail"
    assert _captured_target_kind("/c/douga/") == "browse"
    assert _captured_target_kind("/account/history") == "history"
    assert _captured_target_kind("/space/demo") == "profile"


def test_action_navigation_redirects_resolve_only_same_origin():
    class Response:
        status = 302
        headers = {"location": "/category/video"}

    assert _local_redirect_target(
        Response(),
        "http://127.0.0.1:40000/api/navigate/open_category",
        "http://127.0.0.1:40000",
    ) == "http://127.0.0.1:40000/category/video"

    Response.headers = {"location": "https://outside.test/category/video"}
    assert _local_redirect_target(
        Response(),
        "http://127.0.0.1:40000/api/navigate/open_category",
        "http://127.0.0.1:40000",
    ) is None


def test_replay_contract_rejects_capture_mutation_and_false_concurrency(tmp_path: Path):
    spec_path, output = _make_bundle(tmp_path)
    compile_synthetic_replay_contract(spec_path, output)
    (output.parent / "capture/home.html").write_text("mutated\n", encoding="utf-8")
    with pytest.raises(SyntheticReplayContractError, match="capture file hash mismatch"):
        load_synthetic_replay_contract(output)

    spec_path, second = _make_bundle(tmp_path / "second")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["concurrency"]["max_parallel_sessions"] = 2
    _write_json(spec_path, spec)
    with pytest.raises(SyntheticReplayContractError, match="exactly one session"):
        compile_synthetic_replay_contract(spec_path, second)


def test_product_complete_contract_rejects_event_only_business_success(tmp_path: Path):
    spec_path, output = _make_bundle(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    tasks_path = spec_path.parent / spec["tasks"]["path"]
    tasks = [json.loads(line) for line in tasks_path.read_text(encoding="utf-8").splitlines()]
    state_task = next(task for task in tasks if task["workflow"] == "stateful_action")
    state_task["success"] = [
        {"surface": "sqlite", "table": "replay_events", "where": {"action_id": "save_item"}, "count_equals": 1}
    ]
    tasks_path.write_text(
        "".join(json.dumps(task, sort_keys=True) + "\n" for task in tasks),
        encoding="utf-8",
    )
    with pytest.raises(SyntheticReplayContractError, match="generic action logs"):
        compile_synthetic_replay_contract(spec_path, output)


def test_capture_freezer_hashes_sanitized_files_and_rejects_upstream_javascript(tmp_path: Path):
    bundle = tmp_path / "bundle"
    capture = bundle / "capture"
    capture.mkdir(parents=True)
    (capture / "home.html").write_text("<main>Local replay</main>\n", encoding="utf-8")
    (capture / "style.css").write_text("main { display: block; }\n", encoding="utf-8")
    routes = tmp_path / "routes.json"
    privacy = tmp_path / "privacy.json"
    _write_json(routes, {"routes": [{"source_url": "https://www.example.test/", "local_path": "/"}]})
    _write_json(privacy, {"schema": "webanything-replay-privacy-report-v1", "status": "passed", "source_credentials_removed": True, "private_fields_removed": 2})
    output = capture / "manifest.json"
    manifest = freeze_replay_capture(
        bundle_root=bundle,
        capture_dir=capture,
        source_origin="https://www.example.test",
        routes_path=routes,
        privacy_report_path=privacy,
        output_path=output,
    )
    assert [item["path"] for item in manifest["files"]] == ["capture/home.html", "capture/style.css"]
    assert manifest["privacy"]["private_fields_removed"] == 2
    schema = json.loads((ROOT / "schemas/replay-capture-manifest-v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(manifest)

    _write_json(routes, {
        "routes": [
            {"source_url": "https://www.example.test/", "local_path": "/"},
            {
                "source_url": "https://space.example.test/demo/favorites",
                "local_path": "/__webanything/source/space.example.test/demo/favorites",
            },
        ]
    })
    manifest = freeze_replay_capture(
        bundle_root=bundle,
        capture_dir=capture,
        source_origin="https://www.example.test",
        allowed_source_origins={"https://www.example.test", "https://space.example.test"},
        routes_path=routes,
        privacy_report_path=privacy,
        output_path=output,
    )
    assert manifest["allowed_source_origins"] == ["https://space.example.test"]
    _write_json(routes, {"routes": [{"source_url": "https://www.example.test/", "local_path": "/"}]})

    (capture / "home.html").write_text(
        "<main>Local replay</main>" + trusted_dom_topology_script([
            {"node_id": "child", "parent_node_id": "parent"},
        ]),
        encoding="utf-8",
    )
    freeze_replay_capture(
        bundle_root=bundle,
        capture_dir=capture,
        source_origin="https://www.example.test",
        routes_path=routes,
        privacy_report_path=privacy,
        output_path=output,
    )

    (capture / "home.html").write_text(
        '<main>Local replay</main><script data-webanything-dom-topology="v1">alert(1)</script>',
        encoding="utf-8",
    )
    with pytest.raises(SyntheticReplayCaptureError, match="executable upstream markup"):
        freeze_replay_capture(
            bundle_root=bundle,
            capture_dir=capture,
            source_origin="https://www.example.test",
            routes_path=routes,
            privacy_report_path=privacy,
            output_path=output,
        )

    (capture / "home.html").write_text("<main>Local replay</main>\n", encoding="utf-8")

    (capture / "upstream.js").write_text("alert(1)\n", encoding="utf-8")
    with pytest.raises(SyntheticReplayCaptureError, match="JavaScript is forbidden"):
        freeze_replay_capture(
            bundle_root=bundle,
            capture_dir=capture,
            source_origin="https://www.example.test",
            routes_path=routes,
            privacy_report_path=privacy,
            output_path=output,
        )


def test_replay_runtime_reset_is_byte_identical_and_prefix_safe(tmp_path: Path):
    runtime = _runtime_module()
    seed = tmp_path / "seed.sqlite3"
    seed.write_bytes(b"fixed sqlite bytes")
    target = tmp_path / "instance/runtime.sqlite3"
    target.parent.mkdir()
    target.write_bytes(b"mutated")
    runtime.atomic_seed_reset(seed, target)
    assert target.read_bytes() == seed.read_bytes()
    runtime.atomic_seed_reset(seed, target)
    assert target.read_bytes() == seed.read_bytes()
    assert runtime.scoped_path("/p/40000/", "/feed/shorts") == "/p/40000/feed/shorts"
    markup = (
        '<a href="/login" data-webanything-auth-href="/p/40000/login">Login</a>'
        '<form action = "/api/login"><img src="/assets/avatar.png"></form>'
        '<a href="https://example.test/">External</a><img src="//cdn.test/image.png">'
    )
    scoped = runtime.scope_html_urls(markup, "/p/40000/")
    assert 'href="/p/40000/login"' in scoped
    assert 'data-webanything-auth-href="/p/40000/login"' in scoped
    assert "/p/40000/p/40000/" not in scoped
    assert 'action = "/p/40000/api/login"' in scoped
    assert 'src="/p/40000/assets/avatar.png"' in scoped
    assert 'href="https://example.test/"' in scoped
    assert 'src="//cdn.test/image.png"' in scoped
    assert runtime.scope_html_urls(scoped, "/p/40000") == scoped
    assert runtime.health_payload(site_id="cloudpost", prefix="/p/40000")["effective_prefix"] == "/p/40000"
    with pytest.raises(ValueError):
        runtime.normalize_prefix("/p/../private")


def _release_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    spec_path, contract_path = _make_bundle(tmp_path)
    contract = compile_synthetic_replay_contract(spec_path, contract_path)
    site = tmp_path / "site"
    (site / "fixtures").mkdir(parents=True)
    (site / "instance").mkdir()
    (site / "app.py").write_text("# local replay runtime\n", encoding="utf-8")
    shutil.copy2(contract_path, site / "synthetic_replay_contract.json")
    shutil.copy2(contract_path.parent / contract["storage"]["seed_path"], site / contract["storage"]["seed_path"])
    _write_json(site / "replay-provenance.json", {
        "schema": "webanything-replay-provenance-v1",
        "capture_bundle_sha256": contract["capture_manifest"]["bundle_sha256"],
        "seed_sha256": contract["storage"]["seed_sha256"],
        "candidate_files": ["app.py"],
    })
    reference_path = contract_path.parent / contract["visual_reference"]["path"]
    identity = candidate_identity(
        site,
        contract_paths=[contract_path],
        reference_paths=[reference_path],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic_replay", "build_mode": "benchmark_synthetic_replay"},
    )
    freeze_path = tmp_path / "candidate-freeze.json"
    freeze_candidate(freeze_path, identity)
    return site, contract_path, freeze_path, identity


def _browser_candidate(tmp_path: Path) -> tuple[Path, Path, Path]:
    spec_path, contract_path = _make_bundle(tmp_path)
    contract = compile_synthetic_replay_contract(spec_path, contract_path)
    site = tmp_path / "browser-site"
    (site / "fixtures").mkdir(parents=True)
    (site / "instance").mkdir()
    shutil.copy2(contract_path, site / "synthetic_replay_contract.json")
    shutil.copy2(contract_path.parent / contract["storage"]["seed_path"], site / contract["storage"]["seed_path"])
    shutil.copy2(ROOT / "runtime/webanything_replay_runtime.py", site / "webanything_replay_runtime.py")
    (site / "app.py").write_text('''
import os
import sqlite3
from pathlib import Path
from flask import Flask, jsonify, redirect, request
from webanything_replay_runtime import atomic_seed_reset, effective_prefix, health_payload, scoped_path

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "fixtures/seed.sqlite3"
RUNTIME = ROOT / "instance/runtime.sqlite3"
app = Flask(__name__)

class PrefixMiddleware:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __call__(self, environ, start_response):
        prefix = effective_prefix({"X-Forwarded-Prefix": environ.get("HTTP_X_FORWARDED_PREFIX")})
        path = environ.get("PATH_INFO", "")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            environ["SCRIPT_NAME"] = prefix
            environ["PATH_INFO"] = path[len(prefix):] or "/"
        return self.wrapped(environ, start_response)

app.wsgi_app = PrefixMiddleware(app.wsgi_app)

def ensure_runtime():
    if not RUNTIME.is_file():
        atomic_seed_reset(SEED, RUNTIME)

def state():
    ensure_runtime()
    with sqlite3.connect(RUNTIME) as db:
        signed_in = bool(db.execute("SELECT signed_in FROM users WHERE id='usr_demo'").fetchone()[0])
        db.execute("CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, value INTEGER)")
        saved = db.execute("SELECT value FROM actions WHERE id='saved'").fetchone()
    return {"signed_in": signed_in, "saved": bool(saved and saved[0])}

def update(sql):
    ensure_runtime()
    with sqlite3.connect(RUNTIME) as db:
        db.execute("CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, value INTEGER)")
        db.execute(sql)
        db.commit()

def page(kind, controls):
    return f"<!doctype html><html><body><main data-page='{kind}'>{controls}</main></body></html>"

@app.get("/_health")
def health():
    prefix = effective_prefix(request.headers, request.script_root)
    return jsonify(health_payload(site_id="cloudpost", prefix=prefix))

@app.get("/")
def home():
    current_prefix = effective_prefix(request.headers, request.script_root)
    auth = "<span data-webanything-auth-state='authenticated'>Demo user</span>" if state()["signed_in"] else f"<a data-webanything-auth-entry href='{scoped_path(current_prefix, '/login')}'>Login</a>"
    saved = "<span data-saved='true'>Saved</span>" if state()["saved"] else ""
    controls = auth + saved + f"<form method='post' action='{scoped_path(current_prefix, '/api/logout')}'><button data-action-id='logout'>Logout</button></form><form method='post' action='{scoped_path(current_prefix, '/api/save')}'><button data-action-id='save_item'>Save</button></form>"
    return page("home", controls)

@app.get("/login")
def login_page():
    current_prefix = effective_prefix(request.headers, request.script_root)
    return page("login", f"<form method='post' action='{scoped_path(current_prefix, '/api/login')}'><button data-action-id='login_submit'>Login</button></form>")

@app.post("/api/login")
def login():
    update("UPDATE users SET signed_in=1 WHERE id='usr_demo'")
    return redirect(scoped_path(effective_prefix(request.headers, request.script_root), "/"))

@app.post("/api/logout")
def logout():
    update("UPDATE users SET signed_in=0 WHERE id='usr_demo'")
    return redirect(scoped_path(effective_prefix(request.headers, request.script_root), "/"))

@app.post("/api/save")
def save():
    update("INSERT INTO actions(id,value) VALUES('saved',1) ON CONFLICT(id) DO UPDATE SET value=1")
    return redirect(scoped_path(effective_prefix(request.headers, request.script_root), "/"))

@app.post("/api/reset")
def reset_api():
    atomic_seed_reset(SEED, RUNTIME)
    return jsonify({"status": "reset"})

@app.get("/reset.html")
def reset_page():
    atomic_seed_reset(SEED, RUNTIME)
    return page("reset", "<p>Reset complete</p>")

@app.get("/api/evaluators/<name>")
def evaluator(name):
    current = state()
    passed = {
        "eval_login": current["signed_in"],
        "eval_logout": not current["signed_in"],
        "eval_reset": not current["signed_in"] and not current["saved"],
        "eval_save": current["saved"],
    }.get(name, False)
    return jsonify({"passed": bool(passed)})

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "40000")), debug=False, use_reloader=False)
'''.lstrip(), encoding="utf-8")
    _write_json(site / "replay-provenance.json", {
        "schema": "webanything-replay-provenance-v1",
        "capture_bundle_sha256": contract["capture_manifest"]["bundle_sha256"],
        "seed_sha256": contract["storage"]["seed_sha256"],
        "candidate_files": ["app.py", "webanything_replay_runtime.py"],
    })
    reference_path = contract_path.parent / contract["visual_reference"]["path"]
    identity = candidate_identity(
        site,
        contract_paths=[contract_path],
        reference_paths=[reference_path],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic_replay", "build_mode": "benchmark_synthetic_replay"},
    )
    freeze_path = tmp_path / "browser-candidate-freeze.json"
    freeze_candidate(freeze_path, identity)
    return site, contract_path, freeze_path


def test_replay_browser_verifier_covers_auth_tasks_reset_network_and_immutability(tmp_path: Path):
    site, contract_path, freeze_path = _browser_candidate(tmp_path)
    result = verify_synthetic_replay_candidate(
        site_path=site,
        contract_path=contract_path,
        freeze_path=freeze_path,
        output_path=tmp_path / "browser-verification.json",
        timeout_ms=10000,
    )
    assert result["status"] == "passed", result
    assert {item["workflow"] for item in result["tasks"]} == {"login", "logout", "reset", "stateful_action"}
    assert result["reset"]["byte_identical"] is True
    assert result["network"] == {"unexpected_external_requests": 0, "urls": [], "http_errors": [], "console_errors": [], "resource_errors": []}
    assert result["candidate"]["unchanged"] is True
    schema = json.loads((ROOT / "schemas/synthetic-replay-verification-v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(result)


def test_replay_verifier_rejects_generic_fallback_route(tmp_path: Path):
    site, contract_path, freeze_path = _browser_candidate(tmp_path)
    app_path = site / "app.py"
    source = app_path.read_text(encoding="utf-8").replace(
        "<main data-page='{kind}'>",
        "<main data-page='{kind}' data-webanything-fallback>",
    )
    app_path.write_text(source, encoding="utf-8")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    identity = candidate_identity(
        site,
        contract_paths=[contract_path],
        reference_paths=[contract_path.parent / contract["visual_reference"]["path"]],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic_replay", "build_mode": "benchmark_synthetic_replay"},
    )
    freeze_candidate(freeze_path, identity)
    result = verify_synthetic_replay_candidate(
        site_path=site,
        contract_path=contract_path,
        freeze_path=freeze_path,
        output_path=tmp_path / "fallback-failed.json",
        timeout_ms=10000,
    )
    assert result["status"] == "failed"
    assert result["checks"]["routes"] == "failed"
    assert any(item["code"] == "generic_fallback_detected" for item in result["failures"])


def test_replay_verifier_rejects_evaluator_pass_without_semantic_dom(tmp_path: Path):
    site, contract_path, freeze_path = _browser_candidate(tmp_path)
    app_path = site / "app.py"
    source = app_path.read_text(encoding="utf-8").replace(
        'saved = "<span data-saved=\'true\'>Saved</span>" if state()["saved"] else ""',
        'saved = ""',
    )
    app_path.write_text(source, encoding="utf-8")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    identity = candidate_identity(
        site,
        contract_paths=[contract_path],
        reference_paths=[contract_path.parent / contract["visual_reference"]["path"]],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic_replay", "build_mode": "benchmark_synthetic_replay"},
    )
    freeze_candidate(freeze_path, identity)
    result = verify_synthetic_replay_candidate(
        site_path=site,
        contract_path=contract_path,
        freeze_path=freeze_path,
        output_path=tmp_path / "semantic-failed.json",
        timeout_ms=10000,
    )
    state_task = next(item for item in result["tasks"] if item["workflow"] == "stateful_action")
    assert state_task["status"] == "failed"
    assert result["checks"]["semantic_tasks"] == "failed"
    assert any("DOM visibility" in detail for detail in state_task["failures"])


def test_replay_verifier_rejects_prefixed_route_resource_errors(tmp_path: Path):
    site, contract_path, freeze_path = _browser_candidate(tmp_path)
    app_path = site / "app.py"
    source = app_path.read_text(encoding="utf-8")
    source = source.replace(
        "from flask import Flask, jsonify, redirect, request",
        "from flask import Flask, Response, jsonify, redirect, request",
    )
    source = source.replace(
        '@app.get("/_health")',
        '@app.get("/ok.png")\ndef prefixed_asset():\n    return Response(b"png", mimetype="image/png")\n\n@app.get("/_health")',
    )
    source = source.replace(
        'return page("home", controls)',
        'prefix = effective_prefix(request.headers, request.script_root)\n    if prefix:\n        controls += f"<img src=\'{prefix}{prefix}/ok.png\'>"\n    return page("home", controls)',
        1,
    )
    source = source.replace(
        'if __name__ == "__main__":',
        '@app.get("/<path:unknown>")\ndef fallback(unknown):\n    return page("fallback", "")\n\nif __name__ == "__main__":',
    )
    app_path.write_text(source, encoding="utf-8")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    identity = candidate_identity(
        site,
        contract_paths=[contract_path],
        reference_paths=[contract_path.parent / contract["visual_reference"]["path"]],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic_replay", "build_mode": "benchmark_synthetic_replay"},
    )
    freeze_candidate(freeze_path, identity)

    result = verify_synthetic_replay_candidate(
        site_path=site,
        contract_path=contract_path,
        freeze_path=freeze_path,
        output_path=tmp_path / "prefix-failed-verification.json",
        timeout_ms=10000,
    )
    assert result["status"] == "failed"
    assert result["repairable"] is True
    assert any(item["code"] == "proxy_prefix_browser_errors" for item in result["failures"])
    assert any("/p/40000/p/40000/ok.png" in item for item in result["network"]["resource_errors"])


def test_replay_verifier_rejects_doubled_prefix_in_auth_journey(tmp_path: Path):
    site, contract_path, freeze_path = _browser_candidate(tmp_path)
    app_path = site / "app.py"
    source = app_path.read_text(encoding="utf-8")
    source = source.replace(
        "f\"<a data-webanything-auth-entry href='{scoped_path(current_prefix, '/login')}'>Login</a>\"",
        "f\"<a data-webanything-auth-entry href='{scoped_path(current_prefix, scoped_path(current_prefix, '/login'))}'>Login</a>\"",
    )
    app_path.write_text(source, encoding="utf-8")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    identity = candidate_identity(
        site,
        contract_paths=[contract_path],
        reference_paths=[contract_path.parent / contract["visual_reference"]["path"]],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic_replay", "build_mode": "benchmark_synthetic_replay"},
    )
    freeze_candidate(freeze_path, identity)

    result = verify_synthetic_replay_candidate(
        site_path=site,
        contract_path=contract_path,
        freeze_path=freeze_path,
        output_path=tmp_path / "prefix-auth-failed-verification.json",
        timeout_ms=10000,
    )

    assert result["status"] == "failed"
    failure = next(item for item in result["failures"] if item["code"] == "proxy_prefix_auth_failed")
    assert any("/p/40000/p/40000/login" in detail for detail in failure["details"])


def test_replay_verifier_rejects_evaluator_only_login_without_visible_user_state(tmp_path: Path):
    site, contract_path, freeze_path = _browser_candidate(tmp_path)
    app_path = site / "app.py"
    source = app_path.read_text(encoding="utf-8")
    source = source.replace(
        'auth = "<span data-webanything-auth-state=\'authenticated\'>Demo user</span>" if state()["signed_in"] else f"<a data-webanything-auth-entry href=\'{scoped_path(current_prefix, \'/login\')}\'>Login</a>"',
        'auth = "<span>Demo user</span>" if state()["signed_in"] else "<span>Login</span>"',
    )
    app_path.write_text(source, encoding="utf-8")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    identity = candidate_identity(
        site,
        contract_paths=[contract_path],
        reference_paths=[contract_path.parent / contract["visual_reference"]["path"]],
        configuration={"site": "cloudpost", "profile": "benchmark_synthetic_replay", "build_mode": "benchmark_synthetic_replay"},
    )
    freeze_candidate(freeze_path, identity)

    result = verify_synthetic_replay_candidate(
        site_path=site,
        contract_path=contract_path,
        freeze_path=freeze_path,
        output_path=tmp_path / "auth-surface-failed.json",
        timeout_ms=10000,
    )

    assert result["status"] == "failed"
    failure = next(item for item in result["failures"] if item["code"] == "proxy_prefix_auth_failed")
    assert any("visible" in detail for detail in failure["details"])


def test_replay_verifier_emits_a_bounded_functional_repair_package(tmp_path: Path):
    site, contract_path, freeze_path, _identity = _release_fixture(tmp_path)
    repair_path = tmp_path / "functional-repair.json"
    result = verify_synthetic_replay_candidate(
        site_path=site,
        contract_path=contract_path,
        freeze_path=freeze_path,
        output_path=tmp_path / "failed-verification.json",
        repair_package_path=repair_path,
    )
    package = json.loads(repair_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["repairable"] is True
    assert package["status"] == "repair_required"
    assert package["failure_count"] == sum(len(group["items"]) for group in package["groups"])
    schema = json.loads((ROOT / "pipeline-tools/interaction-verifier-v1/schemas/builder-repair-package-v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(package)


def test_replay_launcher_spools_runtime_logs_instead_of_using_an_unread_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    site = tmp_path / "site"
    site.mkdir()
    (site / "app.py").write_text("# fake runtime\n", encoding="utf-8")
    observed: dict[str, object] = {}

    class FakeProcess:
        def poll(self):
            return None

    fake = FakeProcess()

    def fake_popen(*args, **kwargs):
        observed.update(kwargs)
        return fake

    monkeypatch.setattr(replay_verify.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(replay_verify, "_free_port", lambda: 43210)
    monkeypatch.setattr(replay_verify, "_http_json", lambda *args, **kwargs: {"status": "ok"})
    process, base_url = replay_verify._launch(
        site,
        {"runtime": {"entrypoint": "app.py", "health_path": "/_health"}},
    )
    assert process is fake
    assert base_url == "http://127.0.0.1:43210"
    assert observed["stderr"] is subprocess.STDOUT
    assert observed["stdout"] is not subprocess.PIPE
    assert observed["stdout"].closed is True


def test_replay_verifier_waits_for_async_action_recording(monkeypatch: pytest.MonkeyPatch):
    responses = iter([{"passed": False}, {"passed": False}, {"passed": True}])
    monkeypatch.setattr(replay_verify, "_http_json", lambda _url: next(responses))
    result = replay_verify._wait_for_evaluation(
        "http://127.0.0.1/evaluator",
        result_field="passed",
        timeout_seconds=1,
        poll_seconds=0,
    )
    assert result == {"passed": True}


def test_replay_release_is_hash_bound_and_schema_valid(tmp_path: Path):
    site, contract_path, freeze_path, identity = _release_fixture(tmp_path)
    contract = load_synthetic_replay_contract(contract_path)
    reference_path = contract_path.parent / contract["visual_reference"]["path"]
    verification_path = tmp_path / "replay-verification.json"
    seed_hash = contract["storage"]["seed_sha256"]
    checks = {name: "passed" for name in REPLAY_REQUIRED_GATES.values() if name != "visual_fidelity"}
    _write_json(verification_path, {
        "schema": "webanything-synthetic-replay-verification-v1",
        "status": "passed",
        "site": "cloudpost",
        "build_mode": "benchmark_synthetic_replay",
        "candidate_identity_sha256": identity["sha256"],
        "candidate_sha256": identity["tree"]["sha256"],
        "contract_sha256": sha256_file(contract_path),
        "checks": checks,
        "reset": {"seed_sha256": seed_hash, "first_sha256": seed_hash, "second_sha256": seed_hash, "byte_identical": True},
        "tasks": [{"status": "passed"}],
        "network": {"unexpected_external_requests": 0, "urls": []},
        "candidate": {"pre_hash": identity["tree"], "post_hash": identity["tree"], "unchanged": True},
        "failures": [],
    })
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    visual_path = tmp_path / "visual-verification.json"
    _write_json(visual_path, {
        "schema": "webanything-synthetic-visual-verification-v1",
        "status": "passed",
        "repairable": False,
        "site": "cloudpost",
        "build_mode": "benchmark_synthetic_replay",
        "comparison_mode": "source_visible",
        "contract_sha256": sha256_file(contract_path),
        "reference_manifest_sha256": sha256_file(reference_path),
        "visual_reference_sha256": reference["reference_sha256"],
        "candidate_identity_sha256": identity["sha256"],
        "candidate_sha256": identity["tree"]["sha256"],
        "candidate": {"pre_hash": identity["tree"], "post_hash": identity["tree"], "unchanged": True},
        "routes": [{"route_id": "route_inbox", "status": "passed"}],
        "network": {"unexpected_external_requests": 0, "urls": []},
        "failures": [],
    })
    manifest = release_synthetic_replay_candidate(
        site="cloudpost",
        site_path=site,
        contract_path=contract_path,
        verification_path=verification_path,
        visual_reference_path=reference_path,
        visual_verification_path=visual_path,
        freeze_path=freeze_path,
        output_dir=tmp_path / "release",
    )
    gate = json.loads((tmp_path / "release/release-gate-result.json").read_text(encoding="utf-8"))
    assert manifest["deployment_binding"]["capture_bundle_sha256"] == contract["capture_manifest"]["bundle_sha256"]
    assert manifest["concurrency"] == {"isolation": "isolated_instance", "max_parallel_sessions": 1}
    for filename, value in (("release-gate-result-v1.schema.json", gate), ("release-manifest-v2.schema.json", manifest)):
        schema = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(value)

    stale = json.loads(verification_path.read_text(encoding="utf-8"))
    stale["candidate_identity_sha256"] = "0" * 64
    _write_json(verification_path, stale)
    with pytest.raises(ReleaseError, match="synthetic replay release gate failed"):
        release_synthetic_replay_candidate(
            site="cloudpost",
            site_path=site,
            contract_path=contract_path,
            verification_path=verification_path,
            visual_reference_path=reference_path,
            visual_verification_path=visual_path,
            freeze_path=freeze_path,
            output_dir=tmp_path / "stale-release",
        )
