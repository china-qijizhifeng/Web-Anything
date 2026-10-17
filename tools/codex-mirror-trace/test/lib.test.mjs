import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, symlink, utimes, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import {
  EventRecorder,
  auditEventLifecycle,
  auditTarArchiveSecrets,
  buildExecutionPrompt,
  buildRepairPrompt,
  buildRepairReflectionPrompt,
  buildReflectionPrompt,
  buildSyntheticExecutionPrompt,
  buildSyntheticReflectionPrompt,
  buildSyntheticRepairPrompt,
  buildSyntheticReplayExecutionPrompt,
  buildSyntheticReplayReflectionPrompt,
  buildSyntheticReplayRepairPrompt,
  createDeterministicTarGzip,
  diffInventories,
  inventoryTree,
  isBuilderRepairPackage,
  isTransientMirrorArtifactPath,
  parseAgentProfile,
  reconstructBaselineInventory,
  redactCredentialText,
  redactValue,
  sanitizeExactSecrets,
  selectMirrorArtifactPaths,
  verifyTraceArtifactBinding,
} from "../lib.mjs";

const execFileAsync = promisify(execFile);
const toolDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("trace entry points import every shared finalizer helper they call", async () => {
  for (const name of ["capture.mjs", "continue.mjs"]) {
    const source = await readFile(path.join(toolDirectory, name), "utf8");
    assert.match(source, /\bsanitizeExactSecrets,\s*\n\s*writePrivateJson,/);
    assert.match(source, /await sanitizeExactSecrets\(/);
  }
});

test("trace entry points disable SDK web search while retaining network tools", async () => {
  for (const name of ["capture.mjs", "continue.mjs"]) {
    const source = await readFile(path.join(toolDirectory, name), "utf8");
    assert.match(source, /networkAccessEnabled:\s*true/);
    assert.match(source, /webSearchMode:\s*"disabled"/);
    assert.match(source, /webSearchEnabled:\s*false/);
    assert.doesNotMatch(source, /webSearchMode:\s*"live"/);
  }
});

test("repair can explicitly archive and reset an incomplete prior trace", async () => {
  const source = await readFile(path.join(toolDirectory, "continue.mjs"), "utf8");
  assert.match(source, /--reset-trace/);
  assert.match(source, /trace-history/);
  assert.match(source, /append:\s*!args\.resetTrace/);
  assert.match(source, /prior_trace_archived:\s*true/);
});

test("synthetic capture copies only the hash-bound frozen visual reference", async () => {
  const source = await readFile(path.join(toolDirectory, "capture.mjs"), "utf8");
  assert.match(source, /contract\.visual_reference\?\.required !== true/);
  assert.match(source, /reference\?\.raw_material_persisted !== true/);
  assert.match(source, /route\?\.comparison_screenshot/);
  assert.match(source, /visual_reference\.artifact\[\$\{index\}\]/);
  assert.match(source, /synthetic visual reference artifact hash mismatch/);
  assert.match(source, /\.webanything["'], "synthetic-input"/);
  assert.doesNotMatch(source, /storage-state|auth-profile|raw-screenshot/i);
});

test("recovers a failed initial baseline only inside explicit scopes", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "trace-recover-"));
  const baselineRoot = path.join(directory, "baseline");
  const runDirectory = path.join(directory, "run");
  const workspace = path.join(runDirectory, "workspace");
  await mkdir(path.join(baselineRoot, "site"), { recursive: true });
  await mkdir(path.join(workspace, "site"), { recursive: true });
  await writeFile(path.join(baselineRoot, "catalog.json"), "old\n");
  await writeFile(path.join(baselineRoot, "site", "app.py"), "old\n");
  await writeFile(path.join(workspace, "catalog.json"), "new\n");
  await writeFile(path.join(workspace, "site", "app.py"), "new\n");
  await writeFile(path.join(workspace, "site", "seed.db"), "seed\n");
  await writeFile(path.join(workspace, "outside.txt"), "workspace-only\n");
  await writeFile(path.join(runDirectory, "run.json"), '{"fatal_error":"old finalizer failure"}\n');

  await execFileAsync(process.execPath, [
    path.join(toolDirectory, "recover-baseline.mjs"), runDirectory,
    "--baseline-root", baselineRoot,
    "--scope", "catalog.json",
    "--scope", "site",
  ]);

  const changes = JSON.parse(await readFile(path.join(runDirectory, "changed-files.json"), "utf8"));
  assert.deepEqual(changes.map((entry) => entry.path), ["catalog.json", "site/app.py", "site/seed.db"]);
  assert.ok(changes.every((entry) => entry.path !== "outside.txt"));
  const run = JSON.parse(await readFile(path.join(runDirectory, "run.json"), "utf8"));
  assert.equal(run.changed_files, 3);
  assert.equal(run.finalization_recovery.status, "staged_for_continuation");
  assert.equal(run.finalization_recovery.previous_fatal_error, "old finalizer failure");
});

test("recursively redacts structured and embedded secrets", () => {
  const secret = "sk-test-super-secret";
  const value = redactValue({
    api_key: secret,
    usage: { input_tokens: 12 },
    command: `curl -H 'Authorization: Bearer ${secret}' https://alice:password@example.com/`,
    nested: [{ cookie: "session=abc" }, `value=${secret}`],
  }, [secret]);
  assert.equal(value.api_key, "[REDACTED]");
  assert.equal(value.usage.input_tokens, 12);
  assert.doesNotMatch(value.command, /super-secret|alice:password/);
  assert.equal(value.nested[0].cookie, "[REDACTED]");
  assert.equal(value.nested[1], "value=[REDACTED]");
});

