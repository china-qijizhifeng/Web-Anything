from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.synthetic_pipeline_supervisor import SyntheticPipelineError, SiteRuntime, Supervisor, _attempt_key, apply_stage_result, atomic_json, fingerprint, initial_checkpoint, load_spec, render_stage_command


STAGES = ("capture", "build", "freeze", "functional_verify", "visual_verify", "repair", "refreeze", "release")


def _site(command: list[str], **overrides):
    value = {
        "id": "demo",
        "auth_required": False,
        "cwd": ".",
        "max_visual_repairs": 3,
        "max_functional_repairs": 4,
        "max_stage_attempts": 2,
        "commands": {stage: list(command) for stage in STAGES},
    }
    value.update(overrides)
    return value


def _active(runtime: SiteRuntime, stage: str, token: str = "token") -> None:
    runtime.checkpoint["state"] = stage
    runtime.checkpoint["active_stage"] = stage
    runtime.checkpoint["active_token"] = token
    runtime.checkpoint.setdefault("stage_attempts", {})[stage] = 1


def test_stage_commands_expand_repair_rounds_without_a_shell():
    command = ["verify", "visual-r{repair_round}.json", "repair-r{failed_visual_round}.json", "{site}", "{stage}"]
    assert render_stage_command(command, site="demo", stage="repair", repair_round=2) == [
        "verify", "visual-r2.json", "repair-r1.json", "demo", "repair",
    ]


def test_visual_repairs_are_bounded_to_three_rounds(tmp_path: Path):
    site = _site([sys.executable, "-c", "pass"])
    checkpoint = initial_checkpoint(site, "a" * 64, tmp_path)
    runtime = SiteRuntime(site="demo", spec=site, checkpoint=checkpoint)
    for expected_round in (1, 2, 3):
        _active(runtime, "visual_verify", f"visual-{expected_round}")
        apply_stage_result(runtime, {"stage": "visual_verify", "token": f"visual-{expected_round}", "returncode": 2, "timed_out": False})
        assert checkpoint["state"] == "repair"
        assert checkpoint["repair_round"] == expected_round
        _active(runtime, "repair", f"repair-{expected_round}")
        apply_stage_result(runtime, {"stage": "repair", "token": f"repair-{expected_round}", "returncode": 0, "timed_out": False})
        assert checkpoint["state"] == "refreeze"
    _active(runtime, "visual_verify", "visual-final")
    apply_stage_result(runtime, {"stage": "visual_verify", "token": "visual-final", "returncode": 2, "timed_out": False})
    assert checkpoint["state"] == "visual_review_required"
    assert checkpoint["terminal"] is True


def test_functional_failure_enters_the_same_bounded_builder_repair_loop(tmp_path: Path):
    site = _site([sys.executable, "-c", "pass"])
    checkpoint = initial_checkpoint(site, "a" * 64, tmp_path)
    runtime = SiteRuntime(site="demo", spec=site, checkpoint=checkpoint)
    _active(runtime, "functional_verify", "functional-0")
    apply_stage_result(runtime, {
        "stage": "functional_verify", "token": "functional-0", "returncode": 2, "timed_out": False,
    })
    assert checkpoint["state"] == "repair"
    assert checkpoint["repair_round"] == 1
    assert checkpoint["repair_kind"] == "functional"
    assert checkpoint["repair_counts"] == {"functional": 1, "visual": 0}


def test_functional_repairs_do_not_consume_visual_repair_budget(tmp_path: Path):
    site = _site([sys.executable, "-c", "pass"])
    checkpoint = initial_checkpoint(site, "a" * 64, tmp_path)
    runtime = SiteRuntime(site="demo", spec=site, checkpoint=checkpoint)
    for round_number in (1, 2, 3):
        _active(runtime, "functional_verify", f"functional-{round_number}")
        apply_stage_result(runtime, {
            "stage": "functional_verify", "token": f"functional-{round_number}", "returncode": 2, "timed_out": False,
        })
        assert checkpoint["state"] == "repair"
        _active(runtime, "repair", f"functional-repair-{round_number}")
        apply_stage_result(runtime, {
            "stage": "repair", "token": f"functional-repair-{round_number}", "returncode": 0, "timed_out": False,
        })
    assert checkpoint["repair_counts"] == {"functional": 3, "visual": 0}

    _active(runtime, "visual_verify", "visual-first")
    apply_stage_result(runtime, {
        "stage": "visual_verify", "token": "visual-first", "returncode": 2, "timed_out": False,
    })
    assert checkpoint["state"] == "repair"
    assert checkpoint["repair_round"] == 4
    assert checkpoint["repair_kind"] == "visual"
    assert checkpoint["repair_counts"] == {"functional": 3, "visual": 1}


