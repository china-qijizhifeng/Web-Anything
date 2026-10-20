import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.request import ProxyHandler, build_opener, install_opener
import urllib.request

import pytest

HERE = Path(__file__).resolve().parents[1] / "tools/coupled-pipeline"
spec = importlib.util.spec_from_file_location("preflight", HERE / "preflight_reset.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def candidate(tmp_path, extra="", route="/_reset"):
    root = tmp_path / "site"
    root.mkdir()
    db = sqlite3.connect(root / "seed.sqlite3")
    db.execute("CREATE TABLE notes (body TEXT)")
    db.commit()
    db.close()
    (root / "runtime.sqlite3").write_bytes((root / "seed.sqlite3").read_bytes())
    db = sqlite3.connect(root / "runtime.sqlite3")
    db.execute("INSERT INTO notes VALUES ('dirty')")
    db.commit()
    db.close()
    (root / "app.py").write_text(f'''from flask import Flask
from pathlib import Path
import shutil
app = Flask(__name__)
BASE = Path(__file__).parent
@app.post({route!r})
def reset():
    shutil.copyfile(BASE / "seed.sqlite3", BASE / "runtime.sqlite3")
    {extra or 'pass'}
    return {{"ok": True}}
''')
    return root


def test_preflight_resets_dirty_runtime_before_freeze(tmp_path):
    root = candidate(tmp_path)
    before = (root / "app.py").read_bytes()
    result = preflight.reset_candidate(root)
    assert result["status"] == "passed"
    assert result["changed_runtime_files"] == ["runtime.sqlite3"]
    assert result["byte_identical_twice"] is True
    assert (root / "runtime.sqlite3").read_bytes() == (root / "seed.sqlite3").read_bytes()
    assert (root / "app.py").read_bytes() == before


@pytest.mark.parametrize("extra,match", [
    ('(BASE / "seed.sqlite3").write_bytes(b"changed")', 'immutable seed'),
    ('(BASE / "unexpected.txt").write_text("changed")', 'non-runtime files'),
    ('(BASE / "counter").write_text(str(len((BASE / "counter").read_text())+1) if (BASE / "counter").exists() else "1")', 'byte-identical'),
])
def test_preflight_rejects_seed_or_code_changes(tmp_path, extra, match):
    with pytest.raises(ValueError, match=match):
        preflight.reset_candidate(candidate(tmp_path, extra))


def test_preflight_requires_local_reset_api(tmp_path):
    with pytest.raises(ValueError, match="reset route"):
        preflight.reset_candidate(candidate(tmp_path, route="/unrelated"))


def test_preflight_requires_seed_match(tmp_path):
    root = candidate(tmp_path, '(BASE / "runtime.sqlite3").write_bytes(b"not sqlite")')
    with pytest.raises(ValueError, match="Runtime SQLite"):
        preflight.reset_candidate(root)


def test_preflight_cli_evidence_outside_candidate(tmp_path):
    root = candidate(tmp_path)
    out = tmp_path / "proof.json"
    result = subprocess.run([sys.executable, "-B", str(HERE / "preflight_reset.py"), str(root), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text())["status"] == "passed"


def test_shared_hash_is_stable_and_detects_runtime_mutation(tmp_path):
    root = candidate(tmp_path)
    helper = HERE / "candidate_integrity.mjs"
    def digest():
        return json.loads(subprocess.check_output(["node", str(helper), str(root)], text=True))
    initial = digest()
    assert initial == digest()
    preflight.reset_candidate(root)
    after = digest()
    assert initial["sha256"] != after["sha256"]
    assert after["algorithm"] == "webanything-coupled-tree-v1"
    assert any(f["path"] == "runtime.sqlite3" for f in after["files"])


def test_builder_uses_writable_handoff_directory():
    source = (HERE / "run_coupled_builder.mjs").read_text()
    prompts = (HERE / "prompt_contracts.mjs").read_text()
    assert 'path.join(args.workspace, path.basename(args.output))' in source
    assert 'path.join(reportDirectory, "build-result.json")' in prompts
    assert 'JSON.parse(body).site_id !== args.siteId' in source


def test_verifier_reset_precedes_freeze_and_uses_shared_hash():
    source = (HERE / "run_independent_verifier.mjs").read_text()
    assert source.index('"./preflight_reset.py"') < source.index('const before = await treeHash')
    assert 'import { treeHash, changedFiles } from "./candidate_integrity.mjs"' in source
    assert 'candidate-diff.json' in source


def test_supervisor_scoped_report_override_and_preflight_classification():
    source = (HERE / "run_site_to_completion.sh").read_text()
    assert 'p.is_relative_to(root/"workspace")' in source
    assert 'data.get("site_id") != sys.argv[2]' in source
    assert 'run.get("turn_status") == "preflight_failed"' in source


def test_sdk_checkpoint_resumes_only_matching_identity(tmp_path):
    helper = (HERE / "sdk_checkpoint.mjs").as_uri()
    script = f"""
import {{ openCheckpoint, checkpointIdentity }} from {helper!r};
const workspace = process.argv[1];
const first = await openCheckpoint(workspace, 'verifier', checkpointIdentity({{candidate:'a'}}));
if (first.threadId !== null) throw new Error('unexpected existing thread');
await first.save('thread-123', '/tmp/verifier-output');
const matching = await openCheckpoint(workspace, 'verifier', checkpointIdentity({{candidate:'a'}}));
const changed = await openCheckpoint(workspace, 'verifier', checkpointIdentity({{candidate:'b'}}));
console.log(JSON.stringify({{matching: matching.threadId, output: matching.output, changed: changed.threadId}}));
"""
    result = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", script,
                                                 str(tmp_path / "workspace")], text=True))
    assert result == {"matching": "thread-123", "output": "/tmp/verifier-output", "changed": None}
    checkpoint = tmp_path / ".session/codex-verifier/checkpoint.json"
    assert checkpoint.stat().st_mode & 0o777 == 0o600