test("redacts Sentry ingestion credentials with stable fingerprints", () => {
  const sentryKey = "0123456789abcdef0123456789abcdef";
  const value = redactValue({
    request: `https://o1.ingest.sentry.io/api/1/store/?sentry_key=${sentryKey}&sentry_version=7`,
    endpoint: `https://${sentryKey}@o1.ingest.sentry.io/1`,
  });
  assert.doesNotMatch(value.request, new RegExp(sentryKey));
  assert.doesNotMatch(value.endpoint, new RegExp(sentryKey));
  const requestFingerprint = value.request.match(/\[REDACTED_SENTRY_[0-9a-f]{12}\]/)?.[0];
  assert.ok(requestFingerprint);
  assert.equal(value.endpoint.match(/\[REDACTED_SENTRY_[0-9a-f]{12}\]/)?.[0], requestFingerprint);
});

test("redacts both Segment environment keys with stable fingerprints", () => {
  const production = "Ab3Def5Gh7Jk9Mn2Pq4Rs6Tu8Vw1Xy3Z";
  const staging = "Zy9Xw7Vu5Ts3Rq1Pn8Mk6Jh4Gf2Ed0Cb";
  const source = `SEGMENT_WRITE_KEY",0,"production"===n?"${production}":"${staging}"; analytics._writeKey = '${production}'`;
  const redacted = redactCredentialText(source);
  assert.doesNotMatch(redacted, new RegExp(production));
  assert.doesNotMatch(redacted, new RegExp(staging));
  assert.equal((redacted.match(/\[REDACTED_SEGMENT_[0-9a-f]{12}\]/g) ?? []).length, 3);
});

test("redacts response and request cookie headers in raw and JSON-escaped output", () => {
  const firstCookie = "session=temporary-value-123456789; Path=/; HttpOnly";
  const secondCookie = "csrf=temporary-value-987654321; Path=/";
  const raw = `HTTP/1.1 302 Found\r\nSet-Cookie: ${firstCookie}\r\nCookie: ${secondCookie}\r\nLocation: /next\r\n`;
  const redactedRaw = redactCredentialText(raw);
  assert.doesNotMatch(redactedRaw, /temporary-value/);
  assert.match(redactedRaw, /Set-Cookie: \[REDACTED\]/);
  assert.match(redactedRaw, /Cookie: \[REDACTED\]/);
  assert.match(redactedRaw, /Location: \/next/);

  const serialized = JSON.stringify({ aggregated_output: raw });
  const redactedSerialized = redactCredentialText(serialized);
  assert.doesNotMatch(redactedSerialized, /temporary-value/);
  assert.deepEqual(JSON.parse(redactedSerialized), {
    aggregated_output: "HTTP/1.1 302 Found\r\nSet-Cookie: [REDACTED]\r\nCookie: [REDACTED]\r\nLocation: /next\r\n",
  });
});

test("parses the model and reasoning profile", () => {
  const profile = parseAgentProfile(`llm_config:\n  model: gpt-5.6-sol\n  reasoning:\n    effort: xhigh\n    summary: detailed\n`);
  assert.deepEqual(profile, { model: "gpt-5.6-sol", effort: "xhigh", summary: "detailed" });
});

test("recognizes complete Builder repair packages before allowing the larger continuation limit", () => {
  const packageValue = {
    schema_version: 1,
    repair_package: "webharbor-builder-repair-package-v1",
    site: "demo",
    status: "repair_required",
    candidate_fingerprint: { algorithm: "webanything-site-tree-v1", sha256: "a".repeat(64) },
    failure_count: 1,
    group_count: 1,
    groups: [{
      route_family: "/",
      page_archetype: "home",
      control_type: "link",
      failure_codes: ["missing_destination"],
      items: [{ code: "missing_destination", route: "/" }],
    }],
  };
  assert.equal(isBuilderRepairPackage(packageValue), true);
  assert.equal(isBuilderRepairPackage({ ...packageValue, failure_count: 2 }), false);
  assert.equal(isBuilderRepairPackage({ ...packageValue, candidate_fingerprint: { algorithm: "x", sha256: "not-a-hash" } }), false);
});

test("prompts constrain construction and reflection directories", () => {
  const execution = buildExecutionPrompt({
    url: "https://example.com/",
    maxPages: 3,
    maxAssets: 4,
    supplementalUrls: ["https://jobs.example.net/apply"],
    requirements: "Store a local form submission.",
  });
  assert.match(execution, /\$build-webharbor-mirrors/);
  assert.match(execution, /--max-pages 3 --max-assets 4/);
  assert.match(execution, /Never modify or write to the source/);
  assert.match(execution, /https:\/\/jobs\.example\.net\/apply/);
  assert.match(execution, /Store a local form submission/);
  assert.match(execution, /Never POST to an upstream form/);
  assert.match(execution, /Never remove it or put it in excluded_routes/);
  assert.match(execution, /audit-candidate-resources/);
  assert.match(execution, /--require-current-quality/);
  assert.match(execution, /quality grade rejected is not completion/);
  const reflection = buildReflectionPrompt({ proposalPath: "skill-proposal/build-webharbor-mirrors", executionStatus: "completed" });
  assert.match(reflection, /advisory/);
  assert.match(reflection, /must not edit \.agents\/skills\/build-webharbor-mirrors/);
  assert.match(reflection, /at most eight tool calls/);
  assert.match(reflection, /Do not rerun the upstream build/);
});

