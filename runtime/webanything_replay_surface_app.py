"""Orchestrator-seeded Flask base that serves frozen captured DOM verbatim.

Builder extends this application with local SQLite semantics. Captured route
handlers are the visual authority and must not be replaced by model-authored
layouts.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from runtime.webanything_replay_runtime import effective_prefix, health_payload, scope_html_urls


BASE = Path(__file__).resolve().parent
CONTRACT = json.loads((BASE / "synthetic_replay_contract.json").read_text(encoding="utf-8"))
SITE_ID = CONTRACT["site_id"]


class PrefixMiddleware:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        match = re.match(r"^(/p/[0-9]+)(?=/|$)", path)
        if match:
            prefix = match.group(1)
            environ["SCRIPT_NAME"] = prefix
            environ["PATH_INFO"] = path[len(prefix) :] or "/"
            environ["HTTP_X_FORWARDED_PREFIX"] = prefix
        return self.application(environ, start_response)


app = Flask(__name__)
app.wsgi_app = PrefixMiddleware(app.wsgi_app)


def render_captured_route(route: dict[str, object]) -> Response:
    """Return the captured route surface; Builder may inject semantics here."""
    route_id = str(route["id"])
    html = (BASE / "pages" / f"{route_id}.html").read_text(encoding="utf-8")
    if "data-route-kind=" not in html:
        kind = str(route["kind"])
        html = re.sub(r"<body\b", f'<body data-route-kind="{kind}"', html, count=1)
    prefix = effective_prefix(request.headers, request.environ.get("SCRIPT_NAME"))
    base_href = f"{prefix}/" if prefix else "/"
    html = re.sub(r"(<head\b[^>]*>)", rf'\1<base href="{base_href}">', html, count=1)
    return Response(scope_html_urls(html, prefix), mimetype="text/html")


def _register_captured_routes() -> None:
    for route in CONTRACT["routes"]:
        if route.get("source") != "captured":
            continue
        local_path = str(route["path"]).split("?", 1)[0]
        endpoint = f"captured_{route['id']}"
        app.add_url_rule(
            local_path,
            endpoint,
            lambda route=route: render_captured_route(route),
            methods=["GET"],
        )


_register_captured_routes()


@app.get("/_health")
def health():
    prefix = effective_prefix(request.headers, request.environ.get("SCRIPT_NAME"))
    return jsonify(health_payload(site_id=SITE_ID, prefix=prefix))


@app.get("/assets/<path:name>")
def assets(name: str):
    return send_from_directory(BASE / "assets", name)


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
