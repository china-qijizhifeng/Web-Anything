# Standalone interaction verifier v1

This directory is the distributed pipeline's interaction-verification stage. It
normalizes an input Site Contract, inventories candidate Python/Jinja/HTML/JS and
an isolated Flask route map, triangulates source/contract/DOM/route evidence, and
runs bounded desktop browser workflows. It emits only machine-readable evidence:
the normalized contract, code inventory, interaction diff, interaction closure,
Builder repair package, and one terminal result.

Handoff boundary:

`Explorer/discovery package -> Site Contract -> Builder candidate -> existing runtime/visual verification -> this verifier -> repair package or release gate`

The candidate site, templates, static assets, seed DB, and runtime DB are inputs.
They are read-only. With an explicit launch configuration, the verifier stages
only that self-contained site into an isolated temporary workspace, owns and
reaps the process, and records pre/post candidate hashes. With `--base-url`, it
attaches to the caller-owned server and still fails if candidate hashes change.
No credentials, HOME, production Skill path, or Apple workspace is inferred; all
paths are explicit. Output is written only to the caller's `--evidence-dir`,
which must be outside the candidate site but may be a workspace sibling.

## Invocation

```bash
python3 scripts/terminal.py \
  --candidate-workspace /path/to/candidate \
  --site-path /path/to/candidate/sites/example \
  --site example \
  --contract /path/to/candidate/sites/example/interaction_contract.json \
  --base-url http://127.0.0.1:43100/ \
  --evidence-dir /tmp/example-interaction-evidence \
  --browser-path /path/to/chromium \
  --dependency-path /path/to/runtime/dependencies \
  --viewport-width 1440 --viewport-height 1000 \
  --max-pages 24 --max-depth 2 --timeout-ms 10000
```

Use `--launch-config` instead of `--base-url` for an owned local launch. The
launch JSON must explicitly contain `command`, `cwd`, and `base_url`; optional
string-only `env` values are applied without inheriting credentials. Generic
multi-site declarations can be validated with
`python3 scripts/pipeline_adapter.py --batch-spec <file>`. Each worker must have
an independent workspace, evidence directory, launch port, and explicit browser
and dependency paths.

### Exhaustive checkpointed execution

For a frozen candidate, `terminal.py` partitions the complete ordered route and
control inventory into deterministic stages. Immutable source/contract/static
analysis is written once to `shared-preflight`; delta workers evaluate only
their assigned `route:<id>` and `control:<id>` items. The final authoritative
merge emits the standard artifacts once. Delta checkpoints contain only identity
bindings, assigned IDs, bounded outcomes/failures, and shared-artifact hashes,
so their size grows with the batch delta rather than the full site:

```bash
python3 scripts/terminal.py ... \
  --candidate-freeze /run/candidate-freeze.json \
  --batch-size 32 --checkpoint-dir /run/checkpoints
```

Add `--resume` to reuse only atomic checkpoints whose plan identity, candidate
tree/frozen candidate identity, contract hash, analyzer implementation,
verifier configuration, viewport, and shared-artifact hashes still match.
Corrupt, stale, missing, duplicate, failed, or timed-out batches are never
accepted as exhaustive closure. A structural preflight failure (for example,
all active source controls lacking identity selectors) stops before launching
browser batches and emits the normal failed terminal result and repair package.
The terminal result includes an `exhaustive` block with the plan and
authoritative merge result; browser sampling or archetype evidence is not a
substitute for complete coverage.

## Scope and acceptance

Visible active-looking controls must have natural local outcomes; navigation,
queries, methods, menus, popups, downloads, state changes, SQLite assertions,
and reset evidence are checked. Unexpected upstream requests, dead/empty/404 or
defensive pages, generic fallbacks, and hard source/DOM/route triangulation gaps
fail closed. Failed closure always points to `builder-repair-package.json` for a
later Builder continuation. This component never edits the candidate.

Control triangulation uses a canonical semantic identity (source route, element
type, normalized accessible name, target/query, method, event, and provenance).
Literal anchors and forms receive escaped `identity_selector` values such as
`a[href="/fr/"]` and `form[action="/submit"]`; browser execution may separately
use a verifier-only `runtime_selector` audit attribute. Dynamic/Jinja targets
remain unresolved and fail closed.

The focused fixture and behavioral tests live under `tests/`.
`tests/run_forward_fixture.py` runs the gateway-prefixed fixture twice. No
packages or network access are needed when the pinned browser/dependency paths
are supplied by the caller.
