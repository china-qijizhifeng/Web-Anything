# SDK interaction-closure loop

Use this contract when an SDK agent must repeatedly repair an existing mirror and
publish it to the shared E2B preview gallery without manual polling.

## Ownership

- The SDK repairs source-grounded mirror code and refreshes fidelity/quality proof.
- `audit-clickthrough` independently inventories visible desktop links, query
  variants, popup destinations, rendered resources, and runtime requests.
- `finalize-preview` owns the locked gallery transaction and E2B deployment.
- An SDK message, HTTP 200, `full_fidelity` grade, limitation page, departure
  notice, or trace archive is never a substitute for the deterministic gates.

## Forced loop

Run the orchestrator from the production repository. For a fast single-workflow
human-review handoff, add `--acceptance-profile reviewable-workflow`; the runner
will use the target's `reviewable_workflow.json` and publish only with the explicit
`sdk_reviewable_workflow` tier.

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh iterate-sdk-preview \
  --run-dir generated/codex-mirror-traces/<run> \
  --site <evidence-site-id> \
  --source-site <sites-directory-name> \
  --gallery-id <gallery-entry-id> \
  --preview-port <stable-e2b-port> \
  --name "<display name>" \
  --expected-text "<homepage identity text>" \
  --official-host <same-owner-host> \
  --replace-existing
