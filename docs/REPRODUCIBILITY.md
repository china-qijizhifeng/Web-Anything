# Reproducibility guide

## 1. Source snapshot

`provenance/source_snapshot.json` records the upstream commit and working-tree files copied into this repository. The snapshot is intentionally a new repository: it does not modify the source checkout. The installable distribution, Python package, and CLI are now named `webanything`. Historical artifact paths and upstream names in the provenance records remain unchanged so they continue to identify their original sources.

The reference implementation has these stages:

1. Discover browser-observable structure, resources, routes, controls, and interactions from a permitted URL.
2. Compile a site contract grounded in the captured evidence.
3. Build a candidate site in an isolated workspace.
4. Freeze candidate identity and invoke an independent Verifier.
5. Return a machine-readable repair package to the Builder on failure; release only when the applicable gate passes.

The reference source-coupled route is in `tools/coupled-pipeline/`. The documented decoupled route is in `src/webanything/`, `tools/codex-mirror-trace/`, and `pipeline-tools/interaction-verifier-v1/`. Existing code also supports synthetic and replay modes. See `docs/WEBANYTHING_UPSTREAM.md` and `docs/coupled-pipeline.md` for their command details.

## 2. Frozen 244-site manifest

`data/sites244.json` was generated from the 2026-10-09 `evidence.json` and `site-table.csv` in the original local audit. The manifest stores each selected site's ID, source URL, category, path relative to the WebHarbor artifact root, `app.py` SHA-256, provenance field, homepage observation, and diagnostic depth. The SHA-256 of each input file is recorded in the manifest.

Regenerate it if those two frozen inputs are available:

```bash
python scripts/import_site_inventory.py \
  --evidence /path/to/evidence.json \
  --table /path/to/site-table.csv \
  --source-root /path/to/WebHarbor \
  --output data/sites244.json
python scripts/verify_site_inventory.py
```

To audit an artifact store on another machine, copy or mount the selected `sites/` and `generated/` directories under one root, then run `python scripts/verify_site_inventory.py --webharbor-root /path/to/WebHarbor`. The command fails on missing `app.py`, unsafe paths, duplicate IDs, invalid hashes, or a SHA mismatch. It does not execute websites or certify interactions.

Historical selected artifacts are distributed across batches: 152 under `sites/` and 92 under `generated/`. The manifest does not contain the large website trees or third-party media. The original 2026-10-09 audit recorded HTTP 200 for all 244 selected entrypoints; its screenshot run reported 237 normal startup observations and 7 exceptions. Only 113 sites received a delayed-rendering review; 131 did not.

## 3. Build and verify a new site

After `uv sync`, Playwright installation, and `npm ci` as in the README, `uv run webanything doctor --json` checks local dependencies. A real build requires a permitted source site and a configured provider. Keep provider credentials in the process environment or a secret manager, outside run specs and this repository.

The current provider variable names are documented in `docs/getting-started.md`. Existing frozen run directories are historical records and are not migrated in place; use the revision that created a run if it needs to be resumed.

The minimal reference command is:

```bash
uv run webanything build https://example.com/ \
  --site example \
  --run-dir runs/example-r1 \
  --profile task-driven
```

The source-coupled path is documented in `docs/coupled-pipeline.md`. Reproducing a fresh build byte-for-byte is generally impossible when the source website or model output changes. Preserve the URL, capture package, contract, model identifier, run configuration, candidate hash, Verifier report, and release manifest to make a particular run auditable. A passing `doctor`, HTTP response, screenshot, or Builder completion is not a release result.

## 4. Local checks and CI

`pytest` checks the Python code and verifier protocol; the Node package has its own tests. `scripts/verify_site_inventory.py` checks the bundled manifest offline. The CI workflow runs the same checks and a CLI `doctor`/`release-scan` smoke test. CI intentionally does not call live websites, use provider credentials, or assert that all 244 historical sites pass current functional gates.

## 5. Distribution

WebAnything-authored code is released under the root MIT License. Website media, source-site content, private browser state, model credentials, run traces, candidate databases, and other third-party material must still be reviewed separately before publication or redistribution.
