# Evidence contract

The following artifacts are the intended stable handoff surface:

```text
runs/<run-id>/
  webanything-run.json
  discovery/discovery-package.json
  contract/site-contract.json
  workspace/
  verification/runtime.json
  verification/visual.json
  verification/interaction-contract.json
  verification/code-interaction-inventory.json
  verification/interaction-diff.json
  verification/interaction-closure.json
  verification/builder-repair-package.json
  verification/terminal-result.json
  release/release-manifest.json
  deployment/deployment.json
  deployment/public-verification.json
  candidate-freeze.json
  verification/checkpoints/batch-*.json
  release/release-gate-result.json
```

Acceptance requires domain-specific fields, not only file existence:

- runtime and visual checks must report `passed`;
- interaction terminal result must report its accepted terminal state;
- candidate pre/post hashes must match across verification;
- reset runtime SHA-256 must equal seed SHA-256;
- secret and private-evidence scans must pass;
- deployment must use a fresh sandbox identity;
- public verification must address the public URL and release hash.

Missing evidence is a failure, not a warning.

`candidate-freeze.json` (`webanything-candidate-freeze-v1`) binds the canonical
candidate tree hash to contract/input hashes and verifier configuration. All
passing evidence must carry that identity and the current tree hash. A normal
Builder `build` cannot replace a frozen candidate in the same run directory;
`repair` first invalidates downstream evidence and transitions the freeze to
`invalidated`.

The release gate is read-only and fail-closed. It requires current Fidelity,
Quality, exhaustive Interaction Closure, resource/privacy,
candidate-immutability, and byte-identical
runtime-reset evidence. It writes `release-gate-result.json` atomically and
only then writes the hash-bound v2 release manifest. A bare `{status: passed}`,
stale filename, mixed candidate hash, incomplete checkpoint merge, unresolved
external/private evidence, or candidate mutation is rejected with a structured
reason code.

`webanything build` writes `webanything-run.json` before dispatch and atomically
reconciles it with the SDK runner's terminal `run.json` afterward. A successful
process without terminal trace evidence is recorded as `failed`, never left as
`dispatched` or inferred as complete.
