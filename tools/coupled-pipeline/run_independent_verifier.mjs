#!/usr/bin/env node

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { createWriteStream, existsSync } from "node:fs";
import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { treeHash, changedFiles } from "./candidate_integrity.mjs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

import { Codex } from "../codex-mirror-trace/node_modules/@openai/codex-sdk/dist/index.js";
import { checkpointIdentity, openCheckpoint } from "./sdk_checkpoint.mjs";
import { buildVerifierPrompt } from "./prompt_contracts.mjs";
import { runCheckpointedTurn } from "./run_checkpointed_turn.mjs";

function parseArgs(argv) {
  const args = {
    workspace: "",
    output: "",
    builderOutput: "",
    sourceUrl: "",
    siteId: "",
    storageState: "",
    taskRequest: "",
    model: "gpt-5.6-sol",
    reasoningEffort: "xhigh",
    timeoutMinutes: 90,
    coverageProfile: "standard",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    const next = () => {
      index += 1;
      if (index >= argv.length) throw new Error(`${value} requires a value`);
      return argv[index];
    };
    if (value === "--workspace") args.workspace = path.resolve(next());
    else if (value === "--output") args.output = path.resolve(next());
    else if (value === "--builder-output") args.builderOutput = path.resolve(next());
    else if (value === "--source-url") args.sourceUrl = next();
    else if (value === "--site-id") args.siteId = next();
    else if (value === "--storage-state") args.storageState = path.resolve(next());
    else if (value === "--task-request") args.taskRequest = path.resolve(next());
    else if (value === "--model") args.model = next();
    else if (value === "--reasoning-effort") args.reasoningEffort = next();
    else if (value === "--timeout-minutes") args.timeoutMinutes = Number(next());
    else if (value === "--coverage-profile") args.coverageProfile = next();
    else throw new Error(`unknown option: ${value}`);
  }
  return args;
}

function safeEnvironment(codexHome) {
  const allowed = [
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "PYTHONPATH", "PLAYWRIGHT_BROWSERS_PATH", "WEBHARBOR_CHROMIUM_EXECUTABLE",
  ];
  const env = Object.fromEntries(
    allowed.flatMap((name) => process.env[name] ? [[name, process.env[name]]] : []),
  );
  env.CODEX_HOME = codexHome;
  env.PYTHONDONTWRITEBYTECODE = "1";
  return env;
}

async function taskContractPreflight(args) {
  if (!args.taskRequest) return null;
  const request = JSON.parse(await readFile(args.taskRequest, "utf8"));
  const contract = JSON.parse(await readFile(path.join(args.builderOutput, "task-contract.json"), "utf8"));
  const problems = [];
  if (contract.site_id !== args.siteId || contract.task_query_sha256 !== request.query_sha256) {
    problems.push("site ID or task query hash differs from the frozen request");
  }
  if (!Array.isArray(contract.steps) || contract.steps.length === 0) {
    problems.push("task contract has no ordered steps");
  } else {
    const ids = new Set();
    for (const [index, step] of contract.steps.entries()) {
      if (!step || ["id", "source_route", "local_route", "action", "target", "expected_state"]
        .some((key) => typeof step[key] !== "string" || !step[key].trim())) {
        problems.push(`step ${index + 1} is missing a required field`);
      } else if (ids.has(step.id)) {
        problems.push(`duplicate step ID: ${step.id}`);
      } else ids.add(step.id);
    }
  }
  if (typeof contract.terminal_expected_state !== "string" || !contract.terminal_expected_state.trim()) {
    problems.push("terminal expected state is missing");
  }
  return { status: problems.length ? "failed" : "passed", problems,
    step_count: contract.steps?.length ?? 0 };
}

