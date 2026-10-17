#!/usr/bin/env node

import { Codex } from "@openai/codex-sdk";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import {
  EventRecorder,
  auditEventLifecycle,
  auditTarArchiveSecrets,
  buildRepairPrompt,
  buildRepairReflectionPrompt,
  buildSyntheticReflectionPrompt,
  buildSyntheticRepairPrompt,
  buildSyntheticReplayReflectionPrompt,
  buildSyntheticReplayRepairPrompt,
  createDeterministicTarGzip,
  describeFile,
  diffInventories,
  hashTree,
  inventoryTree,
  isBuilderRepairPackage,
  parseAgentProfile,
  publicBaseUrl,
  reconstructBaselineInventory,
  redactText,
  selectMirrorArtifactPaths,
  sanitizeExactSecrets,
  writePrivateJson,
} from "./lib.mjs";

const SDK_VERSION = "0.146.1";
function usage() {
  return `usage: continue.mjs RUN_DIR --requirements-file FILE [options]\n\nOptions:\n  --site SITE                  Target mirror site id\n  --phase NAME                 Trace phase name (default: repair-N)\n  --model MODEL                Override the prior trace model\n  --agent-config FILE          Optional model/effort/summary YAML\n  --reasoning-effort LEVEL     minimal|low|medium|high|xhigh\n  --timeout-minutes N          Repair timeout (default: 120)\n  --reflection-timeout-minutes N  Reflection timeout (default: 20)\n  --skip-artifact              Skip the outer mirror-artifact.tar.gz package\n  --no-reflection              Skip post-repair proposal reflection\n\nRequirements files may be free-form text up to 64 KiB, or a schema-valid\nwebharbor-builder-repair-package-v1 JSON document up to 1 MiB.\n`;
}

function parseArgs(argv) {
  const args = {
    agentConfig: process.env.WEBANYTHING_AGENT_CONFIG || null,
    timeoutMinutes: 120,
    reflectionTimeoutMinutes: 20,
    reflection: true,
    skipArtifact: false,
    resetTrace: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    const next = () => {
      index += 1;
      if (index >= argv.length) throw new Error(`${value} requires a value`);
      return argv[index];
    };
    if (value === "--requirements-file") args.requirementsFile = next();
    else if (value === "--site") args.site = next();
    else if (value === "--phase") args.phase = next();
    else if (value === "--model") args.model = next();
    else if (value === "--agent-config") args.agentConfig = next();
    else if (value === "--reasoning-effort") args.reasoningEffort = next();
    else if (value === "--timeout-minutes") args.timeoutMinutes = Number(next());
    else if (value === "--reflection-timeout-minutes") args.reflectionTimeoutMinutes = Number(next());
    else if (value === "--skip-artifact") args.skipArtifact = true;
    else if (value === "--no-reflection") args.reflection = false;
    else if (value === "--reset-trace") args.resetTrace = true;
    else if (value === "--help" || value === "-h") args.help = true;
    else if (value.startsWith("-")) throw new Error(`unknown option: ${value}`);
    else if (!args.runDir) args.runDir = value;
    else throw new Error(`unexpected positional argument: ${value}`);
  }
  return args;
}

function safeEnvironment({ codexHome, sourceRoot }) {
  const allowed = [
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
  ];
  const environment = Object.fromEntries(
    allowed.flatMap((name) => process.env[name] ? [[name, process.env[name]]] : []),
  );
  environment.CODEX_HOME = codexHome;
  // Keep all shell startup state in the temporary trace home rather than
  // falling back to the caller's real account home outside the sandbox.
  environment.HOME = codexHome;
  environment.PYTHONDONTWRITEBYTECODE = "1";
  const dependencyPath = path.join(sourceRoot, ".assets", "builder-python");
  const browserPath = path.join(sourceRoot, ".assets", "playwright");
  if (existsSync(path.join(dependencyPath, ".webharbor-builder-ready"))) {
    environment.WEBHARBOR_BUILDER_DEPENDENCY_PATH = dependencyPath;
    environment.WEBHARBOR_RUNTIME_DEPENDENCY_PATH = dependencyPath;
    environment.PYTHONPATH = dependencyPath;
  }
  if (existsSync(browserPath)) environment.PLAYWRIGHT_BROWSERS_PATH = browserPath;
  return environment;
}

