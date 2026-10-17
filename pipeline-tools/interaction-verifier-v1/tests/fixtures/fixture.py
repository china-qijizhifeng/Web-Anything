"""Real HTTP fixture and contract builder for immersive verifier tests."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SOURCE_RELATIVE = "mirror_factory/evidence/demo/source.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _page(
    *,
    title: str,
    context: str,
    main: str,
    return_id: str,
    return_href: str,
    return_text: str,
    canonical: str | None = None,
) -> str:
    canonical_link = f"<link id='canonical' rel='canonical' href='{canonical}'>" if canonical else ""
    return f"""<!doctype html>
<html><head><title>{title}</title>{canonical_link}</head><body>
<nav id="context" aria-label="Breadcrumb">{context}</nav>
<h1>{title}</h1>
<main>{main}<p>{'grounded fixture body ' * 8}</p></main>
<a id="{return_id}" href="{return_href}">{return_text}</a>
</body></html>"""


class ImmersiveFixtureHandler(BaseHTTPRequestHandler):
    mode = "passing"
    gateway_prefix = ""

    def _local(self, logical: str) -> str:
        return f"{self.gateway_prefix}{logical}" if self.gateway_prefix else logical

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if self.gateway_prefix:
            if path == self.gateway_prefix:
                path = "/"
            elif path.startswith(self.gateway_prefix + "/"):
                path = path[len(self.gateway_prefix):]
            else:
                self._respond(404, "<h1>Missing gateway route</h1>")
                return
        if path == "/":
            extra = ""
            if self.mode == "dead":
                extra += f"<a id='dead' href='{self._local('/missing')}'>Broken destination</a>"
            if self.mode == "inert":
                extra += "<button id='inert'>Open inert panel</button><div id='inert-panel' hidden>Panel</div>"
            if self.mode == "external":
                extra += "<img alt='unexpected public pixel' src='https://public.example.invalid/pixel.png'>"
            if self.mode == "upstream_nav":
                extra += "<button id='upstream-nav' onclick=\"location.href='https://upstream.example.invalid/private'\">Open upstream</button>"
            if self.mode == "source_omitted":
                extra += f"<a id='source-only' href='{self._local('/catalog')}'>Source only</a>"
            if self.mode == "browser_injected":
                extra += f"<a id='injected' href='{self._local('/catalog')}'>Injected control</a>"
            if self.mode == "js_external":
                extra += "<button id='js-external' onclick=\"location.href='https://outside.example.invalid/path'\">External JS</button>"
            if self.mode == "dynamic_target":
                extra += f"<a id='dynamic' href='{self._local('/catalog')}'>Dynamic target</a>"
            if self.mode == "missing_route":
                extra += f"<a id='ghost' href='{self._local('/ghost')}'>Ghost page</a>"
            body = f"""
<article class="product-card"><a id="catalog" href="{self._local('/catalog')}">Browse catalog</a></article>
<button id="menu" aria-haspopup="menu" aria-expanded="false" aria-controls="menu-panel">Products</button>
<nav id="menu-panel" hidden><a id="menu-detail" href="{self._local('/detail')}">Featured detail</a></nav>
<form id="search-form" aria-label="Search catalog" action="{self._local('/search')}" method="get">
  <label for="query">Query</label><input id="query" name="q" type="search">
  <button id="search-submit" type="submit">Search</button>
