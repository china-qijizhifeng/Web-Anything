from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

import pipeline_adapter
import terminal
from fixture import prepare_workspace, running_fixture

def _browser_path() -> Path:
    explicit = os.environ.get("WEBANYTHING_TEST_BROWSER")
    if explicit:
        return Path(explicit)
    browser_root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "~/.cache/ms-playwright")).expanduser()
    matches = sorted(browser_root.glob("chromium_headless_shell-*/chrome-linux/headless_shell"))
    matches.extend(sorted(browser_root.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))
    matches.extend(sorted(browser_root.glob("chromium-*/chrome-linux/chrome")))
    matches.extend(sorted(browser_root.glob("chromium-*/chrome-linux64/chrome")))
    if matches:
        return matches[0]
    executable = shutil.which("chromium") or shutil.which("chromium-browser")
    return Path(executable or "/missing-webanything-test-browser")


BROWSER = _browser_path()
DEPENDENCIES = Path(os.environ.get("WEBHARBOR_BUILDER_DEPENDENCY_PATH", sys.prefix))


def _args(workspace: Path, contract: Path, evidence: Path, base_url: str) -> Namespace:
    return Namespace(
        candidate_workspace=workspace,
        site_path=workspace / "sites/demo",
        site="demo",
        contract=contract,
        evidence_dir=evidence,
        base_url=base_url,
        launch_config=None,
        viewport_width=1440,
        viewport_height=1000,
        gateway_prefix=None,
        browser_path=BROWSER,
        dependency_path=DEPENDENCIES,
        temp_dir=None,
        max_pages=None,
        max_depth=None,
        timeout_ms=5000,
        route_map_timeout_seconds=10,
        launch_timeout=10.0,
    )


def test_arbitrary_cwd_cli_and_terminal_result(tmp_path: Path):
    workspace = tmp_path / "candidate"
    contract = prepare_workspace(workspace)
    evidence = tmp_path / "evidence"
    script = (ROOT / "scripts/terminal.py").resolve()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(BROWSER.parent.parent)),
        "WEBHARBOR_BUILDER_DEPENDENCY_PATH": str(DEPENDENCIES),
    }
    with running_fixture() as base_url:
        command = [
            sys.executable,
            str(script),
            "--candidate-workspace", str(workspace),
            "--site-path", str(workspace / "sites/demo"),
            "--site", "demo",
            "--contract", str(contract),
            "--base-url", base_url,
            "--evidence-dir", str(evidence),
            "--browser-path", str(BROWSER),
            "--dependency-path", str(DEPENDENCIES),
            "--viewport-width", "1440", "--viewport-height", "1000",
        ]
        first = subprocess.run(command, cwd=tmp_path, env=env, text=True, capture_output=True, check=False)
        second = subprocess.run(command, cwd=tmp_path, env=env, text=True, capture_output=True, check=False)
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout)["status"] == "passed"
    assert json.loads(first.stdout) == json.loads(second.stdout)
    result = json.loads((evidence / "terminal-result.json").read_text())
    assert result["candidate"]["unchanged"] is True
    assert result["repair_package"]["status"] == "empty"