test("repair prompts require evidence-backed fixes and proposal isolation", () => {
  const repair = buildRepairPrompt({
    site: "example_trace",
    requirements: "Include the captured application route in matched fidelity.",
  });
  assert.match(repair, /Target site id: example_trace/);
  assert.match(repair, /matched desktop fidelity/);
  assert.match(repair, /Never issue an upstream POST/);
  assert.match(repair, /Do not relabel a failed result as passed/);
  assert.match(repair, /Never remove it or put it in excluded_routes/);
  assert.match(repair, /audit-candidate-resources/);
  assert.match(repair, /inspection-only candidate/);
  const reflection = buildRepairReflectionPrompt({
    proposalPath: "skill-proposal/build-webharbor-mirrors",
    repairStatus: "completed",
  });
  assert.match(reflection, /edit only that proposal directory/);
  assert.match(reflection, /at most eight tool calls/);
  assert.match(reflection, /Do not rerun capture/);
});

test("synthetic prompts preserve the frozen contract and support frozen source fidelity", () => {
  const execution = buildSyntheticExecutionPrompt({
    url: "https://seed.example/",
    site: "cloudpost",
    requirements: "Implement every fixed task.",
  });
  assert.match(execution, /\$build-synthetic-benchmarks/);
  assert.match(execution, /\.webanything\/synthetic-input\/synthetic_contract\.json/);
  assert.match(execution, /synthetic_visual_reference\.json/);
  assert.match(execution, /frozen source-visible screenshot, content-neutral comparison artifact, and text-free geometry/);
  assert.match(execution, /Do not fetch, crawl, or screenshot the live upstream/);
  assert.match(execution, /content_policy\.mode/);
  assert.match(execution, /For source_fidelity, reproduce the visible brand, text, media/);
  assert.match(execution, /never ship the whole reference screenshot as a page-sized overlay/);
  assert.match(execution, /write the candidate only under sites\/cloudpost/);
  assert.match(execution, /Never weaken or rewrite the contract/);

  const repair = buildSyntheticRepairPrompt({ site: "cloudpost", requirements: "Fix reset replay." });
  assert.match(repair, /Preserve the frozen contract byte-for-byte/);
  assert.match(repair, /Preserve the frozen visual reference byte-for-byte/);
  assert.match(repair, /Never seek the live source, credentials, screenshots beyond the frozen source-visible reference, or raw DOM/);
  assert.match(repair, /source_fidelity repairs must preserve visible source brand, text, media/);
  assert.match(repair, /Work only under sites\/cloudpost/);
  assert.match(repair, /localStorage namespace/);

  const reflection = buildSyntheticReflectionPrompt({
    proposalPath: "skill-proposal/build-synthetic-benchmarks",
    executionStatus: "completed",
  });
  assert.match(reflection, /advisory only/);
  assert.match(reflection, /must not edit the production skill, frozen contract, candidate, verifier/);
  assert.match(reflection, /at most eight tool calls/);
});

test("synthetic replay prompts preserve capture, SQLite, auth, reset, and isolation gates", () => {
  const execution = buildSyntheticReplayExecutionPrompt({
    url: "https://www.example.test/",
    site: "video_replay",
    requirements: "Implement the frozen tasks.",
  });
  assert.match(execution, /\$build-synthetic-replays/);
  assert.match(execution, /\.webanything\/replay-input\/synthetic_replay_contract\.json/);
  assert.match(execution, /Never access the live seed URL or source authentication/);
  assert.match(execution, /orchestrator-seeded sites\/video_replay\/app\.py/);
  assert.match(execution, /preserve those handlers and inject Flask\/SQLite semantics into that DOM/);
  assert.match(execution, /Preserve every captured data-action-id/);
  assert.match(execution, /HTTP 4xx action targets/);
  assert.match(execution, /Flask\/SQLite/);
  assert.match(execution, /max_parallel_sessions=1/);
  assert.match(execution, /\/p\/40000/);
  assert.match(execution, /one bounded pass/);
  assert.match(execution, /excluded visible-but-occluded controls with browser hit-testing/);
  assert.match(execution, /records a source hit_point/);
  assert.match(execution, /independent visual verifier owns visual comparison/);
  assert.match(execution, /automatic checkpoint restart/);
  const repair = buildSyntheticReplayRepairPrompt({ site: "video_replay", requirements: "Fix evaluator state.", recoveryRetry: true });
  assert.match(repair, /Preserve the frozen contract, capture bundle, visual reference, SQLite seed/);
  assert.match(repair, /Work only under sites\/video_replay/);
  assert.match(repair, /recovery retry/);
  assert.match(repair, /Reuse the prior trace's proven findings/);
  assert.match(repair, /at most six investigative tool calls before the first candidate edit/);
  assert.match(repair, /one bounded affected-route smoke pass/);
  assert.match(repair, /Never start Flask or another long-running command in the foreground/);
  const reflection = buildSyntheticReplayReflectionPrompt({
    proposalPath: "skill-proposal/build-synthetic-replays",
    executionStatus: "completed",
  });
  assert.match(reflection, /independent skill proposal/);
  assert.match(reflection, /concurrency isolation/);
});

test("records SDK events into raw, message, and tool projections", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-trace-test-"));
  const recorder = new EventRecorder(directory, ["secret-value"]);
  await recorder.initialize();
  await recorder.recordEvent("mirror", { type: "item.completed", item: { id: "r1", type: "reasoning", text: "summary" } });
  await recorder.recordEvent("mirror", { type: "item.completed", item: { id: "c1", type: "command_execution", command: "echo secret-value", aggregated_output: "secret-value", status: "completed", exit_code: 0 } });
  await recorder.recordEvent("mirror", { type: "item.completed", item: { id: "a1", type: "agent_message", text: "done" } });
  assert.equal(recorder.finalResponse("mirror"), "done");
  await recorder.close();
  assert.match(await readFile(path.join(directory, "messages.jsonl"), "utf8"), /reasoning_summary/);
  const tools = await readFile(path.join(directory, "tool-calls.jsonl"), "utf8");
  assert.match(tools, /command_execution/);
  assert.doesNotMatch(tools, /secret-value/);
  assert.equal((await readFile(path.join(directory, "events.jsonl"), "utf8")).trim().split("\n").length, 3);
});

