from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_task_gate import main, validate


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def task_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    request = tmp_path / "task-request.json"
    builder = tmp_path / "builder"
    verifier = tmp_path / "verifier"
    write(request, {"query_sha256": "abc"})
    write(builder / "task-contract.json", {
        "site_id": "demo", "task_query_sha256": "abc",
        "steps": [
            {"id": "find", "source_route": "/", "local_route": "/", "action": "filter",
             "target": "Accordion", "expected_state": "one match"},
            {"id": "open", "source_route": "/", "local_route": "/accordion",
             "action": "click", "target": "Accordion", "expected_state": "detail page"},
        ],
        "terminal_expected_state": "second example expanded",
    })
    write(verifier / "verifier-result.json", {
        "site_id": "demo", "status": "passed", "gates": {"task_workflow": {"status": "passed"}},
    })
    (verifier / "proof.txt").write_text("browser evidence", encoding="utf-8")
    write(verifier / "task-coverage.json", {
        "site_id": "demo", "task_query_sha256": "abc", "status": "passed",
        "steps": [
            {"id": "find", "status": "passed", "action": "filter", "assertion": "one match",
             "evidence_paths": ["proof.txt"]},
            {"id": "open", "status": "passed", "action": "click", "assertion": "detail page",
             "evidence_paths": ["proof.txt"]},
        ],
        "terminal_assertion": {"status": "passed", "expected": "expanded", "observed": "expanded",
                               "evidence_paths": ["proof.txt"]},
    })
    return request, builder, verifier


def test_valid_task_coverage_requires_order_and_persisted_evidence(tmp_path: Path):
    request, builder, verifier = task_fixture(tmp_path)
    assert validate(request, builder, verifier) == []
    coverage_path = verifier / "task-coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["steps"].reverse()
    coverage["steps"][0]["evidence_paths"] = ["../outside.txt"]
    write(coverage_path, coverage)
    failures = validate(request, builder, verifier)
    assert any("in order" in failure for failure in failures)
    assert any("persisted evidence" in failure for failure in failures)


def test_structured_browser_assertions_are_valid(tmp_path: Path):
    request, builder, verifier = task_fixture(tmp_path)
    coverage_path = verifier / "task-coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["steps"][0]["assertion"] = {"url": "/", "matches": 1}
    coverage["terminal_assertion"]["observed"] = {"expanded": True}
    write(coverage_path, coverage)
    assert validate(request, builder, verifier) == []
    coverage["steps"][0]["assertion"] = {}
    write(coverage_path, coverage)
    assert any("passed assertion" in failure for failure in validate(request, builder, verifier))


def test_missing_task_coverage_turns_report_into_failed_gate(tmp_path: Path, monkeypatch):
    request, builder, verifier = task_fixture(tmp_path)
    (verifier / "task-coverage.json").unlink()
    monkeypatch.setattr("sys.argv", ["validate_task_gate.py", str(request), str(builder), str(verifier)])
    assert main() == 1
    result = json.loads((verifier / "verifier-result.json").read_text(encoding="utf-8"))
    repair = json.loads((verifier / "repair-package.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert repair["failures"][0]["gate"] == "task_workflow"
