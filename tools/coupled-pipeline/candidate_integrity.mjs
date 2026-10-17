import { createHash } from "node:crypto";
import { readFile, readdir, readlink } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

// Preserve the historical controller ordering; all consumers use this module.
export async function treeHash(root) {
  const files = [];
  async function visit(dir) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const absolute = path.join(dir, entry.name);
      const relative = path.relative(root, absolute).split(path.sep).join("/");
      if (entry.isDirectory()) {
        if (entry.name !== "__pycache__") await visit(absolute);
      } else if (entry.isSymbolicLink()) {
        throw new Error(`Candidate symlink is not freezeable: ${relative} -> ${await readlink(absolute)}`);
      } else if (entry.isFile() && !/\.pyc$|-wal$|-shm$/.test(entry.name)) {
        files.push({ path: relative, sha256: createHash("sha256").update(await readFile(absolute)).digest("hex") });
      }
    }
  }
  await visit(root);
  files.sort((a, b) => a.path.localeCompare(b.path));
  const hash = createHash("sha256");
  for (const file of files) hash.update(`${file.path}\0${file.sha256}\n`);
  return { algorithm: "webanything-coupled-tree-v1", sha256: hash.digest("hex"), fileCount: files.length, files };
}

export function changedFiles(before, after) {
  const a = new Map(before.files.map(f => [f.path, f.sha256]));
  const b = new Map(after.files.map(f => [f.path, f.sha256]));
  return [...new Set([...a.keys(), ...b.keys()])].filter(p => a.get(p) !== b.get(p)).sort();
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  if (!process.argv[2]) throw new Error("usage: node candidate_integrity.mjs CANDIDATE");
  console.log(JSON.stringify(await treeHash(path.resolve(process.argv[2])), null, 2));
}
