#!/usr/bin/env node

import { Codex } from "@openai/codex-sdk";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import {
  EventRecorder,
  auditEventLifecycle,
  auditTarArchiveSecrets,
  buildExecutionPrompt,
  buildReflectionPrompt,
  buildSyntheticExecutionPrompt,
  buildSyntheticReflectionPrompt,
  buildSyntheticReplayExecutionPrompt,
  buildSyntheticReplayReflectionPrompt,
  createDeterministicTarGzip,
  describeFile,
  diffInventories,
  hashTree,
  inventoryTree,
  parseAgentProfile,
  publicBaseUrl,
  redactText,
  selectMirrorArtifactPaths,
  sanitizeExactSecrets,
  writePrivateJson,
} from "./lib.mjs";

const SDK_VERSION = "0.146.1";
function usage() {
  return `usage: capture.mjs URL [options]\n\nOptions:\n  --output DIR                  Trace run directory\n  --model MODEL                Override model (default: gpt-5.6-sol)\n  --agent-config FILE          Optional model/effort/summary YAML\n  --reasoning-effort LEVEL     minimal|low|medium|high|xhigh\n  --site SITE                  Stable WebHarbor site id\n  --build-mode MODE            mirror|benchmark_synthetic|benchmark_synthetic_replay\n  --profile PROFILE            mirror profile or matching synthetic profile\n  --synthetic-contract FILE    Frozen compiled contract for synthetic mode\n  --max-pages N                Forward bounded page count to the mirror skill\n  --max-assets N               Forward bounded asset count to the mirror skill\n  --supplemental-url URL       Add a public mirror source URL (repeatable)\n  --requirements-file FILE     Add a UTF-8 task acceptance brief (max 64 KiB)\n  --private-research-snapshot  Record the mirror skill's local-only research basis\n  --timeout-minutes N          Builder turn timeout (default: 90)\n  --reflection-timeout-minutes N  Reflection timeout (default: 20)\n  --skip-artifact              Skip the outer candidate archive\n  --no-reflection              Skip the advisory skill-proposal turn\n`;
}

function parseArgs(argv) {
  const args = {
    agentConfig: process.env.WEBANYTHING_AGENT_CONFIG || null,
    timeoutMinutes: 90,
    reflectionTimeoutMinutes: 20,
    reflection: true,
    skipArtifact: false,
    supplementalUrls: [],
    profile: "task-driven",
    buildMode: "mirror",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    const next = () => {
      index += 1;
      if (index >= argv.length) throw new Error(`${value} requires a value`);
      return argv[index];
    };
    if (value === "--output") args.output = next();
    else if (value === "--model") args.model = next();
    else if (value === "--agent-config") args.agentConfig = next();
    else if (value === "--reasoning-effort") args.reasoningEffort = next();
    else if (value === "--site") args.site = next();
    else if (value === "--profile") args.profile = next();
    else if (value === "--build-mode") args.buildMode = next();
    else if (value === "--synthetic-contract") args.syntheticContract = next();
    else if (value === "--max-pages") args.maxPages = Number(next());
    else if (value === "--max-assets") args.maxAssets = Number(next());
    else if (value === "--supplemental-url") args.supplementalUrls.push(next());
    else if (value === "--requirements-file") args.requirementsFile = next();
    else if (value === "--timeout-minutes") args.timeoutMinutes = Number(next());
    else if (value === "--reflection-timeout-minutes") args.reflectionTimeoutMinutes = Number(next());
    else if (value === "--private-research-snapshot") args.privateResearchSnapshot = true;
    else if (value === "--skip-artifact") args.skipArtifact = true;
    else if (value === "--no-reflection") args.reflection = false;
    else if (value === "--help" || value === "-h") args.help = true;
    else if (value.startsWith("-")) throw new Error(`unknown option: ${value}`);
    else if (!args.url) args.url = value;
    else throw new Error(`unexpected positional argument: ${value}`);
  }
  return args;
}

