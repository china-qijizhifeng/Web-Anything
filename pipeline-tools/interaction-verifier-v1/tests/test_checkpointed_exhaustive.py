from __future__ import annotations

import json
import os
import shutil
from argparse import Namespace
from pathlib import Path

import sys
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from checkpoint import (  # noqa: E402
    build_verification_plan,
    execute_batches,
    merge_checkpoints,
    partition_plan,
    write_checkpoint,
    shared_artifact_ref,
)
import terminal  # noqa: E402
from fixture import prepare_workspace, running_fixture  # noqa: E402


def _freeze(site_path: Path, contract: Path, *, configuration=None):
    tree = terminal._canonical_candidate_tree(site_path)
    contract_sha256 = terminal.sha256_file(contract)
    configuration = configuration or {"site": "demo", "profile": "immersive"}
    inputs = {
        "tree_sha256": tree["sha256"],
        "contract_sha256s": [contract_sha256],
        "configuration": configuration,
    }
    identity = {
        "schema": "webanything-candidate-identity-v1",
        "sha256": terminal.hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "tree": tree,
        "contracts": [{"sha256": contract_sha256}],
        "configuration": configuration,
    }
    return {"schema": "webanything-candidate-freeze-v1", "state": "frozen", "identity": identity}


def _plan():
    return build_verification_plan(
        candidate_fingerprint="a" * 64,
        contract_sha256="b" * 64,
        verifier_version="v1",
        verifier_configuration={"timeout_ms": 10},
        viewport={"width": 1440, "height": 1000},
        routes=[{"id": "route-home", "path": "/"}, {"id": "route-detail", "path": "/detail"}],
        controls=[{"id": "control-search", "source_path": "/"}, {"id": "control-menu", "source_path": "/"}],
    )


