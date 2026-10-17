from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import coupled_pipeline


def make_repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "WebAnything Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "WebAnything Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "-C", str(path), "commit", "--quiet", "-m", "fixture"],
        check=True,
        env=environment,
    )
    return path


def test_require_public_url_rejects_credentials():
    with pytest.raises(SystemExit, match="credentials"):
        coupled_pipeline.require_public_url("https://user:secret@example.com/")


def test_initialize_workspace_preserves_existing_checkout(tmp_path: Path):
    root = make_repository(tmp_path / "source")
    workspace = tmp_path / "run/workspace"
    assert coupled_pipeline.initialize_workspace(root, workspace) is True
    (workspace / "checkpoint.txt").write_text("keep\n", encoding="utf-8")
    assert coupled_pipeline.initialize_workspace(root, workspace) is False
    assert (workspace / "checkpoint.txt").read_text(encoding="utf-8") == "keep\n"


def test_initialize_only_keeps_authorized_state_outside_workspace_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root = make_repository(tmp_path / "source")
    requirements = tmp_path / "requirements.md"
    requirements.write_text("Build the contracted routes.\n", encoding="utf-8")
    private_state = tmp_path / "private-browser-state.json"
    private_state.write_text('{"cookies":[{"name":"private"}],"origins":[]}\n', encoding="utf-8")
    run_root = tmp_path / "run"
    monkeypatch.setattr(coupled_pipeline, "repository_root", lambda: root)

    result = coupled_pipeline.main(
        [
            "https://example.com/",
            "--site",
            "example",
            "--run-dir",
            str(run_root),
            "--requirements-file",
            str(requirements),
            "--storage-state",
            str(private_state),
            "--initialize-only",
        ]
    )

    assert result == 0
    manifest_text = (run_root / "coupled-run.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["authorized_storage_state_supplied"] is True
    assert manifest["storage_state_persisted_in_workspace"] is False
    assert str(private_state) not in manifest_text
    assert not (run_root / "workspace/.session").exists()
    assert json.loads(capsys.readouterr().out)["status"] == "initialized"


def test_task_query_creates_frozen_task_brief_and_rejects_changed_query(tmp_path: Path, monkeypatch):
    root = make_repository(tmp_path / "source")
    run_root = tmp_path / "run"
    monkeypatch.setattr(coupled_pipeline, "repository_root", lambda: root)
    argv = [
        "https://example.com/", "--site", "example", "--run-dir", str(run_root),
        "--task-query", "Find the second example and open it", "--initialize-only",
    ]
    assert coupled_pipeline.main(argv) == 0
    request = json.loads((run_root / "task-request.json").read_text(encoding="utf-8"))
    assert request["query"] == "Find the second example and open it"
    assert request["query_sha256"] in (run_root / "requirements.md").read_text(encoding="utf-8")
    assert "complete, shortest legitimate" in (run_root / "requirements.md").read_text(encoding="utf-8")
    assert coupled_pipeline.main(argv) == 0
    with pytest.raises(SystemExit, match="differs from this run checkpoint"):
        coupled_pipeline.main(argv[:-2] + ["Find a different example", "--initialize-only"])

    launched = {}
    def fake_run(command, **kwargs):
        launched["environment"] = kwargs["env"]
        return SimpleNamespace(returncode=66)
    monkeypatch.setattr(coupled_pipeline.subprocess, "run", fake_run)
    assert coupled_pipeline.main(argv[:-1]) == 66
    assert launched["environment"]["WEBANYTHING_PYTHON"]


def test_supervisor_retries_incomplete_verifier_without_rebuilding(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    run_root = tmp_path / "run"
    (run_root / "workspace/sites/demo").mkdir(parents=True)
    (run_root / "workspace/sites/demo/app.py").write_text("app = 1\n", encoding="utf-8")
    (run_root / "requirements.md").write_text("Keep the checkpoint.\n", encoding="utf-8")
    (run_root / ".session").mkdir()
    (run_root / ".session/browser-state.json").write_text(
        '{"cookies":[],"origins":[]}\n', encoding="utf-8"
    )
    builder_output = run_root / "sdk-output-existing"
    builder_output.mkdir()
    (builder_output / "build-result.json").write_text(
        '{"status":"passed"}\n', encoding="utf-8"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_node = fake_bin / "node"
    fake_node.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
script = Path(sys.argv[1]).name
counter = Path(os.environ["FAKE_VERIFIER_COUNTER"])
if script != "run_independent_verifier.mjs":
    raise SystemExit("Builder must not run after an incomplete Verifier")
count = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(count))
output = Path(sys.argv[sys.argv.index("--output") + 1])
output.mkdir(parents=True, exist_ok=True)
if count == 1:
    (output / "sdk-run.json").write_text(json.dumps({"turn_status": "failed"}))
    raise SystemExit(1)
(output / "sdk-run.json").write_text(json.dumps({
    "turn_status": "completed", "candidate_unchanged": True
}))
(output / "verifier-result.json").write_text(json.dumps({"status": "passed"}))
""",
        encoding="utf-8",
    )
    fake_node.chmod(0o755)
    counter = tmp_path / "verifier-count.txt"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_VERIFIER_COUNTER": str(counter),
        "WEBANYTHING_RESPONSES_API_KEY": "test-only",
        "WEBANYTHING_RESPONSES_BASE_URL": "https://example.invalid/v1",
        "WEBANYTHING_PROVIDER_PREFLIGHT": "0",
    }
    completed = subprocess.run(
        [
            str(root / "tools/coupled-pipeline/run_site_to_completion.sh"),
            "demo",
            "https://example.com/",
            str(run_root),
            "3",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )

    assert completed.returncode == 0
    assert counter.read_text(encoding="utf-8") == "2"
    assert "resuming the same Verifier output and candidate" in completed.stdout
    assert len(list(run_root.glob("verifier-output-auto-*"))) == 1
    status = json.loads((run_root / "auto-supervisor-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "complete"
    assert status["cycle"] == 2


@pytest.mark.parametrize("state", ["repair_required", "blocked_retry_limit"])
def test_supervisor_restart_repairs_before_reverification(tmp_path: Path, state: str):
    root = Path(__file__).resolve().parents[1]
    run_root = tmp_path / "run"
    (run_root / "workspace/sites/demo").mkdir(parents=True)
    (run_root / "workspace/sites/demo/app.py").write_text("app = 1\n")
    (run_root / "requirements.md").write_text("Keep the candidate.\n")
    (run_root / ".session").mkdir()
    (run_root / ".session/browser-state.json").write_text('{"cookies":[],"origins":[]}\n')
    old = run_root / "sdk-output-auto-b1-old"
    old.mkdir()
    (old / "build-result.json").write_text(json.dumps({"site_id": "demo", "status": "passed"}))
    (old / "sdk-run.json").write_text(json.dumps({"site_id": "demo", "turn_status": "completed"}))
    status = {"site_id": "demo", "state": state, "detail": "verifier-output-auto-v1-old:failed"}
    if state == "blocked_retry_limit":
        status.update(resume_state="repair_required", resume_detail=status["detail"])
    (run_root / "auto-supervisor-status.json").write_text(json.dumps(status))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_node = fake_bin / "node"
    fake_node.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
script = Path(sys.argv[1]).name
out = Path(sys.argv[sys.argv.index('--output') + 1]); out.mkdir(parents=True, exist_ok=True)
log = Path(os.environ['FAKE_CALL_LOG'])
with log.open('a') as handle: handle.write(script + '\\n')
if script == 'run_coupled_builder.mjs':
    (out / 'sdk-run.json').write_text(json.dumps({'site_id':'demo','turn_status':'completed'}))
    (out / 'build-result.json').write_text(json.dumps({'site_id':'demo','status':'passed'}))
elif script == 'run_independent_verifier.mjs':
    (out / 'sdk-run.json').write_text(json.dumps({'turn_status':'completed','candidate_unchanged':True}))
    (out / 'verifier-result.json').write_text(json.dumps({'status':'passed'}))
else: raise SystemExit('unexpected script')
""")
    fake_node.chmod(0o755)
    log = tmp_path / "calls.log"
    completed = subprocess.run(
        [str(root / "tools/coupled-pipeline/run_site_to_completion.sh"), "demo", "https://example.com/", str(run_root), "1"],
        text=True, capture_output=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_CALL_LOG": str(log),
             "WEBANYTHING_RESPONSES_API_KEY": "test-only",
             "WEBANYTHING_RESPONSES_BASE_URL": "https://example.invalid/v1",
             "WEBANYTHING_PROVIDER_PREFLIGHT": "0"},
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert log.read_text().splitlines() == ["run_coupled_builder.mjs", "run_independent_verifier.mjs"]


@pytest.mark.parametrize("state", ["verifier_retry", "blocked_retry_limit"])
def test_supervisor_restart_resumes_interrupted_verifier_output(tmp_path: Path, state: str):
    root = Path(__file__).resolve().parents[1]
    run_root = tmp_path / "run"
    (run_root / "workspace/sites/demo").mkdir(parents=True)
    (run_root / "requirements.md").write_text("Keep the candidate.\n")
    (run_root / ".session").mkdir()
    (run_root / ".session/browser-state.json").write_text('{"cookies":[],"origins":[]}\n')
    builder = run_root / "sdk-output-auto-b1-old"
    builder.mkdir()
    (builder / "build-result.json").write_text(json.dumps({"site_id": "demo", "status": "passed"}))
    (builder / "sdk-run.json").write_text(json.dumps({"site_id": "demo", "turn_status": "completed"}))
    verifier = run_root / "verifier-output-auto-v1-old"
    verifier.mkdir()
    status = {"site_id": "demo", "state": state,
              "detail": verifier.name + ":infrastructure_incomplete"}
    if state == "blocked_retry_limit":
        status.update(resume_state="verifier_retry", resume_detail=status["detail"])
    (run_root / "auto-supervisor-status.json").write_text(json.dumps(status))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_node = fake_bin / "node"
    fake_node.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
assert Path(sys.argv[1]).name == 'run_independent_verifier.mjs'
out = Path(sys.argv[sys.argv.index('--output') + 1])
assert out.name == 'verifier-output-auto-v1-old'
(out / 'sdk-run.json').write_text(json.dumps({'turn_status':'completed','candidate_unchanged':True}))
(out / 'verifier-result.json').write_text(json.dumps({'status':'passed'}))
""")
    fake_node.chmod(0o755)
    completed = subprocess.run(
        [str(root / "tools/coupled-pipeline/run_site_to_completion.sh"), "demo", "https://example.com/", str(run_root), "1"],
        text=True, capture_output=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
             "WEBANYTHING_RESPONSES_API_KEY": "test-only",
             "WEBANYTHING_RESPONSES_BASE_URL": "https://example.invalid/v1",
             "WEBANYTHING_PROVIDER_PREFLIGHT": "0"},
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert len(list(run_root.glob("verifier-output-auto-*"))) == 1


def test_supervisor_ignores_timed_out_builder_pass(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    run_root = tmp_path / "run"
    (run_root / "workspace").mkdir(parents=True)
    (run_root / "requirements.md").write_text("Build.\n")
    (run_root / ".session").mkdir()
    (run_root / ".session/browser-state.json").write_text('{"cookies":[],"origins":[]}\n')
    stale = run_root / "sdk-output-auto-b1-old"
    stale.mkdir()
    (stale / "build-result.json").write_text(json.dumps({"site_id": "demo", "status": "passed"}))
    (stale / "sdk-run.json").write_text(json.dumps({"site_id": "demo", "turn_status": "timed_out"}))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_node = fake_bin / "node"
    fake_node.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
assert Path(sys.argv[1]).name == 'run_coupled_builder.mjs'
out = Path(sys.argv[sys.argv.index('--output') + 1]); out.mkdir(parents=True)
(out / 'sdk-run.json').write_text(json.dumps({'site_id':'demo','turn_status':'failed'}))
raise SystemExit(1)
""")
    fake_node.chmod(0o755)
    completed = subprocess.run(
        [str(root / "tools/coupled-pipeline/run_site_to_completion.sh"), "demo", "https://example.com/", str(run_root), "1"],
        text=True, capture_output=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
             "WEBANYTHING_RESPONSES_API_KEY": "test-only",
             "WEBANYTHING_RESPONSES_BASE_URL": "https://example.invalid/v1",
             "WEBANYTHING_PROVIDER_PREFLIGHT": "0"},
    )
    assert completed.returncode == 3, completed.stderr + completed.stdout
    assert "starting/resuming Builder" in completed.stdout


