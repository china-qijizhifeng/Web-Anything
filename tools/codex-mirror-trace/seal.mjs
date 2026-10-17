#!/usr/bin/env node

import { existsSync } from "node:fs";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import {
  auditEventLifecycle,
  auditTarArchiveSecrets,
  describeFile,
  hashTree,
  sanitizeExactSecrets,
  scanSensitiveFiles,
  verifyTraceArtifactBinding,
  writePrivateJson,
} from "./lib.mjs";

function usage() {
  return "usage: seal.mjs RUN_DIR\n";
}

async function topLevelArtifactFiles(runDir) {
  const names = await readdir(runDir);
  return names.filter((name) => {
    if (["workspace", "artifact-manifest.json"].includes(name)) return false;
    return name.endsWith(".jsonl") || name.endsWith(".json") || name.endsWith(".md") || name === "mirror-artifact.tar.gz";
  }).sort();
}

async function main() {
  const values = process.argv.slice(2);
  if (values.length !== 1 || values[0] === "--help" || values[0] === "-h") {
    console.error(usage());
    process.exitCode = values.length === 1 ? 0 : 64;
    return;
  }
  const runDir = path.resolve(values[0]);
  const workspace = path.join(runDir, "workspace");
  const archivePath = path.join(runDir, "mirror-artifact.tar.gz");
  const runPath = path.join(runDir, "run.json");
  if (!existsSync(runPath) || !existsSync(path.join(runDir, "events.jsonl"))) {
    throw new Error(`not a trace run directory: ${runDir}`);
  }
  const secrets = [
    process.env.CODEX_MIRROR_API_KEY,
    process.env.HTTP_PROXY,
    process.env.HTTPS_PROXY,
    process.env.http_proxy,
    process.env.https_proxy,
  ].filter(Boolean);

  const traceSanitization = await sanitizeExactSecrets(runDir, secrets, ["workspace", "mirror-artifact.tar.gz"]);
  const residualTraceScan = await scanSensitiveFiles(runDir, secrets, ["workspace", "mirror-artifact.tar.gz"]);
  const lifecycle = await auditEventLifecycle(runDir);
  await writePrivateJson(path.join(runDir, "lifecycle-audit.json"), lifecycle);
  const archiveSecretScan = existsSync(archivePath)
    ? await auditTarArchiveSecrets(archivePath, secrets)
    : { status: "not_available", archives_scanned: [], sensitive_text_members: [], unsafe_binary_members: [], unsafe_archive_members: [] };

  const run = JSON.parse(await readFile(runPath, "utf8"));
  const artifactBinding = await verifyTraceArtifactBinding(runDir, run);
  const topLevelScanPassed = traceSanitization.unsafe_binary_files.length === 0
    && residualTraceScan.sensitive_text_files.length === 0
    && residualTraceScan.unsafe_binary_files.length === 0;
  run.trace_scope = {
    ...(run.trace_scope ?? {}),
    captured: [
      "SDK events delivered to the runner",
      "assistant content",
      "reasoning summaries",
      "commands and outputs",
      "file changes",
      "MCP calls/results",
      "plans",
      "errors",
      "usage",
    ],
    unavailable: "Hidden chain-of-thought not exposed by the SDK is not captured or reconstructed",
    sanitized: true,
  };
  run.trace_lifecycle = {
    path: "lifecycle-audit.json",
    status: lifecycle.status,
    event_count: lifecycle.event_count,
    dropped_event_count: lifecycle.dropped_event_count,
    orphan_started_item_count: lifecycle.orphan_started_items.length,
  };
  run.trace_integrity_status = lifecycle.status === "complete"
      && archiveSecretScan.status === "passed"
      && artifactBinding.status === "passed"
      && topLevelScanPassed
    ? "sealed"
    : "incomplete";
  run.shared_archive_secret_scan = archiveSecretScan;
  run.trace_secret_scan = {
    sanitization: traceSanitization,
    residual: residualTraceScan,
    status: topLevelScanPassed ? "passed" : "failed",
  };
  run.artifact_binding = artifactBinding;
  await writePrivateJson(runPath, run);

  const artifactNames = await topLevelArtifactFiles(runDir);
  const artifactFiles = Object.fromEntries(
    await Promise.all(artifactNames.map(async (name) => [name, await describeFile(path.join(runDir, name))])),
  );
  const proposalDirectory = path.join(workspace, "skill-proposal", "build-webharbor-mirrors");
  await writePrivateJson(path.join(runDir, "artifact-manifest.json"), {
    schema: "webharbor-codex-sdk-artifacts-v1",
    mirror_artifact: run.mirror_artifact ?? null,
    files: artifactFiles,
    proposal: existsSync(proposalDirectory) ? {
      path: "workspace/skill-proposal/build-webharbor-mirrors",
      sha256: await hashTree(proposalDirectory),
    } : null,
    lifecycle: run.trace_lifecycle,
    artifact_binding: artifactBinding,
    shared_archive_secret_scan: archiveSecretScan,
    secret_scan: run.trace_secret_scan,
  });

  const unsafe = archiveSecretScan.status !== "passed"
    || !topLevelScanPassed
    || lifecycle.status !== "complete"
    || artifactBinding.status !== "passed";
  console.log(JSON.stringify({
    run_directory: runDir,
    trace_integrity_status: run.trace_integrity_status,
    lifecycle: run.trace_lifecycle,
    shared_archive_secret_scan_status: archiveSecretScan.status,
    artifact_binding_status: artifactBinding.status,
    top_level_secret_scan: run.trace_secret_scan,
  }, null, 2));
  if (unsafe) process.exitCode = 1;
}

await main();