test("continues event sequence when appending a repair turn", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-trace-resume-test-"));
  const initial = new EventRecorder(directory);
  await initial.initialize();
  await initial.recordEvent("mirror", { type: "turn.started" });
  await initial.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await initial.close();
  const continuation = new EventRecorder(directory);
  await continuation.initialize();
  await continuation.recordEvent("repair-1", { type: "turn.started" });
  await continuation.close();
  const rows = (await readFile(path.join(directory, "events.jsonl"), "utf8"))
    .trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(rows.map((row) => row.sequence), [1, 2, 3]);
});

test("starts a fresh event sequence for a retried initial capture", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-trace-retry-test-"));
  const initial = new EventRecorder(directory);
  await initial.initialize();
  await initial.recordEvent("synthetic-replay", { type: "turn.started" });
  await initial.recordEvent("synthetic-replay", { type: "turn.completed", usage: {} });
  await initial.close();

  const retry = new EventRecorder(directory, [], { append: false });
  await retry.initialize();
  await retry.recordEvent("synthetic-replay", { type: "turn.started" });
  await retry.close();

  const rows = (await readFile(path.join(directory, "events.jsonl"), "utf8"))
    .trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(rows.map((row) => row.sequence), [1]);
});

test("audits sealed trace lifecycle and discloses SDK event loss", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-lifecycle-test-"));
  const recorder = new EventRecorder(directory);
  await recorder.initialize();
  await recorder.recordPrompt("mirror", "build the mirror");
  await recorder.recordEvent("mirror", { type: "turn.started" });
  await recorder.recordEvent("mirror", {
    type: "item.started",
    item: { id: "search-1", type: "web_search", query: "example" },
  });
  await recorder.recordEvent("mirror", {
    type: "item.completed",
    item: { id: "error-1", type: "error", message: "event stream lagged; dropped 7 events" },
  });
  await recorder.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await recorder.close();
  const audit = await auditEventLifecycle(directory);
  assert.equal(audit.status, "incomplete");
  assert.equal(audit.dropped_event_count, 7);
  assert.deepEqual(audit.sequence_gaps, []);
  assert.equal(audit.phases[0].closed, true);
  assert.equal(audit.orphan_started_items[0].id, "search-1");
  assert.match(audit.failures.join("\n"), /SDK reported 7 dropped event/);
});

test("blocks orphan items, conflicting terminals, and events after a terminal", async () => {
  const orphanDirectory = await mkdtemp(path.join(os.tmpdir(), "codex-orphan-test-"));
  const orphanRecorder = new EventRecorder(orphanDirectory);
  await orphanRecorder.initialize();
  await orphanRecorder.recordPrompt("mirror", "build");
  await orphanRecorder.recordEvent("mirror", { type: "turn.started" });
  await orphanRecorder.recordEvent("mirror", { type: "item.started", item: { id: "cmd-1", type: "command_execution", command: "true" } });
  await orphanRecorder.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await orphanRecorder.close();
  const orphanAudit = await auditEventLifecycle(orphanDirectory);
  assert.equal(orphanAudit.status, "incomplete");
  assert.match(orphanAudit.failures.join("\n"), /started SDK items lack a terminal/);

  const conflictDirectory = await mkdtemp(path.join(os.tmpdir(), "codex-terminal-conflict-test-"));
  const conflictRecorder = new EventRecorder(conflictDirectory);
  await conflictRecorder.initialize();
  await conflictRecorder.recordPrompt("mirror", "build");
  await conflictRecorder.recordEvent("mirror", { type: "turn.started" });
  await conflictRecorder.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await conflictRecorder.recordEvent("mirror", { type: "turn.failed", error: { message: "late failure" } });
  await conflictRecorder.close();
  const conflictAudit = await auditEventLifecycle(conflictDirectory);
  assert.equal(conflictAudit.status, "incomplete");
  assert.match(conflictAudit.failures.join("\n"), /conflicting or duplicate turn terminal/);
  assert.match(conflictAudit.failures.join("\n"), /events after the turn terminal/);
});

test("accepts only a fully completed todo list's final update as its protocol terminal", async () => {
  const completeDirectory = await mkdtemp(path.join(os.tmpdir(), "codex-todo-complete-test-"));
  const completeRecorder = new EventRecorder(completeDirectory);
  await completeRecorder.initialize();
  await completeRecorder.recordPrompt("mirror", "build");
  await completeRecorder.recordEvent("mirror", { type: "turn.started" });
  await completeRecorder.recordEvent("mirror", {
    type: "item.started",
    item: {
      id: "todo-1",
      type: "todo_list",
      items: [
        { text: "capture", completed: false },
        { text: "verify", completed: false },
      ],
    },
  });
  await completeRecorder.recordEvent("mirror", {
    type: "item.updated",
    item: {
      id: "todo-1",
      type: "todo_list",
      items: [
        { text: "capture", completed: true },
        { text: "verify", completed: true },
      ],
    },
  });
  await completeRecorder.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await completeRecorder.close();
  const completeAudit = await auditEventLifecycle(completeDirectory);
  assert.equal(completeAudit.status, "complete");
  assert.deepEqual(completeAudit.orphan_started_items, []);
  assert.equal(completeAudit.protocol_completed_todo_items[0].id, "todo-1");

  const incompleteDirectory = await mkdtemp(path.join(os.tmpdir(), "codex-todo-incomplete-test-"));
  const incompleteRecorder = new EventRecorder(incompleteDirectory);
  await incompleteRecorder.initialize();
  await incompleteRecorder.recordPrompt("mirror", "build");
  await incompleteRecorder.recordEvent("mirror", { type: "turn.started" });
  await incompleteRecorder.recordEvent("mirror", {
    type: "item.started",
    item: {
      id: "todo-2",
      type: "todo_list",
      items: [{ text: "verify", completed: false }],
    },
  });
  await incompleteRecorder.recordEvent("mirror", {
    type: "item.updated",
    item: {
      id: "todo-2",
      type: "todo_list",
      items: [{ text: "verify", completed: false }],
    },
  });
  await incompleteRecorder.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await incompleteRecorder.close();
  const incompleteAudit = await auditEventLifecycle(incompleteDirectory);
  assert.equal(incompleteAudit.status, "incomplete");
  assert.equal(incompleteAudit.orphan_started_items[0].id, "todo-2");
});