def test_task_run_rejects_verifier_pass_without_step_evidence(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    run_root = tmp_path / "run"
    (run_root / "workspace/sites/demo").mkdir(parents=True)
    (run_root / "workspace/evidence").mkdir()
    (run_root / "requirements.md").write_text("Task brief\n", encoding="utf-8")
    (run_root / ".session").mkdir()
    (run_root / ".session/browser-state.json").write_text('{"cookies":[],"origins":[]}\n')
    (run_root / "task-request.json").write_text(json.dumps({"query_sha256": "abc"}))
    builder_output = run_root / "sdk-output-existing"
    builder_output.mkdir()
    (builder_output / "build-result.json").write_text('{"status":"passed"}\n')
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_node = fake_bin / "node"
    fake_node.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
out = Path(sys.argv[sys.argv.index('--output') + 1]); out.mkdir(parents=True)
(out / 'sdk-run.json').write_text(json.dumps({'turn_status':'completed','candidate_unchanged':True}))
(out / 'verifier-result.json').write_text(json.dumps({'site_id':'demo','status':'passed','gates':{'task_workflow':{'status':'passed'}}}))
""")
    fake_node.chmod(0o755)
    completed = subprocess.run(
        [str(root / "tools/coupled-pipeline/run_site_to_completion.sh"), "demo", "https://example.com/", str(run_root), "1"],
        text=True, capture_output=True, env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                                        "WEBANYTHING_RESPONSES_API_KEY": "test-only",
                                        "WEBANYTHING_RESPONSES_BASE_URL": "https://example.invalid/v1",
                                        "WEBANYTHING_PROVIDER_PREFLIGHT": "0"},
    )
    assert completed.returncode == 3
    verifier = next(run_root.glob("verifier-output-auto-*"))
    assert json.loads((verifier / "verifier-result.json").read_text())["status"] == "failed"
    assert json.loads((run_root / "auto-supervisor-status.json").read_text())["state"] == "blocked_retry_limit"
