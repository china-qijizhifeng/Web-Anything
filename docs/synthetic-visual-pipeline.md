# Synthetic Visual Pipeline

This production route adds structural visual fidelity to
`benchmark_synthetic` without exposing authentication state or live-source
access to the Builder and without changing the existing `mirror` route. The
frozen screenshot itself is source-visible, so viewport content is not private
from the Builder in this mode.

```text
auth_required -> capture -> sanitize -> reference_frozen -> build -> freeze
  -> functional_verify -> visual_verify -> release -> [deploy -> deployed | released]
                                      | failed
                                      v
                         repair -> refreeze -> functional_verify
```

Capture failures enter `auth_expired`, `source_blocked`, `capture_failed`, or
`sanitize_failed`. Exhausted Builder or Verifier restarts enter
`builder_timeout` or `verifier_timeout`. A candidate that still misses the
visual gate after the configured repair limit enters `visual_review_required`.
No capture failure is converted into Builder work.

## Security boundary

- Authorization is reusable only while the source session remains valid.
- Store one encrypted auth profile per site and account, outside the repository.
- Keep its AES-256-GCM key in a separate mode-0600 file. Do not place the key,
  profile, cookies, or storage state in environment variables, traces, E2B
  artifacts, candidate workspaces, or release packages.
- The Capture Worker decrypts storage state in memory. It never persists raw
  DOM, cookies, local browser storage, or credentials. With
  `content_strategy=source_visible`, it does persist the viewport screenshot in
  the frozen non-release reference. Therefore a dedicated low-risk capture
  account and explicit user authorization are required whenever private page
  content may be visible.
- E2B capture may receive the encrypted profile and key as ephemeral runtime
  files. Delete those files with the sandbox after capture.
- CAPTCHA, risk control, expired sessions, and source/robots denial require a
  new user authorization or source decision; the worker never bypasses them.

The Builder receives a copy of the frozen reference under
`.webanything/synthetic-input/visual-reference/`: a source-visible screenshot for
layout guidance, a separate content-neutral comparison image, text-free element
rectangles, aggregate layout metrics, thresholds, and artifact hashes.

`content_policy.mode=fictional` keeps source text, identities, and media out of
the candidate and uses the content-neutral comparison. With
`content_policy.mode=source_fidelity`, the Builder reproduces the visible brand,
text, media, chrome, colors, and layout from the frozen screenshot using local
files and semantic DOM; bounded media may be derived from that frozen artifact.
The whole screenshot may not be served as a page overlay, and live source access
remains forbidden. Source-derived files are declared in
`content-provenance.json`. The frozen reference bundle remains outside the
release package in both modes.

For `source_fidelity`, the hard visual gate is a high direct screenshot score
plus a screenshot-dominant composite score. DOM geometry, density, columns, and
spacing still contribute diagnostic signal to that composite, but are not
separate hard gates: a deterministic implementation does not need to reproduce
an upstream framework's private custom-element tree to reproduce its rendered
surface.

## Seal a user-authorized session

Create the Playwright storage-state file through a user-controlled login, then
seal it immediately:

```bash
umask 077
openssl rand -hex 32 > /secure/webanything/cloudpost.key
chmod 600 /secure/webanything/cloudpost.key
webanything seal-auth-state \
  --storage-state /secure/webanything/cloudpost.storage-state.json \
  --output /secure/webanything/cloudpost.auth-profile.enc.json \
  --key-file /secure/webanything/cloudpost.key \
  --site cloudpost \
  --account benchmark-reviewer \
  --allowed-origin https://source.example \
  --delete-plaintext
```

The seal command validates the profile and origin binding before deleting the
plaintext. Reauthorization replaces the encrypted profile; the supervisor only
resumes `auth_expired` when that file's hash changes.

## Capture and bind the reference

Create a capture spec that declares a fixed viewport and maps each source URL to
one synthetic route. Its schema is
`schemas/synthetic-visual-capture-spec-v1.schema.json`.

```bash
webanything capture-synthetic-reference \
  --spec contracts/cloudpost/visual-capture-spec.json \
  --auth-profile /secure/webanything/cloudpost.auth-profile.enc.json \
  --key-file /secure/webanything/cloudpost.key \
  --output-dir contracts/cloudpost/visual-reference
```

