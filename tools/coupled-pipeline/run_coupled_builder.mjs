#!/usr/bin/env node

import { createWriteStream } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

import { Codex } from "../codex-mirror-trace/node_modules/@openai/codex-sdk/dist/index.js";
import { checkpointIdentity, openCheckpoint } from "./sdk_checkpoint.mjs";
import { buildBuilderPrompt } from "./prompt_contracts.mjs";
import { runCheckpointedTurn } from "./run_checkpointed_turn.mjs";

function parseArgs(argv) {
  const args = {
    workspace: "",
    output: "",
    requirements: "",
    sourceUrl: "",
    siteId: "",
    storageState: "",
    model: "gpt-5.6-sol",
    reasoningEffort: "xhigh",
    timeoutMinutes: 90,
    coverageProfile: "standard",
    resume: false,
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
    else if (value === "--requirements") args.requirements = path.resolve(next());
    else if (value === "--source-url") args.sourceUrl = next();
    else if (value === "--site-id") args.siteId = next();
    else if (value === "--storage-state") args.storageState = path.resolve(next());
    else if (value === "--model") args.model = next();
    else if (value === "--reasoning-effort") args.reasoningEffort = next();
    else if (value === "--timeout-minutes") args.timeoutMinutes = Number(next());
    else if (value === "--coverage-profile") args.coverageProfile = next();
    else if (value === "--resume") args.resume = true;
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

async function main() {
  const args = parseArgs(process.argv.slice(2));
  for (const key of ["workspace", "output", "requirements", "sourceUrl", "siteId", "storageState"]) {
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
  await mkdir(args.output, { recursive: true });
  const reportDirectory = path.join(args.workspace, path.basename(args.output));
  await mkdir(reportDirectory, { recursive: true });
  await mkdir(path.join(args.workspace, "sites"), { recursive: true });
  await mkdir(path.join(args.workspace, "evidence"), { recursive: true });
  const requirements = await readFile(args.requirements, "utf8");
  const checkpoint = await openCheckpoint(args.workspace, "builder", checkpointIdentity({
    role: "builder", workspace: args.workspace, source_url: args.sourceUrl,
    site_id: args.siteId, requirements, model: args.model,
    coverage_profile: args.coverageProfile,
    reasoning_effort: args.reasoningEffort, provider_url: baseUrl,
  }));
  const eventsPath = path.join(args.output, "sdk-events.jsonl");
  const events = createWriteStream(eventsPath, { flags: "a", mode: 0o600 });
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
      workingDirectory: args.workspace,
    };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), args.timeoutMinutes * 60_000);
    try {
      const result = await runCheckpointedTurn({
        codex, checkpoint, threadOptions,
        prompt: buildBuilderPrompt(args, requirements, checkpoint),
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
  await writeFile(path.join(args.output, "final.md"), `${finalResponse}\n`, { mode: 0o600 });
  for (const name of ["build-result.json", "build-notes.md", "task-contract.json", "coverage-contract.json"]) {
    try {
      const body = await readFile(path.join(reportDirectory, name), "utf8");
      if (name.endsWith(".json") && JSON.parse(body).site_id !== args.siteId) continue;
      await writeFile(path.join(args.output, name), body, { mode: 0o600 });
    } catch (error) {
      if (error.code !== "ENOENT") errorText += `\nReport handoff: ${error.message}`;
    }
  }
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
  }, null, 2) + "\n", { mode: 0o600 });
  if (turnStatus !== "completed") process.exitCode = 1;
}

await main();