def test_capture_exit_codes_do_not_become_builder_repairs(tmp_path: Path):
    site = _site([sys.executable, "-c", "pass"])
    for returncode, state in ((20, "auth_expired"), (21, "source_blocked"), (22, "capture_failed"), (23, "sanitize_failed")):
        checkpoint = initial_checkpoint(site, "a" * 64, tmp_path)
        runtime = SiteRuntime(site="demo", spec=site, checkpoint=checkpoint)
        _active(runtime, "capture")
        apply_stage_result(runtime, {"stage": "capture", "token": "token", "returncode": returncode, "timed_out": False})
        assert checkpoint["state"] == state
        assert checkpoint["state"] != "repair"


def test_pipeline_runs_all_stages_from_checkpoint_to_release(tmp_path: Path):
    success = [sys.executable, "-c", "import sys; print('stage-out'); print('stage-err', file=sys.stderr)"]
    site = _site(success)
    spec = {
        "schema": "webanything-synthetic-pipeline-spec-v1",
        "root": str(tmp_path),
        "pool_limits": {"capture": 1, "builder": 1, "verifier": 1},
        "sites": [site],
    }
    path = tmp_path / "pipeline.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    root, loaded = load_spec(path)
    state_dir = tmp_path / "state"
    assert Supervisor(root=root, spec=loaded, state_dir=state_dir).run(poll_seconds=0.01) == 0
    checkpoint = json.loads((state_dir / "checkpoints/demo.json").read_text(encoding="utf-8"))
    assert checkpoint["state"] == "released"
    visited = [item["state"] for item in checkpoint["history"]]
    for state in ("capture", "sanitize", "reference_frozen", "build", "freeze", "functional_verify", "visual_verify", "release", "released"):
        assert state in visited
    stdout_logs = sorted((state_dir / "stage-logs/demo").glob("*.stdout.log"))
    stderr_logs = sorted((state_dir / "stage-logs/demo").glob("*.stderr.log"))
    assert len(stdout_logs) == len(stderr_logs) == 6
    assert all(path.read_text(encoding="utf-8") == "stage-out\n" for path in stdout_logs)
    assert all(path.read_text(encoding="utf-8") == "stage-err\n" for path in stderr_logs)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in stdout_logs + stderr_logs)


def test_pipeline_can_continue_from_release_to_deployment(tmp_path: Path):
    success = [sys.executable, "-c", "raise SystemExit(0)"]
    site = _site(success)
    site["commands"]["deploy"] = success
    spec = {
        "schema": "webanything-synthetic-pipeline-spec-v1",
        "root": str(tmp_path),
        "pool_limits": {"capture": 1, "builder": 1, "verifier": 1},
        "sites": [site],
    }
    state_dir = tmp_path / "state"

    assert Supervisor(root=tmp_path, spec=spec, state_dir=state_dir).run(poll_seconds=0.01) == 0
    checkpoint = json.loads((state_dir / "checkpoints/demo.json").read_text(encoding="utf-8"))
    summary = json.loads((state_dir / "summary.json").read_text(encoding="utf-8"))
    assert checkpoint["state"] == "deployed"
    assert summary["status"] == "deployed"
    assert [item["state"] for item in checkpoint["history"]][-3:] == ["release", "deploy", "deployed"]


def test_failed_deploy_reconciles_a_later_verified_result(tmp_path: Path):
    success = [sys.executable, "-c", "raise AssertionError('must not relaunch')"]
    deployment_result = tmp_path / "e2b-deployment.json"
    site = _site(success)
    site["commands"]["deploy"] = [*success, "--result", str(deployment_result)]
    spec = {
        "schema": "webanything-synthetic-pipeline-spec-v1",
        "root": str(tmp_path),
        "pool_limits": {"capture": 1, "builder": 1, "verifier": 1},
        "sites": [site],
    }
    state_dir = tmp_path / "state"
    checkpoint = initial_checkpoint(site, fingerprint(spec), tmp_path)
    checkpoint.update({
        "state": "failed",
        "terminal": True,
        "last_result": {"stage": "deploy", "returncode": 1},
    })
    atomic_json(state_dir / "checkpoints/demo.json", checkpoint)
    deployment_result.write_text(json.dumps({
        "sandbox_id": "sandbox-demo",
        "gateway_url": "https://8885-sandbox-demo.example/",
        "external_error": None,
        "verification_failures": [],
        "sites": [{"id": "demo", "url": "https://8885-sandbox-demo.example/p/40000/"}],
    }), encoding="utf-8")

    assert Supervisor(root=tmp_path, spec=spec, state_dir=state_dir).run(poll_seconds=0.01) == 0
    resumed = json.loads((state_dir / "checkpoints/demo.json").read_text(encoding="utf-8"))
    assert resumed["state"] == "deployed"
    assert resumed["deployment_result"]["sandbox_id"] == "sandbox-demo"
    assert resumed["history"][-1]["outcome"] == "verified_deployment_result_reconciled"