function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: options.capture ? ["ignore", "pipe", "pipe"] : "inherit",
    });
    const stdout = [];
    const stderr = [];
    if (child.stdout) child.stdout.on("data", (chunk) => stdout.push(chunk));
    if (child.stderr) child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      if (code === 0 && !signal) resolve(Buffer.concat(stdout).toString("utf8"));
      else reject(new Error(`${command} exited with ${signal ? `signal ${signal}` : `code ${code}`}: ${Buffer.concat(stderr).toString("utf8")}`));
    });
  });
}

function printEvent(phase, event, secrets) {
  if (!["item.completed", "turn.failed", "error"].includes(event.type)) return;
  if (event.type !== "item.completed") {
    console.error(`[${phase}] ${redactText(JSON.stringify(event), secrets)}`);
    return;
  }
  const item = event.item;
  if (item.type === "reasoning") console.log(`[${phase}] reasoning summary: ${redactText(item.text, secrets)}`);
  else if (item.type === "agent_message") console.log(`[${phase}] assistant: ${redactText(item.text, secrets)}`);
  else if (item.type === "command_execution") console.log(`[${phase}] command (${item.status}): ${redactText(item.command, secrets)}`);
  else if (item.type === "file_change") console.log(`[${phase}] file change (${item.status}): ${item.changes.map((entry) => entry.path).join(", ")}`);
  else if (item.type === "mcp_tool_call") console.log(`[${phase}] MCP ${item.server}/${item.tool}: ${item.status}`);
}

async function runTurn(thread, phase, prompt, recorder, timeoutMinutes, secrets) {
  await recorder.recordPrompt(phase, prompt);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMinutes * 60_000);
  let status = "failed";
  let sawTerminal = false;
  try {
    const { events } = await thread.runStreamed(prompt, { signal: controller.signal });
    for await (const event of events) {
      const sanitized = await recorder.recordEvent(phase, event);
      printEvent(phase, sanitized, secrets);
      if (sanitized.type === "turn.completed") {
        status = "completed";
        sawTerminal = true;
      }
      if (sanitized.type === "turn.failed") {
        status = "failed";
        sawTerminal = true;
      }
      if (sanitized.type === "error") status = "failed";
    }
    if (!sawTerminal) {
      throw new Error("SDK_TURN_TERMINAL_MISSING: stream ended without turn.completed or turn.failed");
    }
  } finally {
    clearTimeout(timer);
  }
  return status;
}

async function createArchive(workspace, runDir, changes, environment, secrets, site) {
  const selected = selectMirrorArtifactPaths(changes, site);
  const { paths } = selected;
  if (site && !paths.some((filePath) => filePath.startsWith(`sites/${site}/`))) {
    throw new Error(`target-scoped archive has no files for sites/${site}/`);
  }
  const archivePath = path.join(runDir, "mirror-artifact.tar.gz");
  await createDeterministicTarGzip(workspace, archivePath, paths, environment);
  const secretScan = await auditTarArchiveSecrets(archivePath, secrets);
  if (secretScan.status !== "passed") {
    await rm(archivePath, { force: true });
    throw new Error(`credential material found in shared archive members: ${[
      ...secretScan.sensitive_text_members,
      ...secretScan.unsafe_binary_members,
      ...secretScan.unsafe_archive_members,
    ].join(", ")}`);
  }
  return {
    path: path.basename(archivePath),
    ...await describeFile(archivePath),
    files: paths.length,
    artifact_scope: selected.artifact_scope,
    excluded_transient_paths: selected.excluded_transient_paths,
    excluded_out_of_scope_paths: selected.excluded_out_of_scope_paths,
    secret_scan: secretScan,
  };
}

