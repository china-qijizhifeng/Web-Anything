"""Reset through a local Flask API before freezing; never copy or edit site files."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys


def inventory(root):
    result = {}
    for p in root.rglob("*"):
        if p.is_symlink():
            raise ValueError(f"candidate contains symlink: {p.relative_to(root)}")
        if not p.is_file() or "__pycache__" in p.parts or p.name.endswith((".pyc", "-wal", "-shm")):
            continue
        result[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def sqlite_paths(root, entries):
    result = set()
    for name in entries:
        if Path(name).suffix not in {".db", ".sqlite", ".sqlite3"}:
            continue
        with (root / name).open("rb") as f:
            if f.read(16) == b"SQLite format 3\x00":
                result.add(name)
    return result


def is_seed(name):
    p = Path(name)
    return "instance_seed" in p.parts or p.stem == "seed" or p.stem.endswith("_seed") or p.stem.startswith("seed_")


def reset_candidate(root):
    before = inventory(root)
    seeds = {p: before[p] for p in sqlite_paths(root, before) if is_seed(p)}
    if not seeds:
        raise ValueError("No immutable SQLite seed found; Builder must supply a standard seed database")
    previous = Path.cwd()
    try:
        os.chdir(root)
        sys.path.insert(0, str(root))
        spec = importlib.util.spec_from_file_location("_webanything_preflight_app", root / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        app = getattr(module, "app", None)
        if app is None or not hasattr(app, "test_client"):
            raise ValueError("app.py must expose a Flask app for local API preflight")
        rules = {r.rule: r for r in app.url_map.iter_rules()}
        route = next((p for p in ["/_reset", "/api/reset", "/reset", "/reset/"] if p in rules), None)
        if route is None:
            raise ValueError("No supported local reset route; Builder must expose /_reset or /api/reset")
        method = "POST" if "POST" in rules[route].methods else "GET"
        client = app.test_client()
        response_codes = []
        snapshots = []
        for _ in range(2):
            response = client.open(route, method=method, follow_redirects=True)
            response_codes.append(response.status_code)
            if response.status_code != 200:
                raise ValueError(f"Reset API returned HTTP {response.status_code}")
            snapshots.append(inventory(root))
            if any(snapshots[-1].get(p) != sha for p, sha in seeds.items()):
                raise ValueError("Reset modified an immutable seed")
        after = snapshots[-1]
        if snapshots[0] != after:
            raise ValueError("Two consecutive resets do not produce byte-identical candidate files")
        if any(after.get(p) != sha for p, sha in seeds.items()):
            raise ValueError("Reset modified an immutable seed")
        runtime = sqlite_paths(root, after) - set(seeds)
        if not runtime or any(after[p] not in seeds.values() for p in runtime):
            raise ValueError("Runtime SQLite bytes do not match immutable seed bytes after reset")
        changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
        if set(changed) - runtime:
            raise ValueError(f"Reset changed non-runtime files: {sorted(set(changed) - runtime)}")
        return {"status": "passed", "reset_route": route, "method": method,
                "response_codes": response_codes, "byte_identical_twice": True,
                "seed_hashes": seeds, "runtime_hashes": {p: after[p] for p in sorted(runtime)},
                "changed_runtime_files": changed}
    finally:
        os.chdir(previous)


def main():
    root, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if output.is_relative_to(root):
        raise ValueError("Preflight evidence must be outside the candidate")
    try:
        # Candidate startup output is not a diagnostic channel for credentials.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            report = reset_candidate(root)
    except Exception as exc:
        report = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
