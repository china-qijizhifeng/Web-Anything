from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import webanything.cli as cli
from webanything.cli import _unique_repair_phase, build_parser, reconcile_build_status, reconcile_repair_status, require_public_url
from webanything.release import candidate_identity, freeze_candidate


def test_capture_state_exit_uses_supervisor_protocol(capsys):
    with pytest.raises(SystemExit) as raised:
        cli._capture_state_exit(20, "auth_expired", RuntimeError("authorization required"))
    assert raised.value.code == 20
    assert "auth_expired: authorization required" in capsys.readouterr().err


def test_reset_trace_uses_a_unique_bounded_repair_phase(tmp_path: Path):
    (tmp_path / "run.json").write_text(json.dumps({
        "continuations": [
            {"phase": "synthetic-visual-r4"},
            {"phase": "synthetic-visual-r4-retry-2"},
        ],
    }), encoding="utf-8")
    assert _unique_repair_phase(tmp_path, "synthetic-visual-r4") == "synthetic-visual-r4-retry-3"

    long_phase = "a" * 40
    (tmp_path / "run.json").write_text(json.dumps({"continuations": [{"phase": long_phase}]}), encoding="utf-8")
    resolved = _unique_repair_phase(tmp_path, long_phase)
    assert resolved == f"{'a' * 32}-retry-2"
    assert len(resolved) == 40


def test_parser_exposes_hardened_pipeline_commands_and_options():
    parser = build_parser()
    help_text = parser.format_help()
    for command in (
        "doctor", "build", "contract", "synthetic-contract", "synthetic-replay-contract", "capture-replay", "import-authorized-browser-capture", "replay-build", "freeze-replay-capture", "reconcile-replay-boundary", "repair", "verify",
        "verify-synthetic", "verify-synthetic-replay", "batch", "freeze", "release", "release-synthetic", "release-synthetic-replay",
        "seal-auth-state", "capture-synthetic-reference", "verify-synthetic-visual",
        "synthetic-pipeline", "deploy", "seal", "release-scan",
        "coupled-pipeline", "e2b-coupled-batch", "configure", "create", "preview",
    ):
        assert command in help_text
    verify_help = next(action for action in parser._actions if isinstance(getattr(action, "choices", None), dict) and "verify" in action.choices).choices["verify"].format_help()
    for option in ("--batch-size", "--checkpoint-dir", "--resume", "--candidate-freeze"):
        assert option in verify_help
    batch_help = next(action for action in parser._actions if isinstance(getattr(action, "choices", None), dict) and "batch" in action.choices).choices["batch"].format_help()
    assert "--worker-timeout-seconds" in batch_help
    e2b_batch_help = next(action for action in parser._actions if isinstance(getattr(action, "choices", None), dict) and "e2b-coupled-batch" in action.choices).choices["e2b-coupled-batch"].format_help()
    for option in ("--spec", "--state-dir", "--env-file", "--max-concurrency"):
        assert option in e2b_batch_help
    build_help = next(action for action in parser._actions if isinstance(getattr(action, "choices", None), dict) and "build" in action.choices).choices["build"].format_help()
    assert "--build-mode" in build_help
    assert "--synthetic-contract" in build_help
    release_synthetic_help = next(action for action in parser._actions if isinstance(getattr(action, "choices", None), dict) and "release-synthetic" in action.choices).choices["release-synthetic"].format_help()
    assert "--visual-reference" in release_synthetic_help
    assert "--visual-verification" in release_synthetic_help
    commands = next(action for action in parser._actions if isinstance(getattr(action, "choices", None), dict)).choices
    deploy_help = commands["deploy"].format_help()
    assert "--sandbox-id" in deploy_help
    assert "--create-if-missing" in deploy_help
    capture_replay_help = commands["capture-replay"].format_help()
    for option in ("--route", "--product-profile", "--auth-profile", "--private-selector", "--authenticated-selector", "--max-actions-per-route"):
        assert option in capture_replay_help


def test_replay_boundary_restores_inputs_and_removes_private_candidate_evidence(tmp_path: Path, monkeypatch):
    run_dir = tmp_path / "run"
    frozen = run_dir / "workspace/.webanything/replay-input"
    external = run_dir / "replay-source"
    candidate = run_dir / "workspace/sites/demo"
    for root in (frozen, external, candidate):
        root.mkdir(parents=True)
    (frozen / "synthetic_replay_contract.json").write_text('{"site_id":"demo"}\n', encoding="utf-8")
    (frozen / "capture/pages").mkdir(parents=True)
    (frozen / "capture/pages/home.html").write_text("pristine\n", encoding="utf-8")
    (external / "synthetic_replay_contract.json").write_text("corrupted contract\n", encoding="utf-8")
    (external / "capture/pages").mkdir(parents=True)
    (external / "capture/pages/home.html").write_text("corrupted page\n", encoding="utf-8")
    (candidate / "app.py").write_text("app = 1\n", encoding="utf-8")
    (candidate / "visual-reference").mkdir()
    (candidate / "visual-reference/reference.png").write_bytes(b"private")

    monkeypatch.setattr(cli, "load_synthetic_replay_contract", lambda _path: {"site_id": "demo"})
    result = cli._reconcile_replay_boundary(run_dir, "demo", required=True)

    assert result is not None
    assert result["restored_file_count"] == 2
    assert (external / "capture/pages/home.html").read_text() == "pristine\n"
    assert (external / "synthetic_replay_contract.json").read_text() == '{"site_id":"demo"}\n'
    assert not (candidate / "visual-reference").exists()
    assert (candidate / "app.py").is_file()
    assert len(list((run_dir / "diagnostics/replay-source-restores").rglob("home.html"))) == 1