</form>
<a id="popup" target="_blank" href="{self._local('/popup')}">Open reader</a>
<button id="toggle">Show details</button><section id="state-panel" hidden>Local state changed naturally.</section>
{extra}
<script>
document.getElementById('menu').addEventListener('click', () => {{
  const button = document.getElementById('menu');
  const panel = document.getElementById('menu-panel');
  const open = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', String(open)); panel.hidden = !open;
}});
document.getElementById('toggle').addEventListener('click', () => {{
  document.getElementById('state-panel').hidden = false;
}});
</script>
"""
            html = _page(
                title="Immersive Fixture",
                context="Home experience",
                main=body,
                return_id="catalog",
                return_href=self._local("/catalog"),
                return_text="Browse catalog",
            )
            # _page emits the return selector a second time for the home page;
            # remove that duplicate because one semantic control must map to one
            # contract entry.
            duplicate = f'<a id="catalog" href="{self._local("/catalog")}">Browse catalog</a>\n</body>'
            html = html.replace(duplicate, "</body>")
            self._respond(200, html)
            return
        if path == "/catalog":
            if self.mode == "generic":
                title, context, main = "Generic Surface", "Generic context", "Generic destination content."
            else:
                title, context, main = "Catalog", "Home / Catalog", "A grounded product listing with local navigation."
            self._respond(200, _page(
                title=title,
                context=context,
                main=main,
                return_id="catalog-return",
                return_href=self._local("/"),
                return_text="Return home",
            ))
            return
        if path == "/detail":
            if self.mode == "generic":
                title, context, main = "Generic Surface", "Generic context", "Generic destination content."
            else:
                title, context, main = "Featured Detail", "Home / Featured", "Grounded detail fields and related context."
            if self.mode == "defensive":
                main += " This operation is unsupported and not available in this mirror."
            self._respond(200, _page(
                title=title,
                context=context,
                main=main,
                return_id="detail-return",
                return_href=self._local("/"),
                return_text="Return home",
            ))
            return
        if path == "/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            self._respond(200, _page(
                title=f"Search results for {query}",
                context="Home / Search",
                main=f"Deterministic local results for query {query}.",
                return_id="search-return",
                return_href=self._local("/"),
                return_text="Return home",
            ))
            return
        if path == "/popup":
            self._respond(200, _page(
                title="Local Reader",
                context="Home / Reader",
                main="A meaningful locally hosted reader opened in a popup.",
                return_id="popup-return",
                return_href=self._local("/"),
                return_text="Return home",
            ))
            return
        if path == "/ghost" and self.mode == "missing_route":
            self._respond(200, _page(
                title="Ghost Page",
                context="Home / Ghost",
                main="The browser fixture serves a meaningful page absent from Flask.",
                return_id="ghost-return",
                return_href=self._local("/"),
                return_text="Return home",
            ))
            return
        self._respond(404, "<!doctype html><html><body><h1>Missing fixture route</h1></body></html>")

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


@contextmanager
def running_fixture(*, mode: str = "passing", gateway: bool = False):
    handler = type(
        "ConfiguredImmersiveFixtureHandler",
        (ImmersiveFixtureHandler,),
        {"mode": mode, "gateway_prefix": "/p/43123" if gateway else ""},
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except PermissionError as exc:
        pytest.skip(f"managed test sandbox denies loopback socket creation: {exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    if gateway:
        root += "/p/43123/"
    try:
        yield root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def prepare_workspace(root: Path, *, mode: str = "passing", max_pages: int = 5) -> Path:
    site = root / "sites" / "demo"
    site.mkdir(parents=True)
    templates = site / "templates"
    templates.mkdir()
    app_routes = [
        ("/", "home", "home.html"),
        ("/catalog", "catalog", "catalog.html"),
        ("/detail", "detail", "detail.html"),
        ("/search", "search", "search.html"),
        ("/popup", "popup", "popup.html"),
    ]
    if mode == "orphan_route":
        app_routes.append(("/orphan", "orphan", "orphan.html"))
    app_source = ["from flask import Flask, render_template", "", "app = Flask(__name__)", ""]
    for route, endpoint, template in app_routes:
        app_source.extend([
            f"@app.get({route!r})",
            f"def {endpoint}():",
            f"    return render_template({template!r})",
            "",
        ])
    (site / "app.py").write_text("\n".join(app_source), encoding="utf-8")

    catalog_href = (
        "/catalog?view=grid" if mode == "query_mismatch"
        else "{{ destination }}" if mode == "dynamic_proved"
        else "/catalog"
    )
    form_method = "post" if mode == "method_mismatch" else "get"
    source_extra = ""
    if mode == "dead":
        source_extra += "<a id='dead' href='/missing'>Broken destination</a>"
    if mode == "inert":
        source_extra += "<button id='inert'>Open inert panel</button>"
    if mode == "upstream_nav":
        source_extra += "<button id='upstream-nav' onclick=\"location.href='https://upstream.example.invalid/private'\">Open upstream</button>"
    if mode == "source_removed":
        source_extra += "<a id='removed' href='/catalog'>Removed source control</a>"
    if mode == "source_omitted":
        source_extra += "<a id='source-only' href='/catalog'>Source only</a>"
    if mode == "js_external":
        source_extra += "<button id='js-external'>External JS</button>"
    if mode == "dynamic_target":
        source_extra += "<a id='dynamic' href='{{ destination }}'>Dynamic target</a>"
    if mode == "missing_route":
        source_extra += "<a id='ghost' href='/ghost'>Ghost page</a>"
    home_template = f"""<!doctype html><html><body>