def test_pipeline_timeout_uses_stage_specific_terminal_state(tmp_path: Path):
    success = [sys.executable, "-c", "raise SystemExit(0)"]
    site = _site(success, max_stage_attempts=1, timeouts_seconds={"build": 0.05})
    site["commands"]["build"] = [sys.executable, "-c", "import time; time.sleep(5)"]
    spec = {
        "schema": "webanything-synthetic-pipeline-spec-v1",
        "root": str(tmp_path),
        "pool_limits": {"capture": 1, "builder": 1, "verifier": 1},
        "sites": [site],
    }
    state_dir = tmp_path / "state"
    assert Supervisor(root=tmp_path, spec=spec, state_dir=state_dir).run(poll_seconds=0.01) == 2
    checkpoint = json.loads((state_dir / "checkpoints/demo.json").read_text(encoding="utf-8"))
    assert checkpoint["state"] == "builder_timeout"
    assert checkpoint["terminal"] is True


def test_nonzero_stage_result_restarts_until_attempt_budget_is_exhausted(tmp_path: Path):
    site = _site([sys.executable, "-c", "pass"], max_stage_attempts=2)
    checkpoint = initial_checkpoint(site, "a" * 64, tmp_path)
    runtime = SiteRuntime(site="demo", spec=site, checkpoint=checkpoint)

    _active(runtime, "build", "build-one")
    apply_stage_result(runtime, {
        "stage": "build",
        "token": "build-one",
        "returncode": 1,
        "timed_out": False,
    })
    assert checkpoint["state"] == "build"
    assert checkpoint["terminal"] is False
    assert checkpoint["history"][-1]["outcome"] == "error_restart"

    checkpoint["stage_attempts"]["build"] = 2
    checkpoint["active_stage"] = "build"
    checkpoint["active_token"] = "build-two"
    apply_stage_result(runtime, {
        "stage": "build",
        "token": "build-two",
        "returncode": 1,
        "timed_out": False,
    })
    assert checkpoint["state"] == "failed"
    assert checkpoint["terminal"] is True
    assert checkpoint["history"][-1]["outcome"] == "error_exhausted"


def test_verifier_timeout_budget_resets_after_visual_repair(tmp_path: Path):
    site = _site([sys.executable, "-c", "pass"], max_stage_attempts=2)
    checkpoint = initial_checkpoint(site, "a" * 64, tmp_path)
    checkpoint["repair_round"] = 1
    checkpoint["repair_kind"] = "visual"
    runtime = SiteRuntime(site="demo", spec=site, checkpoint=checkpoint)
    key = _attempt_key(checkpoint, "functional_verify")
    checkpoint["stage_attempts"][key] = 1
    checkpoint["active_stage"] = "functional_verify"
    checkpoint["active_token"] = "round-one"
    apply_stage_result(runtime, {
        "stage": "functional_verify",
        "token": "round-one",
        "returncode": -15,
        "timed_out": True,
    })
    assert checkpoint["state"] == "functional_verify"
    assert checkpoint["terminal"] is False
    assert key == "functional_verify:visual-repair-1"
    assert "functional_verify:visual-repair-0" not in checkpoint["stage_attempts"]


def test_missing_auth_pauses_only_that_site(tmp_path: Path):
    success = [sys.executable, "-c", "raise SystemExit(0)"]
    site = _site(success, auth_required=True, artifacts={"auth_profile": "private/demo.auth-profile.enc.json"})
    spec = {
        "schema": "webanything-synthetic-pipeline-spec-v1",
        "root": str(tmp_path),
        "pool_limits": {"capture": 1, "builder": 1, "verifier": 1},
        "sites": [site],
    }
    state_dir = tmp_path / "state"
    assert Supervisor(root=tmp_path, spec=spec, state_dir=state_dir).run(poll_seconds=0.01) == 2
    checkpoint = json.loads((state_dir / "checkpoints/demo.json").read_text(encoding="utf-8"))
    assert checkpoint["state"] == "auth_required"
    assert checkpoint["terminal"] is False


def test_corrupt_checkpoint_fails_closed(tmp_path: Path):
    site = _site([sys.executable, "-c", "raise SystemExit(0)"])
    spec = {
        "schema": "webanything-synthetic-pipeline-spec-v1",
        "root": str(tmp_path),
        "pool_limits": {"capture": 1, "builder": 1, "verifier": 1},
        "sites": [site],
    }
    state_dir = tmp_path / "state"
    (state_dir / "checkpoints").mkdir(parents=True)
    (state_dir / "checkpoints/demo.json").write_text("not json\n", encoding="utf-8")
    with pytest.raises(SyntheticPipelineError, match="checkpoint is corrupt"):
        Supervisor(root=tmp_path, spec=spec, state_dir=state_dir)