```

The orchestrator replaces the isolated skill copy with the current production skill
before every audit/repair round. It owns a fresh no-reload local process,
so every SDK edit is followed by a real restart. It writes
`<run-dir>/auto-closure.json`, sends the exact audit failures to the next SDK turn,
uses `--skip-artifact --no-reflection`, and stops after the configured round limit
or two consecutive non-improving rounds.
The run directory is exclusively locked for the full audit/SDK/publish transaction;
starting a second worker for the same trace waits for the lock and cannot append a
second prompt or duplicate item events. SDK wrapper calls have a hard parent-side
deadline and are killed as a process group. Transport, timeout, and missing-turn
terminal failures receive bounded fresh-phase retries; semantic, dropped-event,
non-command-orphan, and recorder failures remain fail-closed. A restarted worker
loads only state carrying the same configuration fingerprint and resumes from the
last recorded round, so an unattended supervisor can safely reclaim an interrupted
worker.
Batch worker capture and repair subprocesses must remain in the worker's process
group. Do not nest `setsid` inside a supervisor-owned worker: a detached capture
can survive shutdown, retain the per-trace lease, and make the next retry appear
as `already_running`.
The public orchestrator reads only the required E2B settings from the process
environment or an explicitly supplied untracked file. A failed SDK round
stops the loop and remains recorded in `auto-closure.json`. One narrow exception
exists: if the wrapper returns nonzero solely because completed command tools lack
SDK terminal events, `sdk_trace_gate.py` must independently prove a normal closed
turn, contiguous/no-drop trace projections, command-only orphans, a present target
app, and no other error. Continue with `repairing_with_trace_warning`, retain the
incomplete lifecycle evidence, and rerun every mirror acceptance gate. Do not
generalize this exception to timeouts, failed turns, dropped events, non-command
orphans, recorder failures, or evidence failures.
The current deployment record supplies the default sandbox and gateway. If E2B
explicitly reports that sandbox as missing, publication creates a replacement on
the same E2B platform, accepts it only after all public checks pass, and records
its returned sandbox ID and gateway as the next run's authority.

When a user is actively reviewing an already deployed gallery snapshot, add
`--no-publish`. The SDK still receives deterministic audit failures and iterates
normally, but a passing workspace stops at `automation_verified` with
`publication_skipped: true` and `human_review_required: false`; it must not edit
the shared manifest, upload a new package, restart the E2B supervisor, or replace
the public preview. This is a complete deterministic acceptance result for the
declared scope, not a request for manual review.

For a previously narrow or reviewable candidate, add `--minimum-sdk-rounds 1` so
old passing evidence cannot short-circuit the new autonomous full-site brief. The
runner fingerprints its configuration and persists successful SDK-round progress,
so a supervisor restart does not force redundant rounds. Trace-invalid or failed
SDK turns do not satisfy the minimum.

Use `--extra-requirements <path>` to append bounded operator priorities to every
generated repair brief. The deterministic audit remains authoritative; this file
only focuses the SDK on known visual, route, data, or interaction gaps and cannot
relax any acceptance gate.

For an unattended multi-site batch, do not launch one-shot workers from a shell
background job. Use a persisted worker specification with the batch supervisor
entry point. It owns one process group per site, writes heartbeat and terminal
state atomically, reclaims workers that exit before a terminal state, and emits a
batch summary. It may retry a site only within its declared attempt budget; a
terminal `exhausted` row may be declared in `retryable_terminal_states` for a
bounded fresh worker attempt when the worker left a valid trace/workspace behind;
the default is only `exhausted`. Policy-blocked, evidence-failed, dropped-event,
and other terminal rows remain fail-closed and are retained with their evidence.
An exhausted row still never bypasses any mirror or publication gate. A worker may
publish only through `finalize-preview`, whose manifest lock and public E2B checks
remain the final acceptance gate.

## Acceptance

Require all of the following in the same workspace state:

1. Current `webharbor-fidelity-v19` evidence passes its transitive source hashes.
2. Current quality evidence passes and grades `full_fidelity` or
   `synthetic_grounded_data` with honest disclosure.
   Both evidence files must carry the deterministic fingerprint of the current
   site code, templates, shipped assets, and seed bytes.
3. Canonical local `/` is a meaningful rendered page.
4. Meaningful visible local click-through reaches the requested threshold.
5. No audited route has broken images, unloaded stylesheets, failed/HTTP-error
   resources, or public runtime requests.
6. E2B public health, gallery index, and every proxied homepage return 200; every
   homepage contains its required identity text.
7. The deployment record names the requested sandbox and exact configured gallery
   host; a different E2B sandbox or gateway is a publication failure.

The page inventory is bounded but fail-closed. If `--max-pages` is reached while
more visible local destinations remain, the audit writes those routes as
`unaudited_limit`, fails acceptance, and sends them to the next repair round. Raise
the bound for a legitimately larger site; never accept a truncated inventory.

Keep `coverage.json` for the site's declared full-site taxonomy. The independent
loop writes `clickthrough-coverage.json`; never overwrite either file by hand.

## SDK command lifecycle

Do not ask the SDK to own persistent runtime processes. In repair prompts and
execution, prohibit foreground `app.run(...)`, `flask run`, `site_runner.py`, and
unbounded browser/audit commands. Use `run-owned-site-check` for temporary runtime
checks; it owns the process group, waits for the exact `/_health` site identity,
enforces a hard timeout, and terminates/reaps the server in all exit paths. The
parent closure loop remains the only owner of the audit server between rounds.

## Repair order

1. Repair canonical `/`, blank/error routes, broken resources, and unexpected
   public runtime dependencies.
2. Re-capture and compile missing same-origin GET pages with normal source checks.
3. Promote same-owner official supplemental hosts only after independent robots,
   source, namespace, and workflow evidence.
4. Preserve path and query identities, `target=_blank` behavior, return paths,
   documentation sidebars, listing/detail catalogs, and stateful SQLite workflows.
5. Leave denied/authenticated/live-backend targets honestly unsupported. Do not
   count their limitation, protocol, or departure pages as meaningful behavior.

## Publication transaction

`finalize-preview` locks the shared manifest, deploys a candidate manifest, and
commits the formal manifest only after public verification. When updating the
existing sandbox fails, it redeploys the prior formal manifest before returning a
failure. Concurrent SDK runs therefore serialize publication and cannot overwrite
one another's entries. A failed trace seal/archive does not block a proven mirror;
a failed E2B public check always blocks publication.