test("compares projection content, not only event sequence", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-projection-content-test-"));
  const recorder = new EventRecorder(directory);
  await recorder.initialize();
  await recorder.recordPrompt("mirror", "build");
  await recorder.recordEvent("mirror", { type: "turn.started" });
  await recorder.recordEvent("mirror", { type: "item.completed", item: { id: "a1", type: "agent_message", text: "authentic" } });
  await recorder.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await recorder.close();
  const messagePath = path.join(directory, "messages.jsonl");
  const row = JSON.parse((await readFile(messagePath, "utf8")).trim());
  row.text = "tampered";
  await writeFile(messagePath, `${JSON.stringify(row)}\n`);
  const audit = await auditEventLifecycle(directory);
  assert.equal(audit.status, "incomplete");
  assert.equal(audit.projections.messages.content_match, false);
});

test("detects singular dropped-event notices in structured turn failures", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-structured-drop-test-"));
  const recorder = new EventRecorder(directory);
  await recorder.initialize();
  await recorder.recordPrompt("mirror", "build");
  await recorder.recordEvent("mirror", { type: "turn.started" });
  await recorder.recordEvent("mirror", { type: "turn.failed", error: { message: "subscriber dropped 1 event" } });
  await recorder.close();
  const audit = await auditEventLifecycle(directory);
  assert.equal(audit.status, "incomplete");
  assert.equal(audit.dropped_event_count, 1);
});

test("rejects an empty sealed trace", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-empty-lifecycle-test-"));
  const recorder = new EventRecorder(directory);
  await recorder.initialize();
  await recorder.close();
  const audit = await auditEventLifecycle(directory);
  assert.equal(audit.status, "incomplete");
  assert.match(audit.failures.join("\n"), /no SDK events/);
});

test("accepts an explicit controller watchdog terminal without inventing SDK success", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-watchdog-terminal-test-"));
  const recorder = new EventRecorder(directory);
  await recorder.initialize();
  await recorder.recordPrompt("synthetic", "build");
  await recorder.recordEvent("synthetic", { type: "turn.started" });
  await recorder.recordEvent("synthetic", {
    type: "controller.turn_timed_out",
    reason: "watchdog_timeout",
    timeout_minutes: 90,
  });
  await recorder.close();
  const audit = await auditEventLifecycle(directory);
  assert.equal(audit.status, "complete");
  assert.deepEqual(audit.phases[0].terminal_events, [{
    sequence: 2,
    type: "controller.turn_timed_out",
  }]);
  assert.doesNotMatch(JSON.stringify(audit), /turn\.completed/);
});

test("buffers high-volume SDK events and flushes them on close", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-throughput-test-"));
  const recorder = new EventRecorder(directory);
  await recorder.initialize();
  await recorder.recordPrompt("mirror", "build");
  await recorder.recordEvent("mirror", { type: "turn.started" });
  for (let index = 0; index < 10_000; index += 1) {
    await recorder.recordEvent("mirror", {
      type: "item.completed",
      item: { id: `reason-${index}`, type: "reasoning", text: `summary ${index}` },
    });
  }
  await recorder.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await recorder.close();
  const audit = await auditEventLifecycle(directory);
  assert.equal(audit.status, "complete");
  assert.equal(audit.event_count, 10_002);
  assert.equal(audit.dropped_event_count, 0);
});

test("inventories and diffs mirror files", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-inventory-test-"));
  await mkdir(path.join(directory, "sites", "demo"), { recursive: true });
  await writeFile(path.join(directory, "sites", "demo", "app.py"), "one\n");
  const before = await inventoryTree(directory);
  await writeFile(path.join(directory, "sites", "demo", "app.py"), "two\n");
  await writeFile(path.join(directory, "site_catalog.json"), "{}\n");
  const after = await inventoryTree(directory);
  assert.deepEqual(diffInventories(before, after).map((entry) => [entry.path, entry.change]), [
    ["site_catalog.json", "added"],
    ["sites/demo/app.py", "modified"],
  ]);
});

test("reconstructs the original baseline from a prior final inventory", () => {
  const finalInventory = {
    "added.txt": { kind: "file", bytes: 4, mode: 0o644, sha256: "added" },
    "changed.txt": { kind: "file", bytes: 3, mode: 0o644, sha256: "after" },
  };
  const baseline = reconstructBaselineInventory(finalInventory, [
    { path: "added.txt", change: "added", after: finalInventory["added.txt"] },
    {
      path: "changed.txt",
      change: "modified",
      before: { kind: "file", bytes: 3, mode: 0o644, sha256: "before" },
      after: finalInventory["changed.txt"],
    },
    {
      path: "deleted.txt",
      change: "deleted",
      before: { kind: "file", bytes: 7, mode: 0o644, sha256: "deleted" },
    },
  ]);
  assert.equal(baseline["added.txt"], undefined);
  assert.equal(baseline["changed.txt"].sha256, "before");
  assert.equal(baseline["deleted.txt"].sha256, "deleted");
});

