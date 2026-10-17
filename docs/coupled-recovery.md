# Coupled pipeline checkpoint recovery

The Explorer and Builder remain coupled. Recovery does not install upstream
applications, replace source URLs, edit candidates outside Builder, or relax gates.

## Handoff

Builder writes `build-result.json` and `build-notes.md` inside its workspace,
under the current output directory's basename. The SDK controller copies those
reports into the run-level SDK output. The independent Verifier is a subsequent
controller stage, not a prerequisite for Builder's local `passed` handoff.

For an explicitly inspected historical report at a nonstandard path, set
`WEBANYTHING_BUILDER_REPORT` to its absolute `build-result.json` path. It must be
inside this run's workspace and contain the matching `site_id` and local
`status: passed`. This override is not an acceptance decision. Do not scan for
arbitrary old passing reports or replace a failed latest candidate with stale evidence.

## Reset, freeze, verify

Before invoking the independent Verifier, the controller imports the candidate's
Flask `app.py` in a short-lived subprocess and calls its local reset API through
Flask's test client. Supported routes are `/_reset`, `/api/reset`, `/reset`, and
`/reset/`. No upstream request or source login is involved. Two consecutive resets
must have identical file bytes, immutable SQLite seeds must remain unchanged,
and every runtime SQLite database must match an immutable seed. Only runtime
database changes are allowed during this admission phase. Failure produces a
repair package and returns to Builder without spending a Verifier model turn.

The controller then freezes the entire candidate, including runtime databases.
Controller and Verifier use `tools/coupled-pipeline/candidate_integrity.mjs`,
algorithm `webanything-coupled-tree-v1`. Do not substitute a different tree hash
algorithm or sorting order. The per-file freeze manifest and `candidate-diff.json`
identify changes precisely. Verification must finish with a final reset and an
unchanged frozen tree; a hash mismatch still stops the pipeline.

## Bounded resumption

Restart with the same run directory after checking that no previous worker or
candidate writer is active; the per-run `.auto-supervisor.lock` also rejects a
concurrent supervisor. Preserve prior outputs. The saved supervisor state now
restores the pending stage: a Builder retry or Verifier repair returns to Builder,
while an interrupted Verifier reuses its output directory. A timed-out Builder
report is never treated as a completed pass. The retry-limit status keeps the
last pending stage so the same run can resume with a larger cycle budget.
`WEBANYTHING_FORCE_BUILDER_ON_START=1` remains an explicit override for inspected
historical runs without usable supervisor state.

Keep retries finite. An authorization/challenge pause is not a Builder repair.
`blocked_source` is normalized to `source_blocked`; unknown blocked output stops
for diagnosis. Neither a local Builder pass nor a retry limit extension proves
release: require a fresh independent pass, exact candidate identity, and the
applicable release gate.

Tests: `python -m pytest tests/test_coupled_recovery.py tests/test_coupled_pipeline.py tests/test_batch_supervisor.py -q`.
