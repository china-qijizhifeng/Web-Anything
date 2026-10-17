"""The frozen catalog must be portable and bind to exact application bytes."""

import copy
import hashlib
import json
from pathlib import Path

from scripts.verify_site_inventory import DEFAULT_MANIFEST, verify


def test_frozen_manifest_and_artifact_hashes(tmp_path: Path) -> None:
    original = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    assert original["site_count"] == 244
    assert len({site["site_id"] for site in original["sites"]}) == 244
    assert verify(DEFAULT_MANIFEST)["ok"]

    manifest = copy.deepcopy(original)
    for site in manifest["sites"]:
        app = tmp_path / site["artifact_relpath"] / "app.py"
        app.parent.mkdir(parents=True, exist_ok=True)
        content = f"# {site['site_id']}\n".encode()
        app.write_bytes(content)
        site["app_sha256"] = hashlib.sha256(content).hexdigest()

    fixture = tmp_path / "manifest.json"
    fixture.write_text(json.dumps(manifest), encoding="utf-8")
    matched = verify(fixture, tmp_path)
    assert matched["ok"] and matched["artifact_hashes_checked"] == 244

    first = manifest["sites"][0]
    (tmp_path / first["artifact_relpath"] / "app.py").write_text("tampered")
    mismatch = verify(fixture, tmp_path)
    assert not mismatch["ok"]
    assert mismatch["artifact_hashes_checked"] == 243
    assert any("SHA-256 mismatch" in error for error in mismatch["errors"])
