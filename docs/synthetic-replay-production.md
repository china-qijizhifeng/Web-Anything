# Synthetic Replay Production Route

`benchmark_synthetic_replay` turns the successful full-capture pilot into a
reusable WebAnything route. It coexists with `mirror` and the lighter static
`benchmark_synthetic` mode.

```text
authorized capture
  -> privacy cleanup and frozen capture manifest
  -> replay contract + visual reference + SQLite seed + tasks
  -> isolated Builder
  -> candidate freeze
  -> functional/auth/reset/evaluator verification
  -> source-visible visual verification and bounded repair
  -> hash-bound release
  -> E2B deployment and public replay
```

## URL-first command

The normal entrypoint no longer requires a hand-authored replay package. The
capture compiler opens the seed URL in an isolated Playwright context, explores
a bounded same-origin route set, executes source JavaScript only inside that
temporary capture context, and freezes rendered DOM, shadow DOM, computed
layout, CSS, fonts, images, media, screenshots, and controls that are both
visible and browser hit-testable at one or more points. Visible controls fully
covered by another live layer are recorded as excluded diagnostics rather than
turned into impossible replay tasks; every retained action records its source
`hit_point`. It then
creates the SQLite seed, one replay task per captured control, the privacy
report, visual reference, replay spec, and compiled contract.

Capture and inspect a package without starting the SDK Builder:

```bash
webanything capture-replay https://www.example.test/ \
  --site example_replay \
  --output-dir runs/example-capture \
  --route /search \
  --route /items/featured \
  --max-pages 12 \
  --max-assets 800
```

Run capture and dispatch the Builder in one command:

```bash
webanything replay-build https://www.example.test/ \
  --site example_replay \
  --run-dir runs/example-replay-r1 \
  --route /search \
  --route /items/featured \
  --max-pages 12 \
  --max-assets 800
```

`--route` is optional and repeatable. Automatic discovery follows same-origin
links; explicit routes ensure important archetypes such as search, detail,
settings, or feeds enter the bounded scope before discovered links. If a route
exposes more controls than the current budget, acquisition fails closed and
asks for a larger `--max-actions-per-route` instead of silently dropping them.

For an authenticated capture, seal the user-authorized Playwright storage state
with `seal-auth-state`, then add `--auth-profile`, `--key-file`, and narrow
`--private-selector` values. Authentication is decrypted only in the capture
worker. Add `--authenticated-selector` for a stable signed-in-only element so an
expired session pauses as `auth_expired` instead of freezing the login page.
Source cookies, response headers, raw storage, and credential-bearing
URLs are never written to the package. The generated site always uses a local
demo account.

## Frozen inputs

The compiled `webanything-synthetic-replay-contract-v1` binds:

- a `webanything-replay-capture-manifest-v1` inventory of local DOM, CSS, fonts,
  images, icons, and media;
- a source-visible visual reference and route geometry;
- an immutable SQLite seed and its runtime path;
- fixed clock, local synthetic users, route selectors and action IDs;
- login, logout, reset, and stateful task/evaluator workflows;
- zero external runtime requests and no upstream JavaScript execution;
- proxy-prefix support and an explicit concurrency isolation model.

The authenticated capture worker alone may hold a temporary user-authorized
source session. It freezes no source cookie, token, password, or browser storage.
Builder receives only the frozen replay bundle and never accesses the live site.

`capture-replay` runs this freezer automatically. Advanced integrations that
already produce a sanitized directory can still call it directly:

```bash
webanything freeze-replay-capture \
  --bundle-root contracts/video \
  --capture-dir contracts/video/capture \
  --source-origin https://www.example.test \
  --routes contracts/video/capture-routes.json \
  --privacy-report contracts/video/privacy-report.json \
  --output contracts/video/capture/manifest.json
```

The freezer rejects source JavaScript, sensitive filenames, unsupported files,
cross-origin route mappings, and a missing/failed privacy report, then hashes
every allowed DOM/CSS/font/image/icon/media file.

## Candidate contract

The candidate is a Flask/SQLite application. It copies the contract, tasks, and
seed exactly; writes runtime state only under `instance/`; exposes a health
endpoint, POST reset endpoint, local login/logout, and one evaluator endpoint per
task; and records capture-derived files in `replay-provenance.json`.

All retained hit-testable controls carry stable `data-action-id` values. Their per-route
inventory is exact, so “looks clickable” means the control has an observable
local result and a replayable task path. Asset and navigation URLs must work at
both `/` and a deployment prefix such as `/p/40000/`.

Reset closes active connections and atomically replaces the runtime database
with the seed bytes. Two resets must produce the same SHA-256 as the seed.

## Concurrency

There are two honest deployment models:

- `isolated_instance`: one E2B/runtime per active task; `max_parallel_sessions`
  is exactly 1. Fifteen sites can still run concurrently in fifteen runtimes.
- `session_partitioned`: auth and mutable rows are keyed by an opaque browser
  session, allowing multiple tasks against one runtime without state leakage.

The verifier and release gate reject instance-global SQLite state that claims
multi-session concurrency.

## Verification and release

`verify-synthetic-replay` independently checks the contract/capture/seed copy,
routes, visible action inventory, local auth, task evaluators, reload persistence,
two byte-identical resets, zero external requests, HTTP/console errors, prefix
behavior, concurrency declaration, and candidate immutability.
With `--repair-package`, a repairable functional failure returns code `2` and
enters the same bounded Builder repair/refreeze loop as visual verification.
Frozen-authority and candidate-identity failures remain terminal.
If an interrupted SDK run left only the trace lifecycle incomplete while the
candidate remains independently hashable, `webanything repair --reset-trace`
archives the prior trace under `trace-history/` before a clean repair turn.

`verify-synthetic-visual` reuses the frozen source-visible comparison and can
emit the existing bounded repair package. `release-synthetic-replay` accepts only
current passing functional and visual evidence and binds contract, capture,
reference, seed, candidate, and gate hashes in the release manifest. Any Builder
or seed change invalidates the old evidence.
