# Architecture

WebAnything treats website-environment construction as an evidence-producing
state machine rather than a one-shot code generation call.

```text
configured
  -> discovering
  -> contracted
  -> building
  -> verifying
  -> repair_required -> building
  -> accepted
  -> releasing
  -> deployed
  -> publicly_verified
```

Every stage owns a separate directory and terminal JSON artifact. A later stage
may consume an earlier artifact but must not silently rewrite it.

## Roles

### Explorer

Performs bounded, read-only exploration and records pages, routes, controls,
workflows, screenshots, state profiles, resource outcomes, and limitations. An
authenticated session is private input and is never part of the public package.

### Contract compiler

Normalizes discovery into a Site Contract. Live snapshot assertions and legacy
seed regressions remain separate so upstream content drift does not create false
failures.

### Builder

Runs in an isolated copied workspace and is the only role allowed to edit the
candidate. It implements local routes, assets, deterministic SQLite seed/runtime
state, reset, and declared workflows without upstream side effects.

For `benchmark_synthetic`, a separate Builder Skill consumes a frozen Synthetic
Contract and, when configured, a source-visible visual reference. It never
receives authentication state, raw DOM, credentials, or live-source access, but
the frozen screenshot can contain content visible to the authorized capture
account. It
implements a static candidate with fixed fixtures, localStorage, a fixed UTC
clock, stable IDs, and a canonical reset API.
`content_policy.mode=fictional` preserves synthetic branding and fixtures;
`source_fidelity` permits complete visible-content reconstruction from the
frozen screenshot while keeping runtime state local, deterministic, and offline.

### Verifier

Runs runtime, visual, interaction, state-transition, privacy, and immutability
checks. The interaction stage triangulates declared controls, source code,
rendered browser controls, and actual Flask routes.

The synthetic Verifier is a separate implementation. Its functional stage uses
fresh browser contexts and directly checks DOM, logical URL, raw localStorage,
external requests, reset determinism, reload persistence, content provenance,
task state diffs, and candidate immutability. Its visual stage compares an
internal content-neutral derivative and text-free geometry using a frozen
reference. Neither stage trusts
page-reported success.

### Repair loop

Verifier failures are serialized into `builder-repair-package.json`. A bounded
SDK continuation receives that package in the original isolated workspace. The
Verifier is rerun after every continuation.

### Release and deployment

The Builder emits a candidate freeze record. Verification evidence is bound to
that identity; interaction closure may be partitioned into atomic, resumable
checkpoints whose merge must cover every route/control ID. A read-only release
gate then aggregates current Fidelity, Quality, exhaustive Interaction,
resource/privacy, candidate-immutability, and runtime-reset evidence. It emits one authoritative gate
result and a v2 manifest only when all gates pass. Deployment consumes the
manifest's candidate and identity binding; it cannot trust a filename or bare
status field. Public verification remains a separate external observation.

## Concurrency

Sites may build in parallel because each has a distinct run directory,
workspace, database, port, log, and deployment identity. The synthetic
production supervisor uses independent capture, Builder, and Verifier pools.
Within one site, a filesystem lease permits only one active stage worker, and a
checkpoint plus stage result allows timeout recovery without duplicate writers.
Verifiers remain read-only.