test("sanitizes exact credential values from generated text files", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-secret-scan-test-"));
  await writeFile(path.join(directory, "agent-output.txt"), "credential=actual-secret-value\n");
  const report = await sanitizeExactSecrets(directory, ["actual-secret-value"]);
  assert.deepEqual(report.sanitized_text_files, ["agent-output.txt"]);
  assert.equal(report.unsafe_binary_files.length, 0);
  assert.equal(await readFile(path.join(directory, "agent-output.txt"), "utf8"), "credential=[REDACTED]\n");
});

test("credential sanitization preserves code expressions and aborts writes on unsafe binaries", async () => {
  const source = [
    "const apiKey = process.env.CODEX_MIRROR_API_KEY;",
    "hashed_password = bcrypt.generate_password_hash(password).decode",
    "const sentry_key = process.env.SENTRY_KEY;",
  ].join("\n");
  assert.equal(redactCredentialText(source), source);

  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-atomic-secret-test-"));
  const textPath = path.join(directory, "output.txt");
  const secret = "sk-test-AtomicSecret123456789";
  await writeFile(textPath, `token=${secret}\n`);
  await writeFile(path.join(directory, "payload.bin"), Buffer.concat([Buffer.from([0]), Buffer.from(secret)]));
  const report = await sanitizeExactSecrets(directory, [secret]);
  assert.equal(report.writes_applied, false);
  assert.deepEqual(report.sanitized_text_files, []);
  assert.deepEqual(report.would_sanitize_text_files, ["output.txt"]);
  assert.deepEqual(report.unsafe_binary_files, ["payload.bin"]);
  assert.equal(await readFile(textPath, "utf8"), `token=${secret}\n`);
});

test("detects credentials encoded as UTF-16LE", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-utf16-secret-test-"));
  const secret = "sk-test-Utf16Secret123456789";
  await writeFile(path.join(directory, "payload.bin"), Buffer.from(`Authorization: Bearer ${secret}`, "utf16le"));
  const report = await sanitizeExactSecrets(directory, [secret]);
  assert.deepEqual(report.unsafe_binary_files, ["payload.bin"]);
  assert.equal(report.writes_applied, false);
});

test("sanitizes Sentry credentials without an exact-secret allowlist", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-sentry-scan-test-"));
  const sentryKey = "fedcba9876543210fedcba9876543210";
  const filePath = path.join(directory, "events.jsonl");
  await writeFile(filePath, `url=https://sentry.example/api/store?sentry_key=${sentryKey}\n`);
  const report = await sanitizeExactSecrets(directory, []);
  assert.deepEqual(report.sanitized_text_files, ["events.jsonl"]);
  assert.equal(report.unsafe_binary_files.length, 0);
  const sanitized = await readFile(filePath, "utf8");
  assert.doesNotMatch(sanitized, new RegExp(sentryKey));
  assert.match(sanitized, /sentry_key=\[REDACTED_SENTRY_[0-9a-f]{12}\]/);
});

test("propagates discovered Segment keys to bare occurrences in other trace files", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-segment-scan-test-"));
  const production = "Ab3Def5Gh7Jk9Mn2Pq4Rs6Tu8Vw1Xy3Z";
  const staging = "Zy9Xw7Vu5Ts3Rq1Pn8Mk6Jh4Gf2Ed0Cb";
  await writeFile(
    path.join(directory, "source.js"),
    `SEGMENT_WRITE_KEY",0,"production"===n?"${production}":"${staging}"\n`,
  );
  await writeFile(path.join(directory, "tool-calls.jsonl"), `print(b'${production}')\n`);
  const report = await sanitizeExactSecrets(directory, []);
  assert.deepEqual(report.sanitized_text_files.sort(), ["source.js", "tool-calls.jsonl"]);
  assert.equal(report.unsafe_binary_files.length, 0);
  assert.doesNotMatch(await readFile(path.join(directory, "source.js"), "utf8"), new RegExp(production));
  assert.doesNotMatch(await readFile(path.join(directory, "source.js"), "utf8"), new RegExp(staging));
  assert.doesNotMatch(await readFile(path.join(directory, "tool-calls.jsonl"), "utf8"), new RegExp(production));
});

test("filesystem credential sanitization preserves authentic data images", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-data-image-test-"));
  const filePath = path.join(directory, "page.html");
  const source = '<img src="data:image/png;base64,aGVsbG8="><script>document.cookie="NEXT_LOCALE=en"</script>\n';
  await writeFile(filePath, source);
  const report = await sanitizeExactSecrets(directory, []);
  assert.deepEqual(report.sanitized_text_files, []);
  assert.equal(await readFile(filePath, "utf8"), source);
});

test("rejects Sentry credentials embedded in binary files", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-binary-sentry-test-"));
  const sentryKey = "00112233445566778899aabbccddeeff";
  await writeFile(
    path.join(directory, "payload.bin"),
    Buffer.concat([Buffer.from([0]), Buffer.from(`sentry_key=${sentryKey}`)]),
  );
  const report = await sanitizeExactSecrets(directory, []);
  assert.deepEqual(report.sanitized_text_files, []);
  assert.deepEqual(report.unsafe_binary_files, ["payload.bin"]);
});

test("audits credentials inside nested tar.gz members", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-archive-scan-test-"));
  const innerRoot = path.join(directory, "inner-root");
  const outerRoot = path.join(directory, "outer-root");
  await mkdir(innerRoot);
  await mkdir(outerRoot);
  const sentryKey = "ffeeddccbbaa99887766554433221100";
  await writeFile(path.join(innerRoot, "script.js"), `url=/store?sentry_key=${sentryKey}\n`);
  const innerArchive = path.join(outerRoot, "site.tar.gz");
  await execFileAsync("tar", ["-czf", innerArchive, "-C", innerRoot, "."]);
  const outerArchive = path.join(directory, "mirror-artifact.tar.gz");
  await execFileAsync("tar", ["-czf", outerArchive, "-C", outerRoot, "."]);
  const report = await auditTarArchiveSecrets(outerArchive, []);
  assert.equal(report.status, "failed");
  assert.equal(report.archives_scanned.length, 2);
  assert.match(report.sensitive_text_members.join("\n"), /site\.tar\.gz.*script\.js/);
});