def _browser_asset() -> Path | None:
    explicit = os.environ.get("WEBANYTHING_TEST_BROWSER")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    candidates = sorted(root.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    candidates.extend(sorted(root.glob("chromium-*/chrome-linux*/chrome")))
    executable = shutil.which("chromium") or shutil.which("chromium-browser")
    if executable:
        candidates.append(Path(executable))
    return candidates[0] if candidates else None


def test_checkpoint_resume_and_deterministic_merge(tmp_path: Path):
    plan = _plan()
    batches = partition_plan(plan, 2)
    calls = []

    def execute(batch):
        calls.append(batch["batch_id"])
        return {"status": "passed", "ids": batch["assigned_ids"]}

    first = execute_batches(plan, batches, tmp_path, execute)
    assert first["status"] == "passed"
    assert len(calls) == len(batches)
    second = execute_batches(plan, batches, tmp_path, execute, resume=True)
    assert second == first
    assert len(calls) == len(batches)


def test_interrupted_resume_executes_only_missing_batch(tmp_path: Path):
    plan = _plan()
    batches = partition_plan(plan, 2)
    write_checkpoint(tmp_path / f"{batches[0]['batch_id']}.json", batches[0], status="passed", result={"status": "passed"})
    calls = []

    def execute(batch):
        calls.append(batch["batch_id"])
        return {"status": "passed"}

    merged = execute_batches(plan, batches, tmp_path, execute)
    assert merged["status"] == "passed"
    assert calls == [batches[1]["batch_id"]]


def test_corrupt_stale_and_incomplete_checkpoints_fail_closed(tmp_path: Path):
    plan = _plan()
    batches = partition_plan(plan, 2)
    valid = write_checkpoint(tmp_path / "valid.json", batches[0], status="passed", result={"status": "passed"})
    stale = dict(valid)
    stale["contract_sha256"] = "c" * 64
    stale["batch_id"] = batches[1]["batch_id"]
    stale["batch_index"] = batches[1]["batch_index"]
    stale["assigned_ids"] = batches[1]["assigned_ids"]
    stale["result_sha256"] = "0" * 64
    merged = merge_checkpoints(plan, batches, [valid, stale])
    assert merged["status"] == "failed"
    assert {item["code"] for item in merged["failures"]} & {"invalid_batch", "missing_batch"}


def test_corrupt_checkpoint_is_reexecuted_and_failed_or_timed_out_batch_blocks_closure(tmp_path: Path):
    plan = _plan()
    batches = partition_plan(plan, 2)
    (tmp_path / f"{batches[0]['batch_id']}.json").write_text("{corrupt", encoding="utf-8")
    calls = []

    def execute(batch):
        calls.append(batch["batch_id"])
        return ("timed_out", {"error": "bounded timeout"}) if batch["batch_index"] == 1 else {"status": "passed"}

    merged = execute_batches(plan, batches, tmp_path, execute, resume=True)
    assert calls == [item["batch_id"] for item in batches]
    assert merged["status"] == "failed"
    assert "batch_not_passed" in {item["code"] for item in merged["failures"]}


def test_resume_does_not_skip_a_prior_failed_batch(tmp_path: Path):
    plan = _plan()
    batches = partition_plan(plan, 2)
    write_checkpoint(tmp_path / f"{batches[0]['batch_id']}.json", batches[0], status="failed", result={"error": "old failure"})
    calls = []

    def execute(batch):
        calls.append(batch["batch_id"])
        return {"status": "passed"}

    assert execute_batches(plan, batches, tmp_path, execute)["status"] == "passed"
    assert batches[0]["batch_id"] in calls


def test_duplicate_batch_is_rejected():
    plan = _plan()
    batches = partition_plan(plan, 2)
    first = dict(batches[0])
    first.update({"status": "passed", "result": {}, "result_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"})
    merged = merge_checkpoints(plan, batches, [first, first])
    assert merged["status"] == "failed"
    assert "duplicate_batch" in {item["code"] for item in merged["failures"]}


def test_terminal_resume_executes_only_missing_plan_batches(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    site = workspace / "sites/demo"
    site.mkdir(parents=True)
    (site / "app.py").write_text("app = 1\n", encoding="utf-8")
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({
        "route_inventory": [{"id": "home", "path": "/"}],
        "control_inventory": [{"id": "open", "source_path": "/"}],
    }), encoding="utf-8")
    calls = []

    def fake_verify(**kwargs):
        calls.append(kwargs["evidence_dir"].name)
        return {}, {"status": "passed", "failures": []}, {"status": "empty", "group_count": 0}

    monkeypatch.setattr(terminal, "_load_verifier", lambda _path: fake_verify)
    args = Namespace(
        candidate_workspace=workspace, site_path=site, site="demo", contract=contract,
        evidence_dir=tmp_path / "evidence", base_url="http://127.0.0.1:1/", launch_config=None,
        viewport_width=1440, viewport_height=1000, gateway_prefix=None, browser_path=None,
        dependency_path=None, temp_dir=None, max_pages=10, max_depth=2, timeout_ms=100,
        route_map_timeout_seconds=1, launch_timeout=1.0, batch_size=1,
        checkpoint_dir=tmp_path / "checkpoints", resume=True, candidate_freeze=None,
    )
    first = terminal.run(args)
    assert first["status"] == "passed"
    first_repair = json.loads((tmp_path / "evidence/builder-repair-package.json").read_text())
    assert first["repair_package"]["group_count"] == first_repair["group_count"]
    assert calls == ["batch-000000", "batch-000001"]
    resumed = terminal.run(args)
    resumed_repair = json.loads((tmp_path / "evidence/builder-repair-package.json").read_text())
    assert resumed["repair_package"]["group_count"] == resumed_repair["group_count"]
    assert calls == ["batch-000000", "batch-000001"]
    (tmp_path / "checkpoints/batch-000001.json").write_text("{corrupt", encoding="utf-8")
    terminal.run(args)
    assert calls == ["batch-000000", "batch-000001", "batch-000001"]


def test_plan_identity_changes_for_candidate_contract_viewport_and_config():
    base = _plan()
    variants = []
    for kwargs in (
        {"candidate_fingerprint": "d" * 64},
        {"contract_sha256": "e" * 64},
        {"viewport": {"width": 1280, "height": 800}},
        {"verifier_configuration": {"timeout_ms": 20}},
    ):
        data = {
            "candidate_fingerprint": "a" * 64,
            "contract_sha256": "b" * 64,
            "verifier_version": "v1",
            "verifier_configuration": {"timeout_ms": 10},
            "viewport": {"width": 1440, "height": 1000},
            "routes": [{"id": "route-home", "path": "/"}, {"id": "route-detail", "path": "/detail"}],
            "controls": [{"id": "control-search", "source_path": "/"}, {"id": "control-menu", "source_path": "/"}],
        }
        data.update(kwargs)
        variants.append(build_verification_plan(**data)["plan_identity"])
    assert all(item != base["plan_identity"] for item in variants)


def test_shared_artifact_binding_and_bounded_checkpoint_delta(tmp_path: Path):
    shared = tmp_path / "shared" / "code-interaction-inventory.json"
    shared.parent.mkdir()
    shared.write_text("x" * 100_000, encoding="utf-8")
    plan = build_verification_plan(
        candidate_fingerprint="a" * 64,
        contract_sha256="b" * 64,
        verifier_version="v1",
        verifier_configuration={},
        viewport={"width": 800, "height": 600},
        routes=[{"id": "home", "path": "/"}],
        controls=[{"id": "one", "source_path": "/"}, {"id": "two", "source_path": "/"}],
        candidate_identity_sha256="c" * 64,
        analyzer_implementation_sha256="d" * 64,
        shared_artifacts={"inventory": shared_artifact_ref(shared)},
    )
    batch = partition_plan(plan, 1)[0]
    checkpoint = write_checkpoint(
        tmp_path / "batch.json",
        batch,
        status="passed",
        result={"status": "passed", "outcomes": [{"id": "home", "passed": True}]},
    )
    assert checkpoint["candidate_identity_sha256"] == "c" * 64
    assert checkpoint["analyzer_implementation_sha256"] == "d" * 64
    assert checkpoint["shared_artifacts"]["inventory"]["bytes"] == 100_000
    assert "closure" not in checkpoint["result"]
    assert (tmp_path / "batch.json").stat().st_size < 10_000


def test_shared_binding_changes_plan_for_analyzer_or_shared_hash():
    base = _plan()
    changed_analyzer = build_verification_plan(
        candidate_fingerprint="a" * 64,
        contract_sha256="b" * 64,
        verifier_version="v1",
        verifier_configuration={"timeout_ms": 10},
        viewport={"width": 1440, "height": 1000},
        routes=[{"id": "route-home", "path": "/"}, {"id": "route-detail", "path": "/detail"}],
        controls=[{"id": "control-search", "source_path": "/"}, {"id": "control-menu", "source_path": "/"}],
        analyzer_implementation_sha256="e" * 64,
    )
    assert changed_analyzer["plan_identity"] != base["plan_identity"]


def test_positive_five_batch_search_return_is_authoritative(tmp_path: Path):
    """Regression: a control-only batch must seed its own source route.

    ``search-return`` is the fourth control in batch-000003 with batch size 4.
    Before the fix, that worker only crawled ``/`` and emitted
    ``not_discovered`` despite every assigned outcome being otherwise valid.
    """
    browser = _browser_asset()
    if browser is None:
        pytest.skip("external Chromium asset is not configured")
    workspace = tmp_path / "candidate"
    contract = prepare_workspace(workspace)
    with running_fixture() as base_url:
        args = Namespace(
            candidate_workspace=workspace,
            site_path=workspace / "sites/demo",
            site="demo",
            contract=contract,
            evidence_dir=tmp_path / "evidence",
            base_url=base_url,
            launch_config=None,
            viewport_width=1440,
            viewport_height=1000,
            gateway_prefix=None,
            browser_path=browser,
            dependency_path=Path(sys.prefix),
            temp_dir=None,
            max_pages=None,
            max_depth=None,
            timeout_ms=5000,
            route_map_timeout_seconds=10,
            launch_timeout=10.0,
            batch_size=4,
            checkpoint_dir=tmp_path / "checkpoints",
            resume=False,
            candidate_freeze=None,
        )
        result = terminal.run(args)
    assert result["status"] == "passed"
    assert result["exhaustive"]["merge"]["status"] == "passed"
    assert result["exhaustive"]["merge"]["expected_batch_count"] == 5
    batch = json.loads((tmp_path / "checkpoints/batch-000003.json").read_text(encoding="utf-8"))
    assert batch["status"] == "passed"
    search_return = next(item for item in batch["result"]["outcomes"] if item["id"] == "search-return")
    assert search_return["passed"] is True
    closure = json.loads((tmp_path / "evidence/interaction-closure.json").read_text(encoding="utf-8"))
    assert closure["status"] == "passed"
    assert closure["interaction_diff"]["hard_gap_count"] == 0
    assert closure["failures"] == []


def test_terminal_rejects_changed_freeze_configuration_digest(tmp_path: Path):
    workspace = tmp_path / "workspace"
    site = workspace / "sites/demo"
    site.mkdir(parents=True)
    (site / "app.py").write_text("app = 1\n", encoding="utf-8")
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"routes": [{"id": "home", "path": "/"}]}), encoding="utf-8")
    freeze = _freeze(site, contract)
    freeze["identity"]["configuration"]["profile"] = "task-driven"
    path = tmp_path / "candidate-freeze.json"
    path.write_text(json.dumps(freeze), encoding="utf-8")
    args = Namespace(
        candidate_workspace=workspace, site_path=site, site="demo", contract=contract,
        evidence_dir=tmp_path / "evidence", base_url="http://127.0.0.1:1/", launch_config=None,
        viewport_width=1440, viewport_height=1000, gateway_prefix=None, browser_path=None,
        dependency_path=None, temp_dir=None, max_pages=10, max_depth=2, timeout_ms=100,
        route_map_timeout_seconds=1, launch_timeout=1.0, batch_size=1,
        checkpoint_dir=tmp_path / "checkpoints", resume=True, candidate_freeze=path,
    )
    with pytest.raises(ValueError, match="identity digest"):
        terminal.run(args)