function requireHttpUrl(rawValue) {
  const parsed = new URL(rawValue);
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("URL must use http or https");
  }
  if (parsed.username || parsed.password) {
    throw new Error("URL must not contain credentials");
  }
  return parsed.toString();
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
  // Keep login/non-login shell helpers inside the isolated trace home. Leaving
  // HOME unset makes some Codex builds fall back to the real account home,
  // which is intentionally outside the workspace sandbox.
  environment.HOME = codexHome;
  environment.PYTHONDONTWRITEBYTECODE = "1";
  const dependencyPath = path.join(sourceRoot, ".assets", "builder-python");
  const browserPath = path.join(sourceRoot, ".assets", "playwright");
  if (existsSync(path.join(dependencyPath, ".webharbor-builder-ready"))) {
    environment.WEBHARBOR_BUILDER_DEPENDENCY_PATH = dependencyPath;
    environment.WEBHARBOR_RUNTIME_DEPENDENCY_PATH = dependencyPath;
    environment.PYTHONPATH = dependencyPath;
  }
  if (existsSync(browserPath)) {
    environment.PLAYWRIGHT_BROWSERS_PATH = browserPath;
  }
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
      if (code === 0 && !signal) {
        resolve(Buffer.concat(stdout).toString("utf8"));
      } else {
        reject(new Error(`${command} exited with ${signal ? `signal ${signal}` : `code ${code}`}: ${Buffer.concat(stderr).toString("utf8")}`));
      }
    });
  });
}

async function copyWorkspace(sourceRoot, workspace, runDir, environment) {
  await mkdir(workspace, { recursive: true, mode: 0o700 });
  const exclusions = [
    "/.git/", "/.assets/", "/.venv/", "/.codex/", "/generated/", "/runs/", "node_modules/",
    "*.local.env", "config/*.local.env", "sites/.cache/", "sites/*/static/images/",
    "sites/*/static/external_cache/", "sites/*/instance/", "sites/*/instance_seed/",
    "mirror_factory/captures/", "mirror_factory/evidence/", "mirror_factory/upstream_specs/",
  ];
  const relativeRunDir = path.relative(sourceRoot, runDir);
  if (relativeRunDir && !relativeRunDir.startsWith("..")) exclusions.push(`/${relativeRunDir.split(path.sep).join("/")}/`);
  // A release archive may carry numeric owners from the machine that created
  // it. Preserving those owners can make the isolated copy read-only to the
  // SDK sandbox on another host. Keep metadata needed for fidelity, but make
  // the copy belong to the user running WebAnything and ensure that user can
  // update it.
  const args = ["-a", "--no-owner", "--no-group", "--chmod=u+rwX"];
  for (const exclusion of exclusions) args.push("--exclude", exclusion);
  args.push(`${sourceRoot}/`, `${workspace}/`);
  await runCommand("rsync", args, { env: environment });
  await runCommand("git", ["init", "-q"], { cwd: workspace, env: environment });
}

function safeSyntheticArtifact(base, rawPath, label) {
  if (typeof rawPath !== "string" || !rawPath || path.isAbsolute(rawPath)) {
    throw new Error(`${label} must be a nonempty relative path`);
  }
  const resolved = path.resolve(base, rawPath);
  if (resolved !== base && !resolved.startsWith(`${base}${path.sep}`)) {
    throw new Error(`${label} escapes the synthetic contract bundle`);
  }
  return resolved;
}