async function coverageContractPreflight(args) {
  if (args.coverageProfile !== "fast") return null;
  const contract = JSON.parse(await readFile(
    path.join(args.builderOutput, "coverage-contract.json"), "utf8"));
  const problems = [];
  if (contract.schema_version !== 1 || contract.site_id !== args.siteId || contract.profile !== "fast") {
    problems.push("schema, site ID, or profile differs from the frozen Fast Full run");
  }
  for (const field of ["route_families", "page_templates", "interaction_types", "representative_routes"]) {
    if (!Array.isArray(contract[field]) || contract[field].length === 0) {
      problems.push(`${field} must be a non-empty array`);
    }
  }
  if (!Array.isArray(contract.exclusion_rules)) problems.push("exclusion_rules must be an array");
  if (!contract.exploration_budget || typeof contract.exploration_budget !== "object") {
    problems.push("exploration_budget is missing");
  }
  if (!contract.closure || typeof contract.closure !== "object") problems.push("closure is missing");
  return { status: problems.length ? "failed" : "passed", problems,
    family_count: contract.route_families?.length ?? 0,
    template_count: contract.page_templates?.length ?? 0,
    interaction_count: contract.interaction_types?.length ?? 0 };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  for (const key of ["workspace", "output", "builderOutput", "sourceUrl", "siteId", "storageState"]) {
    if (!args[key]) throw new Error(`missing required argument: ${key}`);
  }
  if (!["standard", "fast"].includes(args.coverageProfile)) {
    throw new Error("coverage profile must be standard or fast");
  }
  const apiKey = process.env.WEBANYTHING_RESPONSES_API_KEY ||
    process.env.CODEX_REFERENCE_AUDIT_API_KEY;
  const baseUrl = process.env.WEBANYTHING_RESPONSES_BASE_URL ||
    process.env.CODEX_REFERENCE_AUDIT_BASE_URL;
  if (!apiKey || !baseUrl) {
    throw new Error("WEBANYTHING_RESPONSES_API_KEY and WEBANYTHING_RESPONSES_BASE_URL are required");
  }
  const candidateRoot = path.join(args.workspace, "sites", args.siteId);
  if (!(await stat(candidateRoot)).isDirectory()) throw new Error(`candidate missing: ${candidateRoot}`);
  await mkdir(args.output, { recursive: true });
  if (args.taskRequest) {
    let contractCheck;
    try {
      contractCheck = await taskContractPreflight(args);
    } catch (error) {
      contractCheck = { status: "failed", problems: [error.message] };
    }
    await writeFile(path.join(args.output, "task-contract-preflight.json"),
      JSON.stringify(contractCheck, null, 2) + "\n", { mode: 0o600 });
    if (contractCheck.status !== "passed") {
      const failure = { gate: "task_contract", expected: "Complete ordered source-derived task steps",
        observed: contractCheck.problems.join("; ") };
      await writeFile(path.join(args.output, "repair-package.json"),
        JSON.stringify({ schema_version: 1, site_id: args.siteId, failures: [failure] }, null, 2));
      await writeFile(path.join(args.output, "verifier-result.json"),
        JSON.stringify({ schema_version: 1, site_id: args.siteId, status: "failed",
          gates: { task_contract: { status: "failed" } } }, null, 2));
      await writeFile(path.join(args.output, "sdk-run.json"),
        JSON.stringify({ schema_version: 1, site_id: args.siteId,
          turn_status: "preflight_failed", candidate_unchanged: null }, null, 2));
      process.exitCode = 2;
      return;
    }
  }
  if (args.coverageProfile === "fast") {
    let coverageCheck;
    try {
      coverageCheck = await coverageContractPreflight(args);
    } catch (error) {
      coverageCheck = { status: "failed", problems: [error.message] };
    }
    await writeFile(path.join(args.output, "coverage-contract-preflight.json"),
      JSON.stringify(coverageCheck, null, 2) + "\n", { mode: 0o600 });
    if (coverageCheck.status !== "passed") {
      const failure = { gate: "coverage_contract",
        expected: "Complete frozen Fast Full family, template, interaction, and representative inventory",
        observed: coverageCheck.problems.join("; ") };
      await writeFile(path.join(args.output, "repair-package.json"),
        JSON.stringify({ schema_version: 1, site_id: args.siteId, failures: [failure] }, null, 2));
      await writeFile(path.join(args.output, "verifier-result.json"),
        JSON.stringify({ schema_version: 1, site_id: args.siteId, status: "failed",
          gates: { coverage_contract: { status: "failed" } } }, null, 2));
      await writeFile(path.join(args.output, "sdk-run.json"),
        JSON.stringify({ schema_version: 1, site_id: args.siteId,
          turn_status: "preflight_failed", candidate_unchanged: null }, null, 2));
      process.exitCode = 2;
      return;
    }
  }
  const repoPython = fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));
  const preflightPython = process.env.WEBANYTHING_PYTHON ||
    (existsSync(repoPython) ? repoPython : "python3");
  try {
    await promisify(execFile)(preflightPython, ["-B",
      fileURLToPath(new URL("./preflight_reset.py", import.meta.url)), candidateRoot,
      path.join(args.output, "preflight-reset.json")], {
      timeout: 60_000, maxBuffer: 1024 * 1024, env: safeEnvironment(os.tmpdir()),
    });
  } catch (error) {
    const diagnostic = await readFile(path.join(args.output, "preflight-reset.json"), "utf8").catch(() => "preflight reset timed out or failed to start");
    const failure = { gate: "preflight_reset", expected: "Local API double reset, immutable seed, no code changes", observed: diagnostic };
    await writeFile(path.join(args.output, "repair-package.json"), JSON.stringify({ schema_version: 1, site_id: args.siteId, failures: [failure] }, null, 2));
    await writeFile(path.join(args.output, "verifier-result.json"), JSON.stringify({ schema_version: 1, site_id: args.siteId, status: "failed", gates: { preflight_reset: { status: "failed" } } }, null, 2));
    await writeFile(path.join(args.output, "sdk-run.json"), JSON.stringify({ schema_version: 1, site_id: args.siteId, turn_status: "preflight_failed", error: diagnostic, candidate_unchanged: null }, null, 2));
    process.exitCode = 2;
    return;
  }
  const before = await treeHash(candidateRoot);
  const taskRequest = args.taskRequest ? JSON.parse(await readFile(args.taskRequest, "utf8")) : null;
  const builderReport = await readFile(path.join(args.builderOutput, "build-result.json"), "utf8");
  const taskContract = taskRequest
    ? await readFile(path.join(args.builderOutput, "task-contract.json"), "utf8") : "";
  const coverageContract = args.coverageProfile === "fast"
    ? await readFile(path.join(args.builderOutput, "coverage-contract.json"), "utf8") : "";
  const checkpoint = await openCheckpoint(args.workspace, "verifier", checkpointIdentity({
    role: "verifier", workspace: args.workspace, source_url: args.sourceUrl,
    site_id: args.siteId, model: args.model, reasoning_effort: args.reasoningEffort,
    coverage_profile: args.coverageProfile,
    task_request: taskRequest, candidate_sha256: before.sha256,
    builder_output: args.builderOutput, provider_url: baseUrl,
    builder_report_sha256: createHash("sha256").update(builderReport).digest("hex"),
    task_contract_sha256: createHash("sha256").update(taskContract).digest("hex"),
    coverage_contract_sha256: createHash("sha256").update(coverageContract).digest("hex"),
  }));
  await writeFile(path.join(args.output, "candidate-freeze.json"), JSON.stringify({
    schema_version: 1,
    site_id: args.siteId,
    candidate_root: candidateRoot,
    ...before,
  }, null, 2) + "\n", { mode: 0o600 });
  // A timed-out turn may already have written a pass report. A resumed turn
  // must explicitly write a new result before the controller can accept it.
  await rename(path.join(args.output, "verifier-result.json"),
    path.join(args.output, "previous-verifier-result.json")).catch((error) => {
    if (error.code !== "ENOENT") throw error;
  });

  const events = createWriteStream(path.join(args.output, "sdk-events.jsonl"), { flags: "a", mode: 0o600 });
  let turnStatus = "failed";
  let finalResponse = "";
  let errorText = "";
  let resumeStrategy = checkpoint.threadId ? "thread" : "fresh";
  let resumeFallbackError = "";
  try {
    const env = safeEnvironment(checkpoint.codexHome);
    env.WEBANYTHING_RESPONSES_API_KEY = apiKey;
    const codex = new Codex({
      env,
      config: {
        model_provider: "webanything_responses",
        model_providers: { webanything_responses: {
          name: "WebAnything Responses", base_url: baseUrl,
          env_key: "WEBANYTHING_RESPONSES_API_KEY", wire_api: "responses",
          requires_openai_auth: false, supports_websockets: false,
        } },
        model_reasoning_summary: "detailed",
        show_raw_agent_reasoning: false,
        sandbox_workspace_write: { network_access: true },
      },
    });
    const threadOptions = {
      model: args.model,
      modelReasoningEffort: args.reasoningEffort,
      sandboxMode: "workspace-write",
      approvalPolicy: "never",
      networkAccessEnabled: true,
      webSearchMode: "disabled",
      workingDirectory: path.dirname(args.workspace),
    };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), args.timeoutMinutes * 60_000);
    try {
      const result = await runCheckpointedTurn({
        codex, checkpoint, threadOptions,
        prompt: buildVerifierPrompt(args, candidateRoot, before, taskRequest, checkpoint.output),
        output: args.output,
        signal: controller.signal,
        onEvent: async (event) => {
        events.write(JSON.stringify(event) + "\n");
        if (event.type === "item.completed" && event.item?.type === "agent_message") {
          finalResponse = event.item.text ?? "";
        }
        if (event.type === "turn.completed") turnStatus = "completed";
        if (event.type === "turn.failed" || event.type === "error") {
          turnStatus = "failed";
          errorText = event.message ?? event.error?.message ?? "";
        }
        },
      });
      resumeStrategy = result.strategy;
      resumeFallbackError = result.fallbackError;
    } finally {
      clearTimeout(timer);
    }
  } catch (error) {
    turnStatus = error?.name === "AbortError" ? "timed_out" : "failed";
    errorText = error?.stack || error?.message || String(error);
    resumeStrategy = error?.resumeStrategy || resumeStrategy;
    resumeFallbackError = error?.resumeFallbackError || resumeFallbackError;
  } finally {
    await new Promise((resolve) => events.end(resolve));
  }

  const after = await treeHash(candidateRoot);
  const candidateUnchanged = before.sha256 === after.sha256 && before.fileCount === after.fileCount;
  await writeFile(path.join(args.output, "candidate-hash-after.json"), JSON.stringify(after, null, 2));
  await writeFile(path.join(args.output, "candidate-diff.json"), JSON.stringify({ changed_files: changedFiles(before, after) }, null, 2));
  await writeFile(path.join(args.output, "final.md"), `${finalResponse}\n`, { mode: 0o600 });
  await writeFile(path.join(args.output, "sdk-run.json"), JSON.stringify({
    schema_version: 1,
    site_id: args.siteId,
    source_url: args.sourceUrl,
    model: args.model,
    reasoning_effort: args.reasoningEffort,
    turn_status: turnStatus,
    resumed_thread: resumeStrategy === "thread",
    resume_strategy: resumeStrategy,
    resume_fallback_error: resumeFallbackError,
    error: errorText,
    candidate_hash_before: before,
    candidate_hash_after: after,
    candidate_unchanged: candidateUnchanged,
  }, null, 2) + "\n", { mode: 0o600 });
  if (turnStatus !== "completed" || !candidateUnchanged) process.exitCode = 1;
}

await main();
