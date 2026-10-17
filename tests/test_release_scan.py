from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_scan import scan


def test_release_scan_accepts_plain_source(tmp_path: Path):
    (tmp_path / "README.md").write_text("public source\n", encoding="utf-8")
    assert scan(tmp_path)["status"] == "passed"


def test_release_scan_rejects_machine_path(tmp_path: Path):
    machine_path = "/Users/" + "xietianci/private\n"
    (tmp_path / "bad.md").write_text(machine_path, encoding="utf-8")
    result = scan(tmp_path)
    assert result["status"] == "failed"
    assert result["failures"][0]["path"] == "bad.md"


def test_release_scan_rejects_encrypted_auth_profile_by_filename(tmp_path: Path):
    (tmp_path / "cloudpost.auth-profile.enc.json").write_text("{}\n", encoding="utf-8")
    result = scan(tmp_path)
    assert result["status"] == "failed"
    assert result["failures"][0]["reason"] == "forbidden release file"


def test_release_scan_rejects_private_stage_logs(tmp_path: Path):
    path = tmp_path / "stage-logs/demo"
    path.mkdir(parents=True)
    (path / "build.stdout.log").write_text("diagnostic\n", encoding="utf-8")
    result = scan(tmp_path)
    assert result["status"] == "failed"
    assert any(item["path"] == "stage-logs" for item in result["failures"])