async function copySyntheticBundle(contractFile, workspace, url, site) {
  const sourceContract = path.resolve(contractFile);
  const sourceBase = path.dirname(sourceContract);
  const contract = JSON.parse(await readFile(sourceContract, "utf8"));
  if (
    contract?.schema !== "webanything-synthetic-contract-v1"
    || contract?.build_mode !== "benchmark_synthetic"
    || contract?.site_id !== site
    || requireHttpUrl(contract?.seed_url) !== url
  ) {
    throw new Error("synthetic contract does not match the requested site, mode, and URL seed");
  }
  if (!Array.isArray(contract.fixtures) || !contract.fixtures.length || !contract.tasks || contract.visual_reference?.required !== true) {
    throw new Error("synthetic contract lacks hash-bound fixtures, tasks, or visual reference");
  }
  const targetBase = path.join(workspace, ".webanything", "synthetic-input");
  await mkdir(targetBase, { recursive: true });
  await cp(sourceContract, path.join(targetBase, "synthetic_contract.json"), { force: false });
  const artifacts = [...contract.fixtures, contract.tasks];
  for (const [index, artifact] of artifacts.entries()) {
    const source = safeSyntheticArtifact(sourceBase, artifact?.path, `artifact[${index}].path`);
    const target = safeSyntheticArtifact(targetBase, artifact?.path, `artifact[${index}].path`);
    const descriptor = await describeFile(source);
    if (descriptor.sha256 !== artifact.sha256) {
      throw new Error(`synthetic artifact hash mismatch: ${artifact.path}`);
    }
    await mkdir(path.dirname(target), { recursive: true });
    await cp(source, target, { force: false });
  }
  const referenceSource = safeSyntheticArtifact(sourceBase, contract.visual_reference.path, "visual_reference.path");
  const referenceDescriptor = await describeFile(referenceSource);
  if (referenceDescriptor.sha256 !== contract.visual_reference.sha256) {
    throw new Error("synthetic visual reference manifest hash mismatch");
  }
  const reference = JSON.parse(await readFile(referenceSource, "utf8"));
  if (
    reference?.schema !== "webanything-synthetic-visual-reference-v1"
    || reference?.state !== "reference_frozen"
    || reference?.site !== site
    || reference?.reference_sha256 !== contract.visual_reference.reference_sha256
    || reference?.raw_material_persisted !== true
    || !Array.isArray(reference?.routes)
    || !reference.routes.length
  ) {
    throw new Error("synthetic visual reference identity is invalid");
  }
  const referenceArtifacts = [
    { path: contract.visual_reference.path, sha256: contract.visual_reference.sha256 },
    ...reference.routes.flatMap((route) => [
      route?.screenshot,
      route?.comparison_screenshot,
      route?.geometry,
    ]),
  ];
  for (const [index, artifact] of referenceArtifacts.entries()) {
    const relative = index === 0
      ? artifact.path
      : path.join(path.dirname(contract.visual_reference.path), artifact?.path || "");
    const source = safeSyntheticArtifact(sourceBase, relative, `visual_reference.artifact[${index}]`);
    const target = safeSyntheticArtifact(targetBase, relative, `visual_reference.artifact[${index}]`);
    const descriptor = await describeFile(source);
    if (descriptor.sha256 !== artifact.sha256) {
      throw new Error(`synthetic visual reference artifact hash mismatch: ${relative}`);
    }
    await mkdir(path.dirname(target), { recursive: true });
    await cp(source, target, { force: false });
  }
  return {
    path: ".webanything/synthetic-input/synthetic_contract.json",
    ...(await describeFile(sourceContract)),
    fixtures: contract.fixtures.length,
    tasks: contract.tasks.count,
    visual_reference: {
      path: contract.visual_reference.path,
      sha256: contract.visual_reference.sha256,
      reference_sha256: contract.visual_reference.reference_sha256,
      routes: reference.routes.length,
      content_mode: contract.content_policy?.mode || "fictional",
    },
  };
}

