#!/usr/bin/env node

import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { lstat, readFile, readdir, readlink } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import {
  describeFile,
  diffInventories,
  inventoryTree,
  reconstructBaselineInventory,
  writePrivateJson,
} from "./lib.mjs";

function usage() {
  return "usage: recover-baseline.mjs RUN_DIR --baseline-root DIR --scope PATH [--scope PATH ...]\n";
}

function parseArgs(argv) {
  const args = { scopes: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    const next = () => {
      index += 1;
      if (index >= argv.length) throw new Error(`${value} requires a value`);
      return argv[index];
    };
    if (value === "--baseline-root") args.baselineRoot = next();
    else if (value === "--scope") args.scopes.push(next());
    else if (value === "--help" || value === "-h") args.help = true;
    else if (value.startsWith("-")) throw new Error(`unknown option: ${value}`);
    else if (!args.runDir) args.runDir = value;
    else throw new Error(`unexpected positional argument: ${value}`);
  }
  return args;
}

function normalizeScope(value) {
  const normalized = path.posix.normalize(String(value).replaceAll(path.sep, "/")).replace(/^\.\//, "");
  if (!normalized || normalized === "." || normalized === ".." || normalized.startsWith("../") || path.posix.isAbsolute(normalized)) {
    throw new Error(`unsafe or unbounded recovery scope: ${value}`);
  }
  if (normalized === ".git" || normalized.startsWith(".git/") || normalized === "skill-proposal" || normalized.startsWith("skill-proposal/")) {
    throw new Error(`reserved recovery scope: ${value}`);
  }
  return normalized.replace(/\/$/, "");
}

function inScopes(filePath, scopes) {
  return scopes.some((scope) => filePath === scope || filePath.startsWith(`${scope}/`));
}

async function inventorySelected(root, scopes) {
  const result = {};
  async function visit(relativePath) {
    const absolutePath = path.join(root, ...relativePath.split("/"));
    let stat;
    try {
      stat = await lstat(absolutePath);
    } catch (error) {
      if (error.code === "ENOENT") return;
      throw error;
    }
    if (stat.isDirectory()) {
      const entries = await readdir(absolutePath, { withFileTypes: true });
      entries.sort((left, right) => left.name.localeCompare(right.name));
      for (const entry of entries) await visit(path.posix.join(relativePath, entry.name));
      return;
    }
    if (stat.isSymbolicLink()) {
      const target = await readlink(absolutePath);
      result[relativePath] = {
        kind: "symlink",
        target,
        sha256: createHash("sha256").update(`symlink:${target}`).digest("hex"),
      };
      return;
    }
    if (stat.isFile()) {
      result[relativePath] = {
        kind: "file",
        ...await describeFile(absolutePath),
        mode: stat.mode & 0o777,
      };
    }
  }
  for (const scope of scopes) await visit(scope);
  return result;
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
  if (!args.runDir || !args.baselineRoot || args.scopes.length === 0) {
    console.error(usage());
    process.exitCode = 64;
    return;
  }

  const runDir = path.resolve(args.runDir);
  const workspace = path.join(runDir, "workspace");
  const baselineRoot = path.resolve(args.baselineRoot);
  const scopes = [...new Set(args.scopes.map(normalizeScope))].sort();
  if (!existsSync(workspace) || !existsSync(path.join(runDir, "run.json")) || !existsSync(baselineRoot)) {
    throw new Error("run workspace, run.json, or baseline root is unavailable");
  }

  const finalInventory = await inventoryTree(workspace, [".git", "skill-proposal"]);
  const selectedBaseline = await inventorySelected(baselineRoot, scopes);
  const baseline = structuredClone(finalInventory);
  for (const filePath of Object.keys(finalInventory)) {
    if (inScopes(filePath, scopes)) delete baseline[filePath];
  }
  Object.assign(baseline, selectedBaseline);
  const changes = diffInventories(baseline, finalInventory);
  if (changes.length === 0) throw new Error("declared recovery scopes contain no changes");
  if (changes.some((entry) => !inScopes(entry.path, scopes))) {
    throw new Error("recovered changes escaped the declared scopes");
  }
  const reconstructed = reconstructBaselineInventory(finalInventory, changes);
  if (Object.keys(reconstructed).length !== Object.keys(baseline).length
      || diffInventories(reconstructed, baseline).length !== 0) {
    throw new Error("recovered change set does not reconstruct the selected baseline");
  }

  const runPath = path.join(runDir, "run.json");
  const run = JSON.parse(await readFile(runPath, "utf8"));
  await writePrivateJson(path.join(runDir, "changed-files.json"), changes);
  run.changed_files = changes.length;
  run.mirror_artifact = null;
  run.workspace_secret_scan = null;
  run.finalization_recovery = {
    schema: "webharbor-codex-sdk-baseline-recovery-v1",
    generated_at: new Date().toISOString(),
    baseline_root: baselineRoot,
    scopes,
    recovered_change_count: changes.length,
    previous_fatal_error: run.fatal_error ?? null,
    status: "staged_for_continuation",
  };
  run.fatal_error = "Initial artifact finalization failed; a bounded baseline was recovered and requires continuation finalization.";
  await writePrivateJson(runPath, run);
  console.log(JSON.stringify(run.finalization_recovery, null, 2));
}

await main();
