from __future__ import annotations

import socket
from pathlib import Path

from webanything.runtime import SiteManager, create_control_app


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_control_reset_restores_seed_and_starts_site(tmp_path: Path) -> None:
    sites_root = tmp_path / "sites"
    site_dir = sites_root / "sample"
    seed_dir = site_dir / "instance_seed"
    runtime_dir = site_dir / "instance"
    seed_dir.mkdir(parents=True)
    runtime_dir.mkdir()
    (seed_dir / "state.txt").write_text("clean\n", encoding="utf-8")
    (runtime_dir / "state.txt").write_text("dirty\n", encoding="utf-8")
    (site_dir / "app.py").write_text(
        "from flask import Flask, jsonify\n"
        "app = Flask(__name__)\n"
        "@app.get('/')\n"
        "def index(): return 'ready'\n"
        "@app.get('/_health')\n"
        "def health(): return jsonify(ok=True, site='sample')\n",
        encoding="utf-8",
    )

    runtime_path = Path(__file__).resolve().parents[1] / "src/webanything/runtime.py"
    manager = SiteManager(
        [{"id": "sample", "port": available_port()}],
        sites_root=sites_root,
        host="127.0.0.1",
        runtime_path=runtime_path,
    )
    try:
        client = create_control_app(manager).test_client()
        response = client.post("/reset/sample")
        assert response.status_code == 200
        assert response.get_json()["ready"] is True
        assert (runtime_dir / "state.txt").read_text(encoding="utf-8") == "clean\n"

        health = client.get("/health")
        assert health.status_code == 200
        assert health.get_json()["sites"]["sample"]["alive"] is True
    finally:
        manager.stop_all()