async function copyReplayBundle(contractFile, workspace, url, site) {
  const sourceContract = path.resolve(contractFile);
  const sourceBase = path.dirname(sourceContract);
  const contract = JSON.parse(await readFile(sourceContract, "utf8"));
  if (
    contract?.schema !== "webanything-synthetic-replay-contract-v1"
    || contract?.build_mode !== "benchmark_synthetic_replay"
    || contract?.site_id !== site
    || requireHttpUrl(contract?.seed_url) !== url
  ) throw new Error("synthetic replay contract does not match the requested site, mode, and URL seed");
  if (!contract.capture_manifest || !contract.storage?.seed_path || !contract.tasks || !contract.visual_reference) {
    throw new Error("synthetic replay contract lacks capture, SQLite seed, tasks, or visual reference");
  }
  const targetBase = path.join(workspace, ".webanything", "replay-input");
  await mkdir(targetBase, { recursive: true });
  await cp(sourceContract, path.join(targetBase, "synthetic_replay_contract.json"), { force: false });
  const captureSource = safeSyntheticArtifact(sourceBase, contract.capture_manifest.path, "capture_manifest.path");
  if ((await describeFile(captureSource)).sha256 !== contract.capture_manifest.sha256) {
    throw new Error("synthetic replay capture manifest hash mismatch");
  }
  const capture = JSON.parse(await readFile(captureSource, "utf8"));
  if (
    capture?.schema !== "webanything-replay-capture-manifest-v1"
    || capture?.external_javascript_execution !== false
    || capture?.source_auth_persisted !== false
    || capture?.privacy?.status !== "passed"
    || capture?.privacy?.source_credentials_removed !== true
    || !Array.isArray(capture?.files)
    || !capture.files.length
  ) throw new Error("synthetic replay capture manifest is unsafe or empty");
  const artifacts = [
    contract.capture_manifest,
    { path: contract.storage.seed_path, sha256: contract.storage.seed_sha256 },
    contract.tasks,
    ...capture.files,
  ];
  for (const [index, artifact] of artifacts.entries()) {
    const source = safeSyntheticArtifact(sourceBase, artifact.path, `replay_artifact[${index}].path`);
    const target = safeSyntheticArtifact(targetBase, artifact.path, `replay_artifact[${index}].path`);
    if ((await describeFile(source)).sha256 !== artifact.sha256) throw new Error(`synthetic replay artifact hash mismatch: ${artifact.path}`);
    await mkdir(path.dirname(target), { recursive: true });
    await cp(source, target, { force: false });
  }
  const referenceSource = safeSyntheticArtifact(sourceBase, contract.visual_reference.path, "visual_reference.path");
  if ((await describeFile(referenceSource)).sha256 !== contract.visual_reference.sha256) {
    throw new Error("synthetic replay visual reference manifest hash mismatch");
  }
  const reference = JSON.parse(await readFile(referenceSource, "utf8"));
  if (
    reference?.schema !== "webanything-synthetic-visual-reference-v1"
    || reference?.state !== "reference_frozen"
    || reference?.site !== site
    || reference?.reference_sha256 !== contract.visual_reference.reference_sha256
    || !Array.isArray(reference?.routes)
    || !reference.routes.length
  ) throw new Error("synthetic replay visual reference identity is invalid");
  const referenceArtifacts = [
    { path: contract.visual_reference.path, sha256: contract.visual_reference.sha256 },
    ...reference.routes.flatMap((route) => [route?.screenshot, route?.comparison_screenshot, route?.geometry]),
  ];
  for (const [index, artifact] of referenceArtifacts.entries()) {
    const relative = index === 0 ? artifact.path : path.join(path.dirname(contract.visual_reference.path), artifact?.path || "");
    const source = safeSyntheticArtifact(sourceBase, relative, `visual_reference.artifact[${index}]`);
    const target = safeSyntheticArtifact(targetBase, relative, `visual_reference.artifact[${index}]`);
    if ((await describeFile(source)).sha256 !== artifact.sha256) throw new Error(`synthetic replay visual artifact hash mismatch: ${relative}`);
    await mkdir(path.dirname(target), { recursive: true });
    await cp(source, target, { force: false });
  }
  const candidate = path.join(workspace, "sites", site);
  await mkdir(path.join(candidate, "pages"), { recursive: true });
  await mkdir(path.join(candidate, "assets"), { recursive: true });
  await mkdir(path.join(candidate, "fixtures"), { recursive: true });
  await mkdir(path.join(candidate, "runtime"), { recursive: true });
  await cp(sourceContract, path.join(candidate, "synthetic_replay_contract.json"), { force: false });
  await cp(
    safeSyntheticArtifact(sourceBase, contract.storage.seed_path, "storage.seed_path"),
    path.join(candidate, "fixtures", "seed.sqlite3"),
    { force: false },
  );
  await cp(
    safeSyntheticArtifact(sourceBase, contract.tasks.path, "tasks.path"),
    path.join(candidate, "tasks.jsonl"),
    { force: false },
  );
  for (const artifact of capture.files) {
    let relative = null;
    if (artifact.path.startsWith("capture/pages/")) relative = artifact.path.slice("capture/".length);
    else if (artifact.path.startsWith("capture/assets/")) relative = artifact.path.slice("capture/".length);
    if (!relative) continue;
    const target = safeSyntheticArtifact(candidate, relative, "candidate replay artifact");
    await mkdir(path.dirname(target), { recursive: true });
    await cp(safeSyntheticArtifact(sourceBase, artifact.path, "capture file"), target, { force: false });
  }
  await cp(
    path.join(workspace, "runtime", "webanything_replay_runtime.py"),
    path.join(candidate, "runtime", "webanything_replay_runtime.py"),
    { force: false },
  );
  await cp(
    path.join(workspace, "runtime", "webanything_replay_surface_app.py"),
    path.join(candidate, "app.py"),
    { force: false },
  );
  return {
    path: ".webanything/replay-input/synthetic_replay_contract.json",
    ...(await describeFile(sourceContract)),
    captured_files: capture.files.length,
    tasks: contract.tasks.count,
    seed_sha256: contract.storage.seed_sha256,
    capture_bundle_sha256: contract.capture_manifest.bundle_sha256,
    visual_reference: {
      path: contract.visual_reference.path,
      sha256: contract.visual_reference.sha256,
      reference_sha256: contract.visual_reference.reference_sha256,
      routes: reference.routes.length,
      content_mode: "source_fidelity",
    },
  };
}

