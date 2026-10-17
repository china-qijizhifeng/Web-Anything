import { createHash } from "node:crypto";
import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

export function checkpointIdentity(fields) {
  return createHash("sha256").update(JSON.stringify(fields)).digest("hex");
}

export async function openCheckpoint(workspace, role, identity) {
  const sessionRoot = path.resolve(workspace, "..", ".session");
  await mkdir(sessionRoot, { recursive: true, mode: 0o700 });
  await chmod(sessionRoot, 0o700);
  const root = path.join(sessionRoot, `codex-${role}`);
  await mkdir(root, { recursive: true, mode: 0o700 });
  await chmod(root, 0o700);
  const metadataPath = path.join(root, "checkpoint.json");
  let saved = null;
  try {
    saved = JSON.parse(await readFile(metadataPath, "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const threadId = saved?.identity === identity && typeof saved.thread_id === "string"
    ? saved.thread_id : null;
  return {
    codexHome: root,
    threadId,
    output: threadId && typeof saved.output === "string" ? saved.output : null,
    async save(id, output) {
      if (!id) return;
      const temporary = `${metadataPath}.${process.pid}.tmp`;
      await writeFile(temporary, JSON.stringify({ schema_version: 1, identity,
        thread_id: id, output, updated_at: new Date().toISOString() }) + "\n", { mode: 0o600 });
      await rename(temporary, metadataPath);
    },
  };
}