test("discovers renamed nested tar.gz payloads by content", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-renamed-archive-test-"));
  const innerRoot = path.join(directory, "inner-root");
  const outerRoot = path.join(directory, "outer-root");
  await mkdir(innerRoot);
  await mkdir(outerRoot);
  const secret = "sk-test-RenamedArchive123456789";
  await writeFile(path.join(innerRoot, "secret.txt"), `Authorization: Bearer ${secret}\n`);
  await execFileAsync("tar", ["-czf", path.join(outerRoot, "payload.blob"), "-C", innerRoot, "."]);
  const outerArchive = path.join(directory, "mirror-artifact.tar.gz");
  await execFileAsync("tar", ["-czf", outerArchive, "-C", outerRoot, "."]);
  const report = await auditTarArchiveSecrets(outerArchive, [secret]);
  assert.equal(report.status, "failed");
  assert.equal(report.archives_scanned.length, 2);
  assert.match(report.sensitive_text_members.join("\n"), /payload\.blob.*secret\.txt/);
});

test("rejects archive symlinks whose target escapes the extraction root", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-archive-symlink-test-"));
  const root = path.join(directory, "root");
  await mkdir(root);
  await symlink("/etc/shadow", path.join(root, "outside"));
  const archive = path.join(directory, "mirror-artifact.tar.gz");
  await execFileAsync("tar", ["-czf", archive, "-C", root, "."]);
  const report = await auditTarArchiveSecrets(archive, []);
  assert.equal(report.status, "failed");
  assert.match(report.unsafe_archive_members.join("\n"), /unsafe symlink target \/etc\/shadow/);
});

test("binds the mirror artifact to changed-files and current workspace bytes", async () => {
  const runDirectory = await mkdtemp(path.join(os.tmpdir(), "codex-artifact-binding-test-"));
  const workspace = path.join(runDirectory, "workspace");
  await mkdir(path.join(workspace, "sites", "demo"), { recursive: true });
  await writeFile(path.join(workspace, "sites", "demo", "app.py"), "print('demo')\n");
  const inventory = await inventoryTree(workspace);
  const changes = [{ path: "sites/demo/app.py", change: "added", after: inventory["sites/demo/app.py"] }];
  await writeFile(path.join(runDirectory, "changed-files.json"), `${JSON.stringify(changes)}\n`);
  const archivePath = path.join(runDirectory, "mirror-artifact.tar.gz");
  await createDeterministicTarGzip(workspace, archivePath, ["sites/demo/app.py"]);
  const archiveBytes = await readFile(archivePath);
  const correctSha = (await import("node:crypto")).createHash("sha256").update(archiveBytes).digest("hex");
  const run = {
    changed_files: 1,
    mirror_artifact: {
      path: "mirror-artifact.tar.gz",
      bytes: archiveBytes.length,
      sha256: "0".repeat(64),
      files: 1,
      excluded_transient_paths: [],
    },
  };
  const failed = await verifyTraceArtifactBinding(runDirectory, run);
  assert.equal(failed.status, "failed");
  assert.match(failed.failures.join("\n"), /byte identity/);
  run.mirror_artifact.sha256 = correctSha;
  const passed = await verifyTraceArtifactBinding(runDirectory, run);
  assert.equal(passed.status, "passed");
});

test("seal writes incomplete when the top-level binary credential scan fails", async () => {
  const runDirectory = await mkdtemp(path.join(os.tmpdir(), "codex-seal-binary-test-"));
  const workspace = path.join(runDirectory, "workspace");
  await mkdir(path.join(workspace, "sites", "demo"), { recursive: true });
  await writeFile(path.join(workspace, "sites", "demo", "app.py"), "print('demo')\n");
  const inventory = await inventoryTree(workspace);
  const changes = [{ path: "sites/demo/app.py", change: "added", after: inventory["sites/demo/app.py"] }];
  await writeFile(path.join(runDirectory, "changed-files.json"), `${JSON.stringify(changes)}\n`);
  const archivePath = path.join(runDirectory, "mirror-artifact.tar.gz");
  await createDeterministicTarGzip(workspace, archivePath, ["sites/demo/app.py"]);
  const archiveBytes = await readFile(archivePath);
  const archiveSha = (await import("node:crypto")).createHash("sha256").update(archiveBytes).digest("hex");
  await writeFile(path.join(runDirectory, "run.json"), `${JSON.stringify({
    changed_files: 1,
    mirror_artifact: {
      path: "mirror-artifact.tar.gz",
      bytes: archiveBytes.length,
      sha256: archiveSha,
      files: 1,
      excluded_transient_paths: [],
    },
  })}\n`);
  const recorder = new EventRecorder(runDirectory);
  await recorder.initialize();
  await recorder.recordPrompt("mirror", "build");
  await recorder.recordEvent("mirror", { type: "turn.started" });
  await recorder.recordEvent("mirror", { type: "turn.completed", usage: {} });
  await recorder.close();
  const secret = "sk-test-TopLevelBinary123456789";
  await writeFile(path.join(runDirectory, "payload.bin"), Buffer.concat([Buffer.from([0]), Buffer.from(secret)]));
  await assert.rejects(execFileAsync("node", [path.join(toolDirectory, "seal.mjs"), runDirectory], {
    cwd: toolDirectory,
    env: { ...process.env, CODEX_MIRROR_API_KEY: secret },
  }));
  const run = JSON.parse(await readFile(path.join(runDirectory, "run.json"), "utf8"));
  assert.equal(run.trace_integrity_status, "incomplete");
  assert.equal(run.trace_secret_scan.status, "failed");
});