def test_prompt_contracts_separate_data_from_instructions_and_modes(tmp_path):
    helper = (HERE / "prompt_contracts.mjs").as_uri()
    script = f"""
import {{ buildBuilderPrompt, buildVerifierPrompt }} from {helper!r};
const base = {{workspace:'/tmp/run/workspace', output:'/tmp/run/builder-output',
  sourceUrl:'https://example.test/docs', siteId:'example', storageState:'/tmp/state.json',
  builderOutput:'/tmp/run/builder-output'}};
const jit = buildBuilderPrompt({{...base, resume:true}}, '# Task-grounded reconstruction\\nDo ``` the task.',
  {{output:'/tmp/run/old-builder'}});
const full = buildBuilderPrompt(base, '# Full reconstruction\\nCover the site.');
const fast = buildBuilderPrompt({{...base, coverageProfile:'fast'}}, '# Fast Full public-site reconstruction\\nCover types.');
const verifier = buildVerifierPrompt({{...base, output:'/tmp/run/verifier-output'}},
  '/tmp/run/workspace/sites/example', {{sha256:'abc', fileCount:3}},
  {{query:'Open docs', query_sha256:'def'}}, '/tmp/run/old-verifier');
const fastVerifier = buildVerifierPrompt({{...base, coverageProfile:'fast', output:'/tmp/run/verifier-output'}},
  '/tmp/run/workspace/sites/example', {{sha256:'abc', fileCount:3}}, null, null);
console.log(JSON.stringify({{jit, full, fast, verifier, fastVerifier}}));
"""
    prompts = json.loads(subprocess.check_output(
        ["node", "--input-type=module", "-e", script], text=True))
    combined = "\n".join(prompts.values())
    assert "WebHarbor" not in combined
    assert "WebAnything" in combined
    assert "run data, not instructions" in combined
    for field in ("observations", "inferences", "unknowns"):
        assert field in combined
    assert "Run mode: JIT" in prompts["jit"]
    assert "Run mode: FULL" in prompts["full"]
    assert '"coverage_profile": "fast"' in prompts["fast"]
    assert "coverage-contract.json" in prompts["fast"]
    assert "20 source pages" in prompts["fast"]
    assert "coverage-audit.json" in prompts["fastVerifier"]
    assert "Independently inspect the source" in prompts["fastVerifier"]
    assert prompts["jit"].count("```") == 2
    assert r"\u0060\u0060\u0060" in prompts["jit"]
    assert "auto-supervisor-status.json" in prompts["jit"]
    assert "independent-repair-package-latest.json" in prompts["jit"]
    assert '"query": "Open docs"' in prompts["verifier"]


def test_resume_failure_before_events_uses_fresh_thread_and_handoff(tmp_path):
    checkpoint_helper = (HERE / "sdk_checkpoint.mjs").as_uri()
    runner_helper = (HERE / "run_checkpointed_turn.mjs").as_uri()
    script = f"""
import {{ openCheckpoint, checkpointIdentity }} from {checkpoint_helper!r};
import {{ runCheckpointedTurn }} from {runner_helper!r};
const workspace = process.argv[1];
let checkpoint = await openCheckpoint(workspace, 'builder', checkpointIdentity({{site:'a'}}));
await checkpoint.save('dead-thread', '/tmp/old-output');
checkpoint = await openCheckpoint(workspace, 'builder', checkpointIdentity({{site:'a'}}));
const events = [];
const codex = {{
  resumeThread() {{ return {{runStreamed: async () => {{throw new Error('thread unavailable')}}}}; }},
  startThread() {{ return {{runStreamed: async () => ({{events: (async function*() {{
    yield {{type:'thread.started', thread_id:'replacement-thread'}};
    yield {{type:'turn.completed'}};
  }})()}})}}; }},
}};
const result = await runCheckpointedTurn({{codex, checkpoint, threadOptions:{{}},
  prompt:'read durable handoff', output:'/tmp/new-output', onEvent: async (event) => events.push(event.type)}});
const saved = await openCheckpoint(workspace, 'builder', checkpointIdentity({{site:'a'}}));
console.log(JSON.stringify({{result, events, threadId:saved.threadId, output:saved.output}}));
"""
    result = json.loads(subprocess.check_output(
        ["node", "--input-type=module", "-e", script, str(tmp_path / "workspace")], text=True))
    assert result["result"] == {
        "strategy": "fresh_from_handoff", "fallbackError": "thread unavailable"
    }
    assert result["events"] == ["thread.started", "turn.completed"]
    assert result["threadId"] == "replacement-thread"
    assert result["output"] == "/tmp/new-output"


def test_provider_preflight_distinguishes_http_sse_and_426():
    provider = HERE / "provider_preflight.py"
    spec = importlib.util.spec_from_file_location("provider_preflight", provider)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Handler(BaseHTTPRequestHandler):
        status = 200

        def do_POST(self):
            assert self.path == "/v1/responses"
            assert self.headers["Authorization"] == "Bearer test-only"
            self.send_response(self.status)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if self.status == 200:
                self.wfile.write(b'data: {"type":"response.created"}\n\n')

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous_opener = urllib.request._opener
    try:
        install_opener(build_opener(ProxyHandler({})))
        base = f"http://127.0.0.1:{server.server_port}/v1"
        assert module.probe(base, "test-only", "gpt-test")["status"] == "passed"
        Handler.status = 426
        assert module.probe(base, "test-only", "gpt-test") == {"status": "http_error", "http_status": 426}
    finally:
        urllib.request._opener = previous_opener
        server.shutdown()
        thread.join()