def test_invalid_input_writes_hash_bound_repair_without_phantom_artifacts(tmp_path: Path):
    workspace = tmp_path / "candidate"
    contract = prepare_workspace(workspace)
    contract.write_text(json.dumps({
        "schema": "webanything-site-contract-v1",
        "site": "demo",
        "routes": [],
    }), encoding="utf-8")
    evidence = tmp_path / "evidence"
    checkpoint = tmp_path / "checkpoints"
    script = (ROOT / "scripts/terminal.py").resolve()
    completed = subprocess.run([
        sys.executable,
        str(script),
        "--candidate-workspace", str(workspace),
        "--site-path", str(workspace / "sites/demo"),
        "--site", "demo",
        "--contract", str(contract),
        "--base-url", "http://127.0.0.1:9/",
        "--evidence-dir", str(evidence),
        "--viewport-width", "1440",
        "--viewport-height", "1000",
        "--batch-size", "2",
        "--checkpoint-dir", str(checkpoint),
    ], cwd=tmp_path, text=True, capture_output=True, check=False)
    assert completed.returncode == 2
    result = json.loads((evidence / "terminal-result.json").read_text())
    repair = json.loads((evidence / "builder-repair-package.json").read_text())
    assert result["status"] == "invalid_input"
    assert result["candidate"]["unchanged"] is True
    assert len(result["candidate_tree_sha256"]) == 64
    assert result["candidate_tree_sha256"] != "0" * 64
    assert len(result["input_contract"]["sha256"]) == 64
    assert set(result["artifacts"]) == {"builder-repair-package.json"}
    assert "closure" not in result
    assert repair["status"] == "repair_required"
    assert repair["candidate_fingerprint"]["sha256"] == result["candidate_tree_sha256"]
    assert repair["groups"][0]["items"][0]["expected_correction"]
    for artifact in result["artifacts"].values():
        assert Path(artifact["path"]).is_file()


def test_no_legacy_skill_dependency():
    forbidden = ("skill-proposal", ".agents/skills", "iterate_sdk_preview", "site_fingerprint")
    for path in (ROOT / "scripts").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), path


def test_candidate_immutability_and_isolated_output(tmp_path: Path):
    workspace = tmp_path / "candidate"
    contract = prepare_workspace(workspace)
    before = terminal._tree_identity(workspace / "sites/demo")
    with running_fixture() as base_url:
        result = terminal.run(_args(workspace, contract, tmp_path / "evidence", base_url))
    assert result["status"] == "passed"
    assert result["candidate"]["pre_hash"] == result["candidate"]["post_hash"] == before
    with running_fixture() as base_url:
        sibling = terminal.run(_args(workspace, contract, workspace / "evidence", base_url))
    assert sibling["candidate"]["unchanged"] is True
    with running_fixture() as base_url, pytest.raises(ValueError, match="candidate site"):
        terminal.run(_args(workspace, contract, workspace / "sites/demo/evidence", base_url))


def test_batch_spec_is_generic_explicit_and_unique(tmp_path: Path):
    browser = tmp_path / "chromium"
    browser.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    browser.chmod(0o755)
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    workers = []
    for index, site in enumerate(("demo_one", "demo_two"), start=1):
        workspace = tmp_path / site / "workspace"
        contract = prepare_workspace(workspace)
        source_site = workspace / "sites/demo"
        site_path = workspace / f"sites/{site}"
        source_site.rename(site_path)
        contract = site_path / contract.name
        workers.append({
            "site": site,
            "workspace": str(workspace.resolve()),
            "site_path": str(site_path.resolve()),
            "contract": str(contract.resolve()),
            "evidence_dir": str((tmp_path / site / "evidence").resolve()),
            "browser_path": str(browser.resolve()),
            "dependency_path": str(dependencies.resolve()),
            "viewport": {"width": 1440, "height": 1000},
            "budgets": {"max_pages": 12, "max_depth": 2, "timeout_ms": 10000},
            "launch": {
                "command": [sys.executable, "sites/demo/app.py"],
                "cwd": str(workspace.resolve()),
                "base_url": f"http://127.0.0.1:{43100 + index}/",
                "env": {},
            },
        })
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(json.dumps({
        "schema_version": 1,
        "batch": "webanything-interaction-verifier-batch-v1",
        "workers": workers,
    }), encoding="utf-8")

    spec = pipeline_adapter.load_batch_spec(spec_path)
    assert [item["site"] for item in spec["workers"]] == ["demo_one", "demo_two"]
    assert len({item["evidence_dir"] for item in spec["workers"]}) == 2
    assert len({item["launch"]["base_url"] for item in spec["workers"]}) == 2