<nav id="context">Home experience</nav><h1>Immersive Fixture</h1><main>
<article class="product-card"><a id="catalog" href="{catalog_href}">Browse catalog</a></article>
<button id="menu" aria-haspopup="menu" aria-expanded="false" aria-controls="menu-panel">Products</button>
<nav id="menu-panel" hidden><a id="menu-detail" href="/detail">Featured detail</a></nav>
<form id="search-form" aria-label="Search catalog" action="/search" method="{form_method}">
<label for="query">Query</label><input id="query" name="q" type="search">
<button id="search-submit" type="submit">Search</button></form>
<a id="popup" target="_blank" href="/popup">Open reader</a>
<button id="toggle">Show details</button><section id="state-panel" hidden>Local state changed naturally.</section>
{source_extra}
</main><script>
document.getElementById('menu').addEventListener('click', () => {{
  const button = document.getElementById('menu');
  const panel = document.getElementById('menu-panel');
  const open = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', String(open)); panel.hidden = !open;
}});
document.getElementById('toggle').addEventListener('click', () => {{
  document.getElementById('state-panel').hidden = false;
}});
{"document.getElementById('js-external').addEventListener('click', () => { location.href = 'https://outside.example.invalid/path'; });" if mode == "js_external" else ""}
</script></body></html>"""
    (templates / "home.html").write_text(home_template, encoding="utf-8")
    template_specs = [
        ("catalog", "Catalog", "catalog-return"),
        ("detail", "Featured Detail", "detail-return"),
        ("search", "Search results", "search-return"),
        ("popup", "Local Reader", "popup-return"),
    ]
    if mode == "orphan_route":
        template_specs.append(("orphan", "Orphan", "orphan-return"))
    for name, title, return_id in template_specs:
        (templates / f"{name}.html").write_text(
            f"<!doctype html><html><body><nav id='context'>Home / {title}</nav><h1>{title}</h1>"
            f"<main>{'grounded source template content ' * 4}</main>"
            f"<a id='{return_id}' href='/'>Return home</a></body></html>",
            encoding="utf-8",
        )
    source = root / SOURCE_RELATIVE
    source.parent.mkdir(parents=True)
    source.write_text('{"source":"grounded fixture"}\n', encoding="utf-8")
    provenance = {
        "kind": "captured",
        "sources": [{"path": SOURCE_RELATIVE, "sha256": _sha256(source)}],
    }

    routes = [
        ("home", "/", "home", "landing", "#catalog", "/catalog"),
        ("catalog", "/catalog", "catalog", "listing", "#catalog-return", "/"),
        ("detail", "/detail", "catalog", "detail", "#detail-return", "/"),
        ("search", "/search?q=hello", "search", "search_results", "#search-return", "/"),
        ("popup", "/popup", "reader", "reader", "#popup-return", "/"),
    ]
    if mode == "missing_route":
        routes.append(("ghost", "/ghost", "ghost", "detail", "#ghost-return", "/"))
    route_inventory = []
    for route_id, path, family, archetype, return_selector, expected_return in routes:
        route_inventory.append({
            "id": route_id,
            "path": path,
            "family": family,
            "archetype": archetype,
            "critical": True,
            "provenance": provenance,
            "minimum_fields": {
                "title": {"selector": "h1", "min_chars": 3},
                "context": {"selector": "#context", "min_chars": 3},
                "main": {"selector": "main", "min_chars": 50},
                "return": {
                    "selector": return_selector,
                    "min_chars": 3,
                    "expected_path": expected_return,
                },
            },
        })

    controls = [
        {"id": "catalog-card", "source_path": "/", "selector": "#catalog", "accessible_name": "Browse catalog", "control_type": "card", "critical": True, "outcome": {"kind": "navigation", "expected_path": "/catalog"}},
        {"id": "products-menu", "source_path": "/", "selector": "#menu", "accessible_name": "Products", "control_type": "menu", "critical": True, "outcome": {"kind": "state_change", "assertions": [{"kind": "attribute", "selector": "#menu", "name": "aria-expanded", "value": "true"}, {"kind": "visible", "selector": "#menu-panel"}]}},
        {"id": "featured-detail", "source_path": "/", "selector": "#menu-detail", "accessible_name": "Featured detail", "control_type": "link", "critical": True, "reveal": ["#menu"], "outcome": {"kind": "navigation", "expected_path": "/detail"}},
        {"id": "search-form", "source_path": "/", "selector": "#search-form", "accessible_name": "Search catalog", "control_type": "form", "critical": True, "outcome": {"kind": "navigation", "action": "submit", "submit_selector": "#search-submit", "steps": [{"action": "fill", "selector": "#query", "value": "hello"}], "expected_path": "/search?q=hello"}},
        {"id": "search-input", "source_path": "/", "selector": "#query", "accessible_name": "Query", "control_type": "search", "critical": True, "outcome": {"kind": "input_state", "action": "fill", "value": "hello", "assertions": [{"kind": "value", "selector": "#query", "value": "hello"}]}},
        {"id": "search-submit", "source_path": "/", "selector": "#search-submit", "accessible_name": "Search", "control_type": "button", "critical": True, "outcome": {"kind": "navigation", "action": "click", "steps": [{"action": "fill", "selector": "#query", "value": "hello"}], "expected_path": "/search?q=hello"}},
        {"id": "popup-reader", "source_path": "/", "selector": "#popup", "accessible_name": "Open reader", "control_type": "popup", "critical": True, "outcome": {"kind": "popup", "expected_path": "/popup"}},
        {"id": "local-state", "source_path": "/", "selector": "#toggle", "accessible_name": "Show details", "control_type": "state_change", "critical": True, "outcome": {"kind": "state_change", "assertions": [{"kind": "visible", "selector": "#state-panel"}, {"kind": "text_contains", "selector": "#state-panel", "value": "Local state changed"}]}},
        {"id": "catalog-return", "source_path": "/catalog", "selector": "#catalog-return", "accessible_name": "Return home", "control_type": "link", "critical": True, "outcome": {"kind": "navigation", "expected_path": "/"}},
        {"id": "detail-return", "source_path": "/detail", "selector": "#detail-return", "accessible_name": "Return home", "control_type": "link", "critical": True, "outcome": {"kind": "navigation", "expected_path": "/"}},
        {"id": "search-return", "source_path": "/search?q=hello", "selector": "#search-return", "accessible_name": "Return home", "control_type": "link", "critical": True, "outcome": {"kind": "navigation", "expected_path": "/"}},
        {"id": "popup-return", "source_path": "/popup", "selector": "#popup-return", "accessible_name": "Return home", "control_type": "link", "critical": True, "outcome": {"kind": "navigation", "expected_path": "/"}},
    ]
    if mode == "inert":
        controls.append({
            "id": "inert-control",
            "source_path": "/",
            "selector": "#inert",
            "accessible_name": "Open inert panel",
            "control_type": "state_change",
            "critical": True,
            "outcome": {
                "kind": "state_change",
                "assertions": [{"kind": "visible", "selector": "#inert-panel"}],
            },
        })
    if mode == "upstream_nav":
        controls.append({
            "id": "upstream-navigation",
            "source_path": "/",
            "selector": "#upstream-nav",
            "accessible_name": "Open upstream",
            "control_type": "state_change",
            "critical": True,
            "outcome": {
                "kind": "state_change",
                "assertions": [{"kind": "visible", "selector": "main"}],
            },
        })
    if mode == "source_removed":
        controls.append({
            "id": "removed-source-control",
            "source_path": "/",
            "selector": "#removed",
            "accessible_name": "Removed source control",
            "control_type": "link",
            "critical": True,
            "outcome": {"kind": "navigation", "expected_path": "/catalog"},
        })
    if mode == "js_external":
        controls.append({
            "id": "js-external",
            "source_path": "/",
            "selector": "#js-external",
            "accessible_name": "External JS",
            "control_type": "state_change",
            "critical": True,
            "outcome": {"kind": "state_change", "assertions": [{"kind": "visible", "selector": "main"}]},
        })
    if mode == "missing_route":
        controls.extend([
            {
                "id": "ghost-link",
                "source_path": "/",
                "selector": "#ghost",
                "accessible_name": "Ghost page",
                "control_type": "link",
                "critical": True,
                "outcome": {"kind": "navigation", "expected_path": "/ghost"},
            },
            {
                "id": "ghost-return",
                "source_path": "/ghost",
                "selector": "#ghost-return",
                "accessible_name": "Return home",
                "control_type": "link",
                "critical": True,
                "outcome": {"kind": "navigation", "expected_path": "/"},
            },
        ])
    edges = [
        {"from": control["source_path"], "to": control["outcome"]["expected_path"], "control_id": control["id"]}
        for control in controls
        if control["outcome"]["kind"] in {"navigation", "popup"}
    ]
    contract = {
        "schema_version": 1,
        "contract": "webharbor-immersive-interaction-contract-v1",
        "profile": "immersive",
        "site": "demo",
        "minimum_body_chars": 80,
        "viewport": {"width": 1440, "height": 1000},
        "crawl": {"max_pages": max_pages, "max_depth": 2, "start_routes": ["/"]},
        "namespaces": [{
            "kind": "primary",
            "source_origin": "https://fixture.example.test",
            "local_prefix": "/",
            "provenance": provenance,
        }],
        "route_inventory": route_inventory,
        "control_inventory": controls,
        "route_graph": edges,
        "critical_workflows": [{
            "id": "complete-fixture-experience",
            "routes": [item["path"] for item in route_inventory],
            "controls": [item["id"] for item in controls],
        }],
        "synthetic_records": [],
    }
    contract_path = site / "interaction_contract.json"
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return contract_path