def test_public_url_rejects_credentials():
    with pytest.raises(SystemExit, match="credentials"):
        require_public_url("https://user:password@example.com/")


def test_batch_forwards_worker_wall_timeout(tmp_path: Path, monkeypatch):
    observed = {}

    def capture(command, *, cwd=None):
        observed["command"] = command
        observed["cwd"] = cwd
        return 0

    monkeypatch.setattr(cli, "run", capture)
    monkeypatch.setattr(cli, "repository_root", lambda: tmp_path)
    args = Namespace(
        spec=str(tmp_path / "batch.json"),
        state_dir=None,
        summary=None,
        max_concurrency=3,
        worker_timeout_seconds=900,
    )
    assert cli.command_batch(args) == 0
    assert observed["command"][-2:] == ["--worker-timeout-seconds", "900"]


def test_e2b_coupled_batch_forwards_controller_options(tmp_path: Path, monkeypatch):
    observed = {}

    def capture(command, *, cwd=None):
        observed["command"] = command
        observed["cwd"] = cwd
        return 0

    monkeypatch.setattr(cli, "run", capture)
    monkeypatch.setattr(cli, "repository_root", lambda: tmp_path)
    args = Namespace(
        spec=str(tmp_path / "batch.json"),
        state_dir=str(tmp_path / "state"),
        env_file=str(tmp_path / "private.env"),
        max_concurrency=15,
    )

    assert cli.command_e2b_coupled_batch(args) == 0
    assert observed["command"][-2:] == ["--max-concurrency", "15"]
    assert "--state-dir" in observed["command"]
    assert "--env-file" in observed["command"]
    assert observed["cwd"] == tmp_path


def test_examples_are_valid_run_configs():
    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "examples").glob("*.run.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema"] == "webanything-run-config-v1"


def test_reconcile_build_and_repair_statuses():
    with TemporaryDirectory() as directory:
        run_dir = Path(directory)
        (run_dir / "run.json").write_text(json.dumps({"execution_status": "completed", "fatal_error": None}), encoding="utf-8")
        assert reconcile_build_status(run_dir, {"schema": "webanything-run-config-v1"}, 0)["status"] == "completed"
        (run_dir / "run.json").write_text(json.dumps({"latest_execution_status": "timed_out", "fatal_error": "deadline"}), encoding="utf-8")
        assert reconcile_repair_status(run_dir, {}, 1, phase="r1")["status"] == "timed_out"


def test_normal_build_rejects_existing_frozen_candidate_before_runner(tmp_path: Path, monkeypatch):
    site = tmp_path / "run/workspace/sites/demo"
    site.mkdir(parents=True)
    (site / "app.py").write_text("app = 1\n")
    freeze_candidate(tmp_path / "run/candidate-freeze.json", candidate_identity(site))
    called = False

    def no_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(cli._base, "run", no_run)
    args = Namespace(
        url="https://example.test/", site="demo", run_dir=str(tmp_path / "run"), profile="immersive",
        viewport_width=1440, viewport_height=1000, max_pages=5, max_assets=10, timeout_minutes=1,
        agent_config=None, model=None, reasoning_effort=None, requirements_file=None, supplemental_url=[],
        private_research_snapshot=False, no_reflection=False, skip_artifact=False,
    )
    with pytest.raises(SystemExit, match="frozen candidate"):
        cli.command_build(args)
    assert called is False


def test_explicit_repair_transition_invalidates_evidence_before_builder(tmp_path: Path, monkeypatch):
    run_dir = tmp_path / "run"
    site = run_dir / "workspace/sites/demo"
    site.mkdir(parents=True)
    (site / "app.py").write_text("app = 1\n")
    (run_dir / "run.json").write_text("{}")
    freeze_candidate(run_dir / "candidate-freeze.json", candidate_identity(site))
    evidence = run_dir / "verification/fidelity.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({"schema": "webharbor-fidelity-v19", "status": "passed"}))
    repair = run_dir / "repair.json"
    repair.write_text(json.dumps({"status": "repair_required"}))
    observed = {}

    def repaired(args):
        observed["evidence_status"] = json.loads(evidence.read_text())["status"]
        observed["freeze_state"] = json.loads((run_dir / "candidate-freeze.json").read_text())["state"]
        return 0

    monkeypatch.setattr(cli, "_ORIGINAL_REPAIR", repaired)
    args = Namespace(run_dir=str(run_dir), repair_package=str(repair), site="demo", phase="r1")
    assert cli.command_repair(args) == 0
    assert observed == {"evidence_status": "invalidated", "freeze_state": "invalidated"}
    transition = json.loads((run_dir / "repair-transition.json").read_text())
    assert transition["status"] == "builder_writes_authorized"