async function topLevelArtifactFiles(runDir) {
  const names = await readdir(runDir);
  return names.filter((name) => {
    if (["workspace", "artifact-manifest.json"].includes(name)) return false;
    return name.endsWith(".jsonl") || name.endsWith(".json") || name.endsWith(".md") || name === "mirror-artifact.tar.gz";
  }).sort();
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    console.error(usage());
    process.exitCode = 64;
    return;
  }
  if (args.help) {
    console.log(usage());
    return;
  }
  if (!args.runDir || !args.requirementsFile) {
    console.error(usage());
    process.exitCode = 64;
    return;
  }
  if (!Number.isFinite(args.timeoutMinutes) || args.timeoutMinutes <= 0) throw new Error("--timeout-minutes must be positive");
  if (!Number.isFinite(args.reflectionTimeoutMinutes) || args.reflectionTimeoutMinutes <= 0) throw new Error("--reflection-timeout-minutes must be positive");

  const apiKey = process.env.CODEX_MIRROR_API_KEY;
  const baseUrl = process.env.CODEX_MIRROR_BASE_URL;
  if (!apiKey || !baseUrl) throw new Error("provider credentials are missing; use scripts/run_codex_trace.sh continue");
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const runDir = path.resolve(args.runDir);
  const workspace = path.join(runDir, "workspace");
  if (!existsSync(path.join(runDir, "run.json")) || !existsSync(path.join(workspace, ".git"))) {
    throw new Error(`not a completed trace workspace: ${runDir}`);
  }
  const requirements = await readFile(path.resolve(args.requirementsFile), "utf8");
  if (!requirements.trim()) throw new Error("--requirements-file must not be empty");
  let repairPackage = null;
  try {
    const parsed = JSON.parse(requirements);
    if (isBuilderRepairPackage(parsed)) repairPackage = parsed;
  } catch {
    // Free-form repair briefs remain valid inputs and retain the smaller cap.
  }
  const requirementsBytes = Buffer.byteLength(requirements, "utf8");
  const requirementsLimit = repairPackage ? 1_048_576 : 65_536;
  if (requirementsBytes > requirementsLimit) {
    const kind = repairPackage ? "builder repair package" : "free-form requirements file";
    const limit = repairPackage ? "1 MiB" : "64 KiB";
    throw new Error(`${kind} must not exceed ${limit}`);
  }

  const priorRun = JSON.parse(await readFile(path.join(runDir, "run.json"), "utf8"));
  const priorChanges = JSON.parse(await readFile(path.join(runDir, "changed-files.json"), "utf8"));
  const buildMode = priorRun.build_mode || "mirror";
  if (!["mirror", "benchmark_synthetic", "benchmark_synthetic_replay"].includes(buildMode)) {
    throw new Error(`unsupported prior build mode: ${buildMode}`);
  }
  const skillName = buildMode === "benchmark_synthetic_replay"
    ? "build-synthetic-replays"
    : buildMode === "benchmark_synthetic"
      ? "build-synthetic-benchmarks"
      : "build-webharbor-mirrors";
  let acceptanceProfile = priorRun.acceptance_profile || "task-driven";
  if (buildMode === "mirror" && ["task-driven", "immersive", "authenticated"].includes(repairPackage?.to_profile)) {
    acceptanceProfile = repairPackage.to_profile;
  }
  const workspaceSkill = path.join(workspace, ".agents", "skills", skillName);
  await rm(workspaceSkill, { recursive: true, force: true });
  await mkdir(path.dirname(workspaceSkill), { recursive: true });
  await cp(path.join(sourceRoot, ".agents", "skills", skillName), workspaceSkill, { recursive: true, force: false });
  const inventoryExclusions = [".git", "skill-proposal", `.agents/skills/${skillName}`];
  const preRepairInventory = await inventoryTree(workspace, inventoryExclusions);
  const baseline = reconstructBaselineInventory(preRepairInventory, priorChanges);
  const continuations = Array.isArray(priorRun.continuations) ? priorRun.continuations : [];
  const phase = args.phase || `repair-${continuations.length + 1}`;
  if (!/^[a-z0-9][a-z0-9_-]{0,39}$/.test(phase)) throw new Error("--phase must match [a-z0-9][a-z0-9_-]{0,39}");
  if (continuations.some((entry) => entry.phase === phase)) throw new Error(`trace phase already exists: ${phase}`);

  const profileText = args.agentConfig ? await readFile(path.resolve(args.agentConfig), "utf8") : "";
  const profile = parseAgentProfile(profileText);
  const model = args.model || priorRun.model || profile.model;
  const reasoningEffort = args.reasoningEffort || priorRun.reasoning_effort || profile.effort;
  if (!new Set(["minimal", "low", "medium", "high", "xhigh"]).has(reasoningEffort)) {
    throw new Error(`unsupported reasoning effort: ${reasoningEffort}`);
  }

  const codexHome = await mkdtemp(path.join(os.tmpdir(), `webharbor-codex-continuation-${phase}-`));
  const childEnvironment = safeEnvironment({ codexHome, sourceRoot });
  const proxySecrets = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"].flatMap((name) => process.env[name] ? [process.env[name]] : []);
  const secrets = [apiKey, ...proxySecrets];
  let traceResetArchive = null;
  if (args.resetTrace) {
    const suffix = new Date().toISOString().replace(/[:.]/g, "-");
    traceResetArchive = path.join(runDir, "trace-history", `${phase}-${suffix}`);
    await mkdir(traceResetArchive, { recursive: true, mode: 0o700 });
    for (const name of [
      "events.jsonl", "messages.jsonl", "tool-calls.jsonl", "prompts.jsonl",
      "lifecycle-audit.json", "run.json", "artifact-manifest.json", "final.md", "reflection.md",
    ]) {
      const source = path.join(runDir, name);
      if (existsSync(source)) await cp(source, path.join(traceResetArchive, name), { force: false });
    }
  }
  const recorder = new EventRecorder(runDir, secrets, { append: !args.resetTrace });
  await recorder.initialize();
  const previousLifecycle = JSON.parse(await readFile(path.join(runDir, "lifecycle-audit.json"), "utf8"));
  const priorTimedOutPhase = priorRun.latest_execution_status === "timed_out"
    ? priorRun.latest_phase
    : priorRun.execution_status === "timed_out"
      ? (buildMode === "benchmark_synthetic_replay" ? "synthetic-replay" : buildMode === "benchmark_synthetic" ? "synthetic" : "mirror")
      : null;
  const timedOutLifecyclePhase = previousLifecycle.phases?.find((entry) => entry.phase === priorTimedOutPhase);
  if (!args.resetTrace && priorTimedOutPhase && timedOutLifecyclePhase?.terminal_events?.length === 0) {
    await recorder.recordEvent(priorTimedOutPhase, {
      type: "controller.turn_timed_out",
      reason: "recovered_watchdog_timeout",
    });
  }
  const sourceSkillBefore = await hashTree(path.join(sourceRoot, ".agents", "skills", skillName));
  const startedAt = new Date().toISOString();
  let executionStatus = "not_started";
  let reflectionStatus = args.reflection ? "not_started" : "skipped";
  let executionError = null;
  let reflectionError = null;
  let threadId = null;
  let archive = null;
  let changes = [];
  let workspaceSanitization = null;
  let lifecycleAudit = null;

  try {
    const codex = new Codex({
      apiKey,
      baseUrl,
      env: childEnvironment,
      config: {
        model_provider: "openai",
        model_reasoning_summary: profile.summary,
        show_raw_agent_reasoning: false,
        sandbox_workspace_write: { network_access: true },
      },
    });
    const thread = codex.startThread({
      model,
      modelReasoningEffort: reasoningEffort,
      sandboxMode: "workspace-write",
      approvalPolicy: "never",
      networkAccessEnabled: true,
      webSearchMode: "disabled",
      webSearchEnabled: false,
      workingDirectory: workspace,
    });
    try {
      executionStatus = await runTurn(
        thread,
        phase,
        buildMode === "benchmark_synthetic_replay"
          ? buildSyntheticReplayRepairPrompt({
            site: args.site || priorRun.site,
            requirements,
            recoveryRetry: /-retry-\d+$/.test(phase),
          })
          : buildMode === "benchmark_synthetic"
            ? buildSyntheticRepairPrompt({ site: args.site || priorRun.site, requirements })
          : buildRepairPrompt({
            site: args.site,
            requirements,
            profile: acceptanceProfile,
          }),
        recorder,
        args.timeoutMinutes,
        secrets,
      );
      threadId = thread.id;
    } catch (error) {
      executionStatus = error?.name === "AbortError" ? "timed_out" : "failed";
      if (executionStatus === "timed_out") {
        await recorder.recordEvent(phase, {
          type: "controller.turn_timed_out",
          reason: "watchdog_timeout",
          timeout_minutes: args.timeoutMinutes,
        });
      }
      executionError = redactText(error?.stack || error?.message || String(error), secrets);
      console.error(`${phase} ${executionStatus}: ${executionError}`);
    }
    if (executionStatus === "completed" && (args.site || priorRun.site) && priorRun.url && buildMode === "mirror") {
      const python = process.env.WEBANYTHING_TEST_PYTHON || "python3";
      await runCommand(python, [
        path.join(workspace, "scripts", "emit_contract_artifacts.py"),
        "--workspace", workspace,
        "--site", args.site || priorRun.site,
        "--entry-url", priorRun.url,
        "--profile", acceptanceProfile,
        "--discovery-output", path.join(runDir, "discovery", "discovery-package.json"),
        "--contract-output", path.join(runDir, "contract", "site-contract.json"),
        "--result", path.join(runDir, "build-artifacts.json"),
      ], { cwd: workspace, env: childEnvironment, capture: true });
    }
    await writeFile(path.join(runDir, `${phase}.md`), `${recorder.finalResponse(phase) || `${phase} ${executionStatus}.\n${executionError || ""}`}\n`, { mode: 0o600 });

    if (args.reflection) {
      const proposalRelative = `skill-proposal/${skillName}`;
      const proposalPath = path.join(workspace, proposalRelative);
      // Initial capture seeds this isolated proposal directory, but continuation
      // runs may target older workspaces that predate that seed. Keep reflection
      // advisory and reproducible by creating the same production-skill copy on
      // demand instead of failing after the mirror repair already completed.
      if (!existsSync(proposalPath)) {
        await mkdir(path.dirname(proposalPath), { recursive: true });
        await cp(path.join(sourceRoot, ".agents", "skills", skillName), proposalPath, { recursive: true, force: false });
      }
      const reflectionPhase = `${phase}-reflection`;
      try {
        reflectionStatus = await runTurn(
          thread,
          reflectionPhase,
          buildMode === "benchmark_synthetic_replay"
            ? buildSyntheticReplayReflectionPrompt({ proposalPath: proposalRelative, executionStatus })
            : buildMode === "benchmark_synthetic"
              ? buildSyntheticReflectionPrompt({ proposalPath: proposalRelative, executionStatus })
            : buildRepairReflectionPrompt({ proposalPath: proposalRelative, repairStatus: executionStatus }),
          recorder,
          args.reflectionTimeoutMinutes,
          secrets,
        );
      } catch (error) {
        reflectionStatus = error?.name === "AbortError" ? "timed_out" : "failed";
        reflectionError = redactText(error?.stack || error?.message || String(error), secrets);
        console.error(`${reflectionPhase} ${reflectionStatus}: ${reflectionError}`);
      }
      await writeFile(
        path.join(runDir, `${reflectionPhase}.md`),
        `${recorder.finalResponse(reflectionPhase) || `${reflectionPhase} ${reflectionStatus}.\n${reflectionError || ""}`}\n`,
        { mode: 0o600 },
      );
    }
  } finally {
    try {
      await recorder.close();
      lifecycleAudit = await auditEventLifecycle(runDir);
      await writePrivateJson(path.join(runDir, "lifecycle-audit.json"), lifecycleAudit);
      if (lifecycleAudit.status !== "complete") {
        executionError = `${executionError ? `${executionError}\n` : ""}trace lifecycle is incomplete: ${lifecycleAudit.failures.join("; ")}`;
      }
    } catch (error) {
      const recorderError = redactText(error?.stack || error?.message || String(error), secrets);
      executionError = `${executionError ? `${executionError}\n` : ""}trace recorder finalization failed: ${recorderError}`;
    }
    try {
      const finalInventory = await inventoryTree(workspace, inventoryExclusions);
      changes = diffInventories(baseline, finalInventory);
      await writePrivateJson(path.join(runDir, "changed-files.json"), changes);
      if (args.skipArtifact) {
        archive = null;
        workspaceSanitization = {
          mode: "changed_files_only_artifact_skipped",
          scanned_scope: "top-level trace files only; workspace retained unmodified",
          status: "skipped",
          reason: "--skip-artifact was set; mirror completion is judged from workspace evidence, not the outer merge archive",
          sanitized_text_files: [],
          unsafe_binary_files: [],
          writes_applied: false,
          archive_secret_scan: {
            status: "skipped",
            reason: "--skip-artifact was set",
          },
        };
      } else {
        archive = await createArchive(
          workspace,
          runDir,
          changes,
          childEnvironment,
          secrets,
          args.site || priorRun.site || null,
        );
        workspaceSanitization = {
          mode: "non_mutating_changed_file_archive_scan",
          scanned_scope: "mirror-artifact.tar.gz members derived from changed-files.json",
          sanitized_text_files: [],
          unsafe_binary_files: [],
          writes_applied: false,
          archive_secret_scan: archive.secret_scan,
        };
      }
    } catch (error) {
      const finalizationError = redactText(error?.stack || error?.message || String(error), secrets);
      executionError = `${executionError ? `${executionError}\n` : ""}artifact finalization failed: ${finalizationError}`;
      workspaceSanitization = {
        mode: "non_mutating_changed_file_archive_scan",
        status: "failed",
        error: finalizationError,
        sanitized_text_files: [],
        unsafe_binary_files: [],
        writes_applied: false,
      };
    }
    const sourceSkillAfter = await hashTree(path.join(sourceRoot, ".agents", "skills", skillName));
    await rm(codexHome, { recursive: true, force: true });
    let buildArtifacts = null;
    try {
      const artifactPath = path.join(runDir, "build-artifacts.json");
      if (existsSync(artifactPath)) buildArtifacts = JSON.parse(await readFile(artifactPath, "utf8"));
    } catch {
      buildArtifacts = null;
    }

    const continuation = {
      phase,
      started_at: startedAt,
      finished_at: new Date().toISOString(),
      site: args.site || null,
      model,
      reasoning_effort: reasoningEffort,
      thread_id: threadId,
      requirements: {
        path: path.relative(sourceRoot, path.resolve(args.requirementsFile)),
        bytes: Buffer.byteLength(requirements, "utf8"),
        sha256: createHash("sha256").update(requirements).digest("hex"),
      },
      execution_status: executionStatus,
      execution_error: executionError,
      reflection_status: reflectionStatus,
      reflection_error: reflectionError,
      final_response: `${phase}.md`,
      reflection_response: args.reflection ? `${phase}-reflection.md` : null,
    };
    const runMetadata = {
      ...priorRun,
      site: args.site || priorRun.site || null,
      acceptance_profile: acceptanceProfile,
      artifacts: buildArtifacts,
      finished_at: new Date().toISOString(),
      latest_phase: phase,
      latest_execution_status: executionStatus,
      latest_reflection_status: reflectionStatus,
      model,
      reasoning_effort: reasoningEffort,
      base_url_origin: publicBaseUrl(baseUrl),
      sdk: { package: "@openai/codex-sdk", version: SDK_VERSION },
      production_skill_source_unchanged: priorRun.production_skill_source_unchanged !== false && sourceSkillBefore === sourceSkillAfter,
      production_skill_sha256_after: sourceSkillAfter,
      changed_files: changes.length,
      outer_artifact_status: args.skipArtifact ? "skipped" : archive ? "created" : "not_available",
      outer_artifact_required_for_mirror_acceptance: false,
      mirror_artifact: archive,
      trace_lifecycle: lifecycleAudit ? {
        path: "lifecycle-audit.json",
        status: lifecycleAudit.status,
        event_count: lifecycleAudit.event_count,
        dropped_event_count: lifecycleAudit.dropped_event_count,
        orphan_started_item_count: lifecycleAudit.orphan_started_items.length,
      } : null,
      workspace_secret_scan: workspaceSanitization,
      private_codex_home_removed: true,
      continuations: [...continuations, continuation],
      trace_reset: args.resetTrace ? {
        prior_trace_archived: true,
        path: path.relative(runDir, traceResetArchive).split(path.sep).join("/"),
      } : null,
      fatal_error: executionError,
    };
    await writePrivateJson(path.join(runDir, "run.json"), runMetadata);
    const traceSanitization = await sanitizeExactSecrets(runDir, secrets, ["workspace", "mirror-artifact.tar.gz"]);
    if (traceSanitization.unsafe_binary_files.length > 0) {
      console.error(`unsafe trace binaries: ${traceSanitization.unsafe_binary_files.join(", ")}`);
      process.exitCode = 1;
    }
    const artifactNames = (await topLevelArtifactFiles(runDir))
      .filter((name) => !(args.skipArtifact && name === "mirror-artifact.tar.gz"));
    const artifactFiles = Object.fromEntries(
      await Promise.all(artifactNames.map(async (name) => [name, await describeFile(path.join(runDir, name))])),
    );
    const proposalDirectory = path.join(workspace, "skill-proposal", skillName);
    await writePrivateJson(path.join(runDir, "artifact-manifest.json"), {
      schema: "webharbor-codex-sdk-artifacts-v1",
      outer_artifact_status: args.skipArtifact ? "skipped" : archive ? "created" : "not_available",
      outer_artifact_required_for_mirror_acceptance: false,
      mirror_artifact: archive,
      files: artifactFiles,
      proposal: existsSync(proposalDirectory) ? {
        path: `workspace/skill-proposal/${skillName}`,
        sha256: await hashTree(proposalDirectory),
      } : null,
      secret_scan: traceSanitization,
    });
  }

  if (executionError || executionStatus !== "completed" || (args.reflection && reflectionStatus !== "completed")) {
    process.exitCode = 1;
  } else {
    console.log(`trace continuation complete: ${runDir} (${phase})`);
  }
}

await main();