function printEvent(phase, event, secrets) {
  if (!["item.completed", "turn.failed", "error"].includes(event.type)) return;
  if (event.type === "item.completed") {
    const item = event.item;
    if (item.type === "reasoning") console.log(`[${phase}] reasoning summary: ${redactText(item.text, secrets)}`);
    else if (item.type === "agent_message") console.log(`[${phase}] assistant: ${redactText(item.text, secrets)}`);
    else if (item.type === "command_execution") console.log(`[${phase}] command (${item.status}): ${redactText(item.command, secrets)}`);
    else if (item.type === "file_change") console.log(`[${phase}] file change (${item.status}): ${item.changes.map((entry) => entry.path).join(", ")}`);
    else if (item.type === "mcp_tool_call") console.log(`[${phase}] MCP ${item.server}/${item.tool}: ${item.status}`);
  } else {
    console.error(`[${phase}] ${redactText(JSON.stringify(event), secrets)}`);
  }
}

async function runTurn(thread, phase, prompt, recorder, timeoutMinutes, secrets) {
  await recorder.recordPrompt(phase, prompt);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMinutes * 60_000);
  let status = "failed";
  try {
    const { events } = await thread.runStreamed(prompt, { signal: controller.signal });
    for await (const event of events) {
      const sanitized = await recorder.recordEvent(phase, event);
      printEvent(phase, sanitized, secrets);
      if (sanitized.type === "turn.completed") status = "completed";
      if (sanitized.type === "turn.failed" || sanitized.type === "error") status = "failed";
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
  const descriptor = await describeFile(archivePath);
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
    ...descriptor,
    files: paths.length,
    artifact_scope: selected.artifact_scope,
    excluded_transient_paths: selected.excluded_transient_paths,
    excluded_out_of_scope_paths: selected.excluded_out_of_scope_paths,
    secret_scan: secretScan,
  };
}