test("builds byte-identical archives across source mtime changes", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "codex-deterministic-archive-test-"));
  const workspace = path.join(directory, "workspace");
  await mkdir(path.join(workspace, "sites", "demo"), { recursive: true });
  const appPath = path.join(workspace, "sites", "demo", "app.py");
  await writeFile(appPath, "print('demo')\n");
  await writeFile(path.join(workspace, "site_catalog.json"), "{}\n");
  const archive = path.join(directory, "mirror-artifact.tar.gz");
  const members = ["sites/demo/app.py", "site_catalog.json"];
  await createDeterministicTarGzip(workspace, archive, members);
  const first = await readFile(archive);
  await utimes(appPath, new Date("2035-01-01T00:00:00Z"), new Date("2035-01-01T00:00:00Z"));
  await createDeterministicTarGzip(workspace, archive, [...members].reverse());
  const second = await readFile(archive);
  assert.deepEqual(second, first);
  const listing = await execFileAsync("tar", ["-tzf", archive]);
  assert.deepEqual(listing.stdout.trim().split("\n"), ["site_catalog.json", "sites/demo/app.py"]);
});

test("excludes runtime and Python cache files from mergeable mirror archives", () => {
  assert.equal(isTransientMirrorArtifactPath("sites/demo/instance/demo.db"), true);
  assert.equal(isTransientMirrorArtifactPath("sites/demo/__pycache__/app.cpython-312.pyc"), true);
  assert.equal(isTransientMirrorArtifactPath("sites/demo/instance_seed/demo.db"), false);
  assert.equal(isTransientMirrorArtifactPath("sites/demo/app.py"), false);
});

test("target-scoped archives exclude auxiliary sites, packages, and local acquisition caches", () => {
  const changes = [
    { path: "sites/demo/app.py", change: "added" },
    { path: "sites/demo/instance/demo.db", change: "added" },
    { path: "sites/demo_capture/static/raw.html", change: "added" },
    { path: "sites/unrelated/app.py", change: "added" },
    { path: ".assets/generated/demo.tar.gz", change: "added" },
    { path: ".assets/generated/demo_capture.tar.gz", change: "added" },
    { path: ".assets/parquet-origin-scan/source-footer.bin", change: "added" },
    { path: ".assets/playwright/chromium/chrome", change: "added" },
    { path: "site_catalog.json", change: "modified" },
    { path: "skill-proposal/build-webharbor-mirrors/SKILL.md", change: "added" },
  ];
  const selected = selectMirrorArtifactPaths(changes, "demo");
  assert.deepEqual(selected.artifact_scope, { mode: "target_site", site: "demo" });
  assert.deepEqual(selected.paths, [
    ".assets/generated/demo.tar.gz",
    "site_catalog.json",
    "sites/demo/app.py",
  ]);
  assert.deepEqual(selected.excluded_transient_paths, ["sites/demo/instance/demo.db"]);
  assert.deepEqual(selected.excluded_out_of_scope_paths, [
    ".assets/generated/demo_capture.tar.gz",
    ".assets/parquet-origin-scan/source-footer.bin",
    ".assets/playwright/chromium/chrome",
    "sites/demo_capture/static/raw.html",
    "sites/unrelated/app.py",
  ]);
});

test("artifact binding proves target scope and excluded auxiliary site bytes", async () => {
  const runDirectory = await mkdtemp(path.join(os.tmpdir(), "codex-target-binding-test-"));
  const workspace = path.join(runDirectory, "workspace");
  await mkdir(path.join(workspace, "sites", "demo"), { recursive: true });
  await mkdir(path.join(workspace, "sites", "demo_capture"), { recursive: true });
  await writeFile(path.join(workspace, "sites", "demo", "app.py"), "print('demo')\n");
  await writeFile(path.join(workspace, "sites", "demo_capture", "raw.html"), "source bytes\n");
  const inventory = await inventoryTree(workspace);
  const changes = [
    { path: "sites/demo/app.py", change: "added", after: inventory["sites/demo/app.py"] },
    { path: "sites/demo_capture/raw.html", change: "added", after: inventory["sites/demo_capture/raw.html"] },
  ];
  await writeFile(path.join(runDirectory, "changed-files.json"), `${JSON.stringify(changes)}\n`);
  const archivePath = path.join(runDirectory, "mirror-artifact.tar.gz");
  await createDeterministicTarGzip(workspace, archivePath, ["sites/demo/app.py"]);
  const archiveBytes = await readFile(archivePath);
  const archiveSha = (await import("node:crypto")).createHash("sha256").update(archiveBytes).digest("hex");
  const run = {
    site: "demo",
    changed_files: 2,
    mirror_artifact: {
      path: "mirror-artifact.tar.gz",
      bytes: archiveBytes.length,
      sha256: archiveSha,
      files: 1,
      artifact_scope: { mode: "target_site", site: "demo" },
      excluded_transient_paths: [],
      excluded_out_of_scope_paths: ["sites/demo_capture/raw.html"],
    },
  };
  const passed = await verifyTraceArtifactBinding(runDirectory, run);
  assert.equal(passed.status, "passed");
  run.mirror_artifact.excluded_out_of_scope_paths = [];
  const failed = await verifyTraceArtifactBinding(runDirectory, run);
  assert.equal(failed.status, "failed");
  assert.match(failed.failures.join("\n"), /excluded_out_of_scope_paths/);
});
