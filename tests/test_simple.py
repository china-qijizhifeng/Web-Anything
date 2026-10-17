from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from webanything import simple
from scripts import coupled_pipeline


def test_configure_keeps_key_private_and_create_passes_it_only_in_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    config = tmp_path / "private/provider.json"
    monkeypatch.setenv("WEBANYTHING_CONFIG", str(config))
    for name in ("WEBANYTHING_API_URL", "WEBANYTHING_API_KEY", "WEBANYTHING_RESPONSES_BASE_URL", "WEBANYTHING_RESPONSES_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEST_WEBANYTHING_KEY", "test-secret-value")
    assert simple.configure(Namespace(api_url="https://provider.example/v1", api_key_env="TEST_WEBANYTHING_KEY")) == 0
    assert config.stat().st_mode & 0o777 == 0o600
    assert simple.provider() == ("https://provider.example/v1", "test-secret-value")
    assert "test-secret-value" not in capsys.readouterr().out

    observed = {}

    def fake_run(command, *, cwd, env, check):
        observed.update(command=command, cwd=cwd, env=env)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(simple.subprocess, "run", fake_run)
    args = Namespace(url="https://example.org/docs/", mode="jit", task="Find the second example",
                     run_dir=None, model="gpt-5.6-sol", reasoning_effort="high",
                     timeout_minutes=90, max_cycles=12, initialize_only=False)
    assert simple.create(args, tmp_path) == 0
    assert "--mode" in observed["command"]
    assert observed["command"][observed["command"].index("--mode") + 1] == "jit"
    assert "--task-query" in observed["command"]
    assert "test-secret-value" not in " ".join(observed["command"])
    assert observed["env"]["WEBANYTHING_RESPONSES_API_KEY"] == "test-secret-value"


def test_setup_installs_agent_skills_and_create_uses_selected_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    monkeypatch.setenv("WEBANYTHING_CONFIG", str(home / "config/provider.json"))
    monkeypatch.setenv("TEST_WEBANYTHING_KEY", "test-secret-value")
    for name in ("WEBANYTHING_MODEL", "WEBANYTHING_API_URL", "WEBANYTHING_API_KEY",
                 "WEBANYTHING_RESPONSES_BASE_URL", "WEBANYTHING_RESPONSES_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    args = Namespace(agent="both", api_url="https://provider.example/v1",
                     api_key_env="TEST_WEBANYTHING_KEY", model="provider-model")
    assert simple.setup(args, tmp_path) == 0
    config = home / "config/provider.json"
    assert config.stat().st_mode & 0o777 == 0o600
    assert json.loads(config.read_text())["model"] == "provider-model"
    for path in (home / "codex/skills/webanything/SKILL.md",
                 home / ".claude/skills/webanything/SKILL.md"):
        skill = path.read_text()
        assert "name: webanything" in skill
        assert str(tmp_path) in skill
        assert "--mode jit --task" in skill
        assert "test-secret-value" not in skill
    assert "test-secret-value" not in capsys.readouterr().out

    observed = {}

    def fake_run(command, *, cwd, env, check):
        observed.update(command=command, env=env)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(simple.subprocess, "run", fake_run)
    create_args = Namespace(url="https://example.org/", mode="jit", task="Open the second example",
                            run_dir=None, model=None, reasoning_effort="high",
                            timeout_minutes=90, max_cycles=12, initialize_only=False)
    assert simple.create(create_args, tmp_path) == 0
    assert observed["command"][observed["command"].index("--model") + 1] == "provider-model"
    assert observed["env"]["WEBANYTHING_RESPONSES_API_KEY"] == "test-secret-value"


def test_setup_preserves_unrelated_agent_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    skill = home / "codex/skills/webanything/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("unrelated skill\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    monkeypatch.setenv("WEBANYTHING_CONFIG", str(home / "provider.json"))
    monkeypatch.setenv("TEST_WEBANYTHING_KEY", "test-secret-value")
    args = Namespace(agent="codex", api_url="https://provider.example/v1",
                     api_key_env="TEST_WEBANYTHING_KEY", model="provider-model")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        simple.setup(args, tmp_path)
    assert skill.read_text() == "unrelated skill\n"
    assert not (home / "provider.json").exists()


def test_simple_modes_enforce_task_boundary(tmp_path: Path):
    common = dict(url="https://example.org/", run_dir=None, model="gpt-5.6-sol",
                  reasoning_effort="high", timeout_minutes=90, max_cycles=12,
                  initialize_only=True)
    with pytest.raises(SystemExit, match="requires --task"):
        simple.create(Namespace(**common, mode="jit", task=None), tmp_path)
    with pytest.raises(SystemExit, match="does not accept --task"):
        simple.create(Namespace(**common, mode="full", task="Do something"), tmp_path)
    assert simple.run_identity("https://example.org/a", "full", None) != simple.run_identity(
        "https://example.org/a", "jit", "Do something"
    )


def test_provider_rejects_readable_or_symlinked_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("WEBANYTHING_API_URL", "WEBANYTHING_API_KEY", "WEBANYTHING_RESPONSES_BASE_URL", "WEBANYTHING_RESPONSES_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "provider.json"
    config.write_text('{"schema_version":1,"api_url":"https://example.org/v1","api_key":"test"}')
    config.chmod(0o644)
    monkeypatch.setenv("WEBANYTHING_CONFIG", str(config))
    with pytest.raises(SystemExit, match="mode 0600"):
        simple.provider()
    config.chmod(0o600)
    link = tmp_path / "provider-link.json"
    link.symlink_to(config)
    monkeypatch.setenv("WEBANYTHING_CONFIG", str(link))
    with pytest.raises(SystemExit, match="not configured"):
        simple.provider()


def test_explicit_full_mode_generates_coverage_brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    (source / "README.md").write_text("fixture\n")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    monkeypatch.setattr(coupled_pipeline, "repository_root", lambda: source)
    run_root = tmp_path / "run"
    # Initialization performs no provider call or model turn.
    argv = ["https://example.org/", "--site", "example", "--run-dir", str(run_root),
            "--mode", "full", "--initialize-only"]
    # A local clone requires an initial commit.
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--quiet", "-m", "fixture"], check=True)
    assert coupled_pipeline.main(argv) == 0
    manifest = json.loads((run_root / "coupled-run.json").read_text())
    assert manifest["mode"] == "full"
    brief = (run_root / "requirements.md").read_text()
    assert "Do not claim full coverage" in brief
    assert "independent Verifier" in brief
    assert coupled_pipeline.main(argv) == 0
