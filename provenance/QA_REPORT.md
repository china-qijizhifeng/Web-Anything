# WebAnything package QA — 2026-10-13

## Frozen inputs

- Upstream committed base: `ac48c8c62377e4724f2fc00c7c5f441aac614103`.
- Current source working-tree overlay: recorded with per-file SHA-256 in `source_snapshot.json`.
- Site inventory: 244 unique site IDs from the frozen 2026-10-09 audit; input-file SHA-256 values are embedded in `data/sites244.json`.

## Completed checks

| Check | Result |
| --- | --- |
| Dependency resolution with the lockfile, including `dev`, `browser`, `visual`, and `builder` extras | Passed offline on Python 3.12 |
| Playwright Chromium installation | Passed |
| Python and verifier tests | 203 passed, 7 skipped |
| Node trace tests | 44 passed |
| Frozen manifest structure | 244 records, unique IDs, safe relative paths and valid SHA-256 values |
| Historical artifact check against the existing WebHarbor store | 244/244 `app.py` files present and SHA-256 matched |
| `webanything doctor --json` | Passed; provider credentials absent in the test environment |
| Repository release scan | Passed; no machine-specific references or private run material detected |

The inventory check verifies selected application entrypoints, not complete site trees. No fresh website construction, model call, functional rerun of 244 sites, or release-gate pass was performed as part of this packaging. The original audit's HTTP and screenshot observations must not be interpreted as those results.

## Re-run

Use the commands in `README.md`; to check artifact hashes on another machine, supply a WebHarbor root containing the selected `sites/` and `generated/` trees to `scripts/verify_site_inventory.py`.