The command exits with a stable capture classification: `20` for
`auth_expired`, `21` for `source_blocked`, `22` for `capture_failed`, and `23`
for `sanitize_failed`. A successful manifest records
`raw_material_persisted=true`, `content_strategy=source_visible`, and binds both
the visible and comparison PNGs plus geometry artifacts by
SHA-256. Compile the Synthetic Contract only after its `visual_reference`
points to this manifest and its self hash.

Before anything is frozen, the capture quality gate rejects extraction
truncation, page-covering classified rectangles, saturated horizontal columns,
missing landmarks, abnormal same-category overlap, and insufficient usable
regions. Custom elements default to containers; only native media elements,
explicit image semantics, and URL-backed image surfaces are classified as
media. Coverage and column occupancy use rectangle unions, so nested elements
cannot inflate either metric above `1.0`.

The reference is frozen once. Repair rounds always compare against the same
reference hash and never recapture the live source.

The Builder input manifest intentionally records `raw_material_persisted=true`:
the frozen source-visible PNG exists in the isolated non-release input bundle.
The release manifest records `false` only after proving that the frozen reference
bundle itself is absent from the candidate and release artifacts. Source-derived
local content is governed separately by `content_policy.mode` and provenance.

## Verify and release

After Builder output, freeze the candidate with the contract and visual
reference bound to its identity. Run both independent verifiers:

```bash
webanything verify-synthetic \
  --site-path runs/cloudpost/sites/cloudpost \
  --contract contracts/cloudpost/synthetic_contract.json \
  --candidate-freeze runs/cloudpost/candidate-freeze.json \
  --output runs/cloudpost/verification/synthetic-functional.json

webanything verify-synthetic-visual \
  --site-path runs/cloudpost/sites/cloudpost \
  --contract contracts/cloudpost/synthetic_contract.json \
  --visual-reference contracts/cloudpost/visual-reference/synthetic_visual_reference.json \
  --candidate-freeze runs/cloudpost/candidate-freeze.json \
  --output runs/cloudpost/verification/synthetic-visual.json \
  --repair-package runs/cloudpost/verification/visual-repair-package.json
```

The visual score combines content-neutral screenshot similarity, DOM geometry, component
density, horizontal column proportions, and spacing. It also rejects external
network requests and candidate mutation. Its repair package contains route
scores, category density deltas, and bounded mismatch rectangles. Repair
packages never include source text, identities, or media.

Release requires both passing evidence files:

```bash
webanything release-synthetic \
  --site cloudpost \
  --site-path runs/cloudpost/sites/cloudpost \
  --contract contracts/cloudpost/synthetic_contract.json \
  --verification runs/cloudpost/verification/synthetic-functional.json \
  --visual-reference contracts/cloudpost/visual-reference/synthetic_visual_reference.json \
  --visual-verification runs/cloudpost/verification/synthetic-visual.json \
  --candidate-freeze runs/cloudpost/candidate-freeze.json \
  --output-dir runs/cloudpost/release
```

The fail-closed release gate binds the compiled contract, frozen visual
reference manifest plus self hash, and candidate tree plus identity. A repair
changes the candidate hashes, invalidates both old verifier results, and must be
followed by refreeze and full re-verification.

## Unattended concurrent operation

`webanything synthetic-pipeline --spec pipeline.json --state-dir state/` runs
three independent pools: authenticated capture, SDK Builder, and browser
Verifier. The spec schema is `schemas/synthetic-pipeline-spec-v1.schema.json`.
Each site has a persistent checkpoint, stage-specific timeout/restart budget,
and filesystem lease. A supervisor restart reconciles a completed stage result
or safely resumes the same task; it does not start concurrent workers that can
modify one candidate.

Both watchdog timeouts and ordinary nonzero stage exits consume
`max_stage_attempts`. A remaining attempt restarts the same checkpointed stage;
only an exhausted budget becomes `builder_timeout`, `verifier_timeout`, or
`failed`. Replay Builders must finish a runnable, provenance-complete baseline
before their bounded smoke pass and leave iterative pixel tuning to the
independent visual verifier and repair stage.

Each stage writes private mode-0600 stdout and stderr logs beneath the
supervisor state directory. These diagnostics are excluded from release
artifacts. Transporting capture jobs into E2B and delivering authorization
notifications remain deployment integrations supplied by the calling system;
the repository supervisor records states and evidence paths but does not itself
send notifications.

Set `max_visual_repairs` to `3` for the normal production policy. Every repair
continues the original Builder trace, refreezes candidate bytes, and reruns both
verifiers. Reaching the limit stops that site at `visual_review_required` while
other sites continue using their own pool slots and checkpoints.
