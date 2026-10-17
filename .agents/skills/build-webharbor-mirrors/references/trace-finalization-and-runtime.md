# Trace Finalization and Runtime Ownership

Use this reference when a Codex SDK continuation repairs an existing mirror
or emits a terminal evidence chain.

## Own the process before mutating state

Do not replace `instance/<site>.db` underneath a long-lived Flask or SQLAlchemy
process. The process may keep a pooled connection to the old inode: reads can
continue to pass while a later POST fails or writes to stale state. For a reset
sensitive proof:

1. Start an explicitly trace-owned server from the canonical seed.
2. Run the browser state-changing workflow and assert the SQLite mutation.
3. Stop that exact process, restore the seed bytes, and verify the seed hash.
4. Restart the same owned build and prove HTTP, SQLite integrity, and reset
   identity once more.
5. Record the owner PID/command, bind address, port, and every restart in the
   runtime evidence.

If the requested port is occupied by a process that cannot be safely restarted,
use a temporary trace-owned port for mutation proof and disclose it. Never mix a
temporary port into final fidelity, quality, coverage, resource, or result
evidence. A stable final port must be started from the current build after all
file and seed changes.

Never start `app.run(...)`, `flask run`, `site_runner.py`, or another persistent
server as a foreground SDK tool call. Never run an unbounded large browser audit
as a foreground SDK tool call either. Delegate both server ownership and the
bounded check to the parent orchestrator:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh run-owned-site-check \
  --workspace "$PWD" \
  --source-site <site> \
  --timeout-seconds 900 \
  -- <check-command> --base-url '{base_url}'
```

The helper starts a new process group, verifies `/_health` identity, applies a
hard timeout, and always terminates and reaps the server group. For a check that
spawns children, keep the check bounded as well and clean its own child process
group in `finally`. The SDK must return only after every foreground command has a
terminal result.

Keep trace integrity separate from mirror acceptance. Do not fabricate missing
SDK item terminal events and do not mark an incomplete lifecycle complete. A
parent may continue the mirror closure loop after a nonzero SDK wrapper exit only
when `sdk_trace_gate.py` proves all of the following: the requested turn has one
normal `turn.completed`, the target `app.py` exists, events are contiguous with no
drops or projection mismatch, the only lifecycle failure is orphaned
`command_execution` items, and no other execution error exists. Record this as a
warning in `auto-closure.json`; never relax fidelity, quality, coverage, resource,
reset, or E2B publication gates.

## Generate evidence in dependency order

Treat evidence as a DAG, not a collection of independent status files:

```text
capture -> reconstruction/source reuse -> mirror contract -> seed/audit
        -> fidelity -> quality -> docs/coverage -> resource/runtime
        -> package -> automation/result
```

Regenerate downstream files after any input hash changes. Finalizers must reject
stale references transitively across source capture, replay bundle, app/contract,
seed, fidelity, quality, coverage, and runtime evidence. Derive the terminal
quality grade, completion level, counts, click-through metrics, and port from the
latest passing artifacts; never copy an earlier narrative forward.

Keep `full_fidelity` scoped to the declared route/data/workflow contract. Report
`major_surface_mirror` or another coverage level independently when sitemap,
official supplemental surfaces, or source-denied routes prevent a full public
surface claim.

## Catalog and browser proofs

When an official captured script supplies a listing/detail catalog, verify every
record field and source order against SQLite. Prove every index, detail, and
apply GET route, unknown-ID 404 behavior, and representative shortest/longest
rendered bodies. A boolean row-count check is insufficient.

For a full-site trace, resource/performance evidence should be deterministic and
route-configured. Record status, final path/query, DOMContentLoaded/load timing,
request count, decoded bytes, failed/HTTP-error resources, unexpected external
requests, console errors, and meaningful body text. Fail closed on an empty page,
resource error, or console error; do not turn a diagnostic-only timing script
into acceptance evidence without these fields.