async function finalizeWorkspaceArtifacts({ workspace, runDir, secrets, environment, baseline, site, skipArtifact }) {
  const finalInventory = await inventoryTree(workspace, [".git", "skill-proposal"]);
  const changes = diffInventories(baseline, finalInventory);
  await writePrivateJson(path.join(runDir, "changed-files.json"), changes);
  if (skipArtifact) {
    const workspaceSanitization = {
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
    return { workspaceSanitization, changes, archive: null };
  }
  const archive = await createArchive(workspace, runDir, changes, environment, secrets, site);
  const workspaceSanitization = {
    mode: "non_mutating_changed_file_archive_scan",
    scanned_scope: "mirror-artifact.tar.gz members derived from changed-files.json",
    sanitized_text_files: [],
    unsafe_binary_files: [],
    writes_applied: false,
    archive_secret_scan: archive.secret_scan,
  };
  return { workspaceSanitization, changes, archive };
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
  if (!args.url) {
    console.error(usage());
    process.exitCode = 64;
    return;
  }
  if (!Number.isFinite(args.timeoutMinutes) || args.timeoutMinutes <= 0) throw new Error("--timeout-minutes must be positive");
  if (!Number.isFinite(args.reflectionTimeoutMinutes) || args.reflectionTimeoutMinutes <= 0) throw new Error("--reflection-timeout-minutes must be positive");
  const url = requireHttpUrl(args.url);
  const supplementalUrls = args.supplementalUrls.map(requireHttpUrl);
  if (!["mirror", "benchmark_synthetic", "benchmark_synthetic_replay"].includes(args.buildMode)) {
    throw new Error("--build-mode must be mirror, benchmark_synthetic, or benchmark_synthetic_replay");
  }
  if (["benchmark_synthetic", "benchmark_synthetic_replay"].includes(args.buildMode)) {
    if (args.profile !== args.buildMode || !args.syntheticContract || !args.site) {
      throw new Error("synthetic mode requires a matching profile, --synthetic-contract, and --site");
    }
    if (supplementalUrls.length || args.privateResearchSnapshot) {
      throw new Error("synthetic mode does not accept supplemental or private research inputs");
    }
  } else if (!["task-driven", "immersive", "authenticated"].includes(args.profile) || args.syntheticContract) {
    throw new Error("mirror mode requires a mirror profile and does not accept --synthetic-contract");
  }
  let requirements = "";
  if (args.requirementsFile) {
    requirements = await readFile(path.resolve(args.requirementsFile), "utf8");
    if (Buffer.byteLength(requirements, "utf8") > 65_536) {
      throw new Error("--requirements-file must not exceed 64 KiB");
    }
  }
  const apiKey = process.env.CODEX_MIRROR_API_KEY;
  const baseUrl = process.env.CODEX_MIRROR_BASE_URL;
  if (!apiKey || !baseUrl) throw new Error("provider credentials are missing; use scripts/run_codex_trace.sh capture");
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const profileText = args.agentConfig ? await readFile(path.resolve(args.agentConfig), "utf8") : "";
  const profile = parseAgentProfile(profileText);
  const model = args.model || profile.model;
  const reasoningEffort = args.reasoningEffort || profile.effort;
  const allowedEfforts = new Set(["minimal", "low", "medium", "high", "xhigh"]);
  if (!allowedEfforts.has(reasoningEffort)) throw new Error(`unsupported reasoning effort: ${reasoningEffort}`);
  const runId = `${new Date().toISOString().replace(/[:.]/g, "-")}-${randomBytes(3).toString("hex")}`;
  const runDir = path.resolve(args.output || path.join(sourceRoot, "generated", "codex-mirror-traces", runId));
  const workspace = path.join(runDir, "workspace");
  const codexHome = await mkdtemp(path.join(os.tmpdir(), `webharbor-codex-home-${runId}-`));
  const proxySecrets = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"].flatMap((name) => process.env[name] ? [process.env[name]] : []);
  const secrets = [apiKey, ...proxySecrets];
  // `capture.mjs` is an initial Builder turn even when the supervisor retries
  // it in the same run directory. Keep the existing workspace/candidate, but
  // start a fresh trace so completed prior attempts cannot create duplicate
  // lifecycle terminals in the new attempt. `continue.mjs` retains the
  // recorder's append default for intentional repair continuations.
  const recorder = new EventRecorder(runDir, secrets, { append: false });
  await recorder.initialize();
  const childEnvironment = safeEnvironment({ codexHome, sourceRoot });
  let executionStatus = "not_started";
  let reflectionStatus = args.reflection ? "not_started" : "skipped";
  let sourceSkillBefore = "";
  let sourceSkillAfter = "";
  let archive = null;
  let changes = [];
  let workspaceSanitization = null;
  let lifecycleAudit = null;
  let reflectionError = null;
  let baseline = null;
  let fatalError = null;
  let syntheticContractDescriptor = null;
  const skillName = args.buildMode === "benchmark_synthetic_replay"
    ? "build-synthetic-replays"
    : args.buildMode === "benchmark_synthetic"
      ? "build-synthetic-benchmarks"
      : "build-webharbor-mirrors";
  const executionPhase = args.buildMode === "benchmark_synthetic_replay" ? "synthetic-replay" : args.buildMode === "benchmark_synthetic" ? "synthetic" : "mirror";
  const startedAt = new Date().toISOString();
  try {
    console.log(`run directory: ${runDir}`);
    console.log("creating isolated WebHarbor workspace...");
    sourceSkillBefore = await hashTree(path.join(sourceRoot, ".agents", "skills", skillName));
    await copyWorkspace(sourceRoot, workspace, runDir, childEnvironment);
    if (args.buildMode === "benchmark_synthetic") {
      syntheticContractDescriptor = await copySyntheticBundle(args.syntheticContract, workspace, url, args.site);
    } else if (args.buildMode === "benchmark_synthetic_replay") {
      syntheticContractDescriptor = await copyReplayBundle(args.syntheticContract, workspace, url, args.site);
    }
    baseline = await inventoryTree(workspace, [".git", "skill-proposal"]);
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
    const executionPrompt = args.buildMode === "benchmark_synthetic_replay"
      ? buildSyntheticReplayExecutionPrompt({ url, site: args.site, requirements })
      : args.buildMode === "benchmark_synthetic"
        ? buildSyntheticExecutionPrompt({ url, site: args.site, requirements })
      : buildExecutionPrompt({
        url,
        site: args.site,
        profile: args.profile,
        maxPages: args.maxPages,
        maxAssets: args.maxAssets,
        privateResearchSnapshot: args.privateResearchSnapshot,
        supplementalUrls,
        requirements,
      });
    try {
      executionStatus = await runTurn(thread, executionPhase, executionPrompt, recorder, args.timeoutMinutes, secrets);
    } catch (error) {
      executionStatus = error?.name === "AbortError" ? "timed_out" : "failed";
      if (executionStatus === "timed_out") {
        await recorder.recordEvent(executionPhase, {
          type: "controller.turn_timed_out",
          reason: "watchdog_timeout",
          timeout_minutes: args.timeoutMinutes,
        });
      }
      throw error;
    }
    // Bridge bounded reconstruction evidence into the explicit discovery /
    // Site-Contract interface before packaging the workspace.  The emitter is
    // deterministic and fail-closed when the candidate cannot prove `/`.
    if (executionStatus === "completed" && args.site && args.buildMode === "mirror") {
      const python = process.env.WEBANYTHING_TEST_PYTHON || "python3";
      await runCommand(python, [
        path.join(workspace, "scripts", "emit_contract_artifacts.py"),
        "--workspace", workspace,
        "--site", args.site,
        "--entry-url", url,
        "--profile", args.profile,
        "--discovery-output", path.join(runDir, "discovery", "discovery-package.json"),
        "--contract-output", path.join(runDir, "contract", "site-contract.json"),
        "--result", path.join(runDir, "build-artifacts.json"),
      ], { cwd: workspace, env: childEnvironment, capture: true });
    }
    await writeFile(path.join(runDir, "final.md"), `${recorder.finalResponse(executionPhase)}\n`, { mode: 0o600 });

    if (args.reflection) {
      const proposalRelative = `skill-proposal/${skillName}`;
      const proposalPath = path.join(workspace, proposalRelative);
      await mkdir(path.dirname(proposalPath), { recursive: true });
      await cp(path.join(sourceRoot, ".agents", "skills", skillName), proposalPath, { recursive: true, force: false });
      const reflectionPrompt = args.buildMode === "benchmark_synthetic_replay"
        ? buildSyntheticReplayReflectionPrompt({ proposalPath: proposalRelative, executionStatus })
        : args.buildMode === "benchmark_synthetic"
          ? buildSyntheticReflectionPrompt({ proposalPath: proposalRelative, executionStatus })
        : buildReflectionPrompt({ proposalPath: proposalRelative, executionStatus });
      try {
        reflectionStatus = await runTurn(
          thread,
          "reflection",
          reflectionPrompt,
          recorder,
          args.reflectionTimeoutMinutes,
          secrets,
        );
      } catch (error) {
        reflectionStatus = error?.name === "AbortError" ? "timed_out" : "failed";
        reflectionError = redactText(error?.stack || error?.message || String(error), secrets);
        console.error(`reflection ${reflectionStatus}: ${reflectionError}`);
      }
      const reflectionResponse = recorder.finalResponse("reflection");
      await writeFile(
        path.join(runDir, "reflection.md"),
        `${reflectionResponse || `Reflection ${reflectionStatus}. See events.jsonl and the isolated proposal directory.\n${reflectionError || ""}`}\n`,
        { mode: 0o600 },
      );
    }

  } catch (error) {
    fatalError = redactText(error?.stack || error?.message || String(error), secrets);
    console.error(fatalError);
  } finally {
    try {
      await recorder.close();
      lifecycleAudit = await auditEventLifecycle(runDir);
      await writePrivateJson(path.join(runDir, "lifecycle-audit.json"), lifecycleAudit);
      if (lifecycleAudit.status !== "complete") {
        const lifecycleError = `trace lifecycle is incomplete: ${lifecycleAudit.failures.join("; ")}`;
        fatalError = fatalError ? `${fatalError}\n${lifecycleError}` : lifecycleError;
      }
    } catch (error) {
      const recorderError = redactText(error?.stack || error?.message || String(error), secrets);
      fatalError = fatalError ? `${fatalError}\nTrace recorder finalization failed:\n${recorderError}` : recorderError;
    }
    if (baseline && existsSync(workspace) && archive === null) {
      try {
        const finalized = await finalizeWorkspaceArtifacts({
          workspace,
          runDir,
          secrets,
          environment: childEnvironment,
          baseline,
          site: args.site ?? null,
          skipArtifact: args.skipArtifact,
        });
        workspaceSanitization = finalized.workspaceSanitization;
        changes = finalized.changes;
        archive = finalized.archive;
      } catch (error) {
        const finalizationError = redactText(error?.stack || error?.message || String(error), secrets);
        fatalError = fatalError ? `${fatalError}\nArtifact finalization failed:\n${finalizationError}` : finalizationError;
        console.error(`artifact finalization failed: ${finalizationError}`);
      }
    }
    try {
      sourceSkillAfter = await hashTree(path.join(sourceRoot, ".agents", "skills", skillName));
    } catch (error) {
      sourceSkillAfter = `error:${redactText(error.message, secrets)}`;
    }
    await rm(codexHome, { recursive: true, force: true });
    let buildArtifacts = null;
    try {
      const artifactPath = path.join(runDir, "build-artifacts.json");
      if (existsSync(artifactPath)) buildArtifacts = JSON.parse(await readFile(artifactPath, "utf8"));
    } catch {
      buildArtifacts = null;
    }
    const runMetadata = {
      schema: "webharbor-codex-sdk-trace-v1",
      started_at: startedAt,
      finished_at: new Date().toISOString(),
      url,
      site: args.site ?? null,
      build_mode: args.buildMode,
      acceptance_profile: args.profile,
      synthetic_contract: syntheticContractDescriptor,
      supplemental_urls: supplementalUrls,
      requirements_brief: requirements ? {
        provided: true,
        bytes: Buffer.byteLength(requirements, "utf8"),
      } : null,
      model,
      reasoning_effort: reasoningEffort,
      reasoning_summary: profile.summary,
      base_url_origin: publicBaseUrl(baseUrl),
      credential_source: "process environment; values never persisted",
      sdk: { package: "@openai/codex-sdk", version: SDK_VERSION },
      trace_scope: {
        captured: ["SDK events delivered to the runner", "assistant content", "reasoning summaries", "commands and outputs", "file changes", "MCP calls/results", "plans", "errors", "usage"],
        unavailable: "Hidden chain-of-thought not exposed by the SDK is not captured or reconstructed",
        sanitized: true,
      },
      trace_lifecycle: lifecycleAudit ? {
        path: "lifecycle-audit.json",
        status: lifecycleAudit.status,
        event_count: lifecycleAudit.event_count,
        dropped_event_count: lifecycleAudit.dropped_event_count,
        orphan_started_item_count: lifecycleAudit.orphan_started_items.length,
      } : null,
      run_directory: runDir,
      workspace,
      artifacts: buildArtifacts,
      execution_status: executionStatus,
      reflection_status: reflectionStatus,
      reflection_error: reflectionError,
      production_skill_source_unchanged: Boolean(sourceSkillBefore) && sourceSkillBefore === sourceSkillAfter,
      production_skill_sha256_before: sourceSkillBefore,
      production_skill_sha256_after: sourceSkillAfter,
      changed_files: changes.length,
      outer_artifact_status: args.skipArtifact ? "skipped" : archive ? "created" : "not_available",
      outer_artifact_required_for_mirror_acceptance: false,
      mirror_artifact: archive,
      workspace_secret_scan: workspaceSanitization,
      private_codex_home_removed: true,
      fatal_error: fatalError,
    };
    await writePrivateJson(path.join(runDir, "run.json"), runMetadata);
    const traceSanitization = await sanitizeExactSecrets(runDir, secrets, ["workspace", "mirror-artifact.tar.gz"]);
    if (traceSanitization.unsafe_binary_files.length > 0) {
      console.error(`unsafe trace binaries: ${traceSanitization.unsafe_binary_files.join(", ")}`);
      process.exitCode = 1;
    }
    const artifactNames = [
      "events.jsonl", "messages.jsonl", "tool-calls.jsonl", "prompts.jsonl", "final.md",
      "reflection.md", "lifecycle-audit.json", "changed-files.json", "run.json", "mirror-artifact.tar.gz",
    ].filter((name) => existsSync(path.join(runDir, name)) && !(args.skipArtifact && name === "mirror-artifact.tar.gz"));
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
      proposal: args.reflection && existsSync(proposalDirectory) ? {
        path: `workspace/skill-proposal/${skillName}`,
        sha256: await hashTree(proposalDirectory),
      } : null,
      secret_scan: traceSanitization,
    });
  }
  if (fatalError || executionStatus !== "completed" || (args.reflection && reflectionStatus !== "completed")) {
    process.exitCode = 1;
  } else {
    console.log(`trace complete: ${runDir}`);
  }
}

await main();
