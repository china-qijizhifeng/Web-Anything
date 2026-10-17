---
name: build-synthetic-benchmarks
description: Build or repair deterministic static browser benchmarks from a frozen WebAnything synthetic contract. Use when the run build_mode is benchmark_synthetic, including fictional or frozen source-fidelity content, localStorage state, fixed time, stable IDs, reset support, and task-replayable interactions without real external APIs.
---

# Build Synthetic Benchmarks

Implement the contract as a static browser application. Treat the frozen input bundle as immutable authority and keep the mirror production route untouched.

## Read the authority

1. Read `.webanything/synthetic-input/synthetic_contract.json` completely.
2. Read every hash-bound fixture and `tasks.jsonl` from that bundle.
3. Read [contract-runtime-and-evidence.md](references/contract-runtime-and-evidence.md).
4. When the contract binds a visual reference, read
   [visual-reference-and-repair.md](references/visual-reference-and-repair.md)
   and the frozen source-visible/comparison manifest and geometry supplied under
   `.webanything/synthetic-input/visual-reference/`.
5. Work only inside `sites/<site_id>/` unless the run brief explicitly narrows the candidate elsewhere.

Do not edit `.webanything/synthetic-input`, verifier code, release evidence, or contract hashes. If the contract is inconsistent or impossible, stop with a concrete blocker. Contract changes require a new compiled contract and a new run.

## Build the candidate

- Read `content_policy.mode` before authoring content. Legacy contracts without a mode use `fictional`.
- For `fictional`, use only fictional brands, identities, text, commerce data, and local synthetic or licensed media.
- For `source_fidelity`, reproduce the frozen screenshot's visible brand, text, identities, media, chrome, colors, and layout as closely as possible. You may crop or derive bounded local media from the frozen screenshot, but must implement the page with semantic HTML/CSS and real task interactions; never use the entire screenshot as a page-sized overlay.
- Do not fetch, trace, or screenshot the live seed site. The orchestrator-owned frozen reference is the only permitted upstream content input. Never access credentials, cookies, storage state, raw DOM, or upstream APIs.
- Copy the frozen `synthetic_contract.json`, `tasks.jsonl`, and `fixtures/` into the site byte-for-byte.
- Copy `runtime/webanything-benchmark-runtime.js` to `sites/<site_id>/webanything-runtime.js` byte-for-byte.
- Serve all scripts, styles, fonts, images, icons, and fixtures locally. Never make an external request.
- Record every source-derived candidate file in `content-provenance.json` for `source_fidelity`. The independent verifier compares the candidate directly with the frozen source-visible screenshot in this mode.
- Keep declared routes, required selectors, stable object IDs, and task action selectors exact.
- When `interaction_policy.mode` is `all_visible_controls`, every visible button,
  link, form control, and ARIA button/link must carry one unique stable
  `data-action-id`. Its value must appear in that route's
  `required_action_ids`, and every declared action id must be covered by a
  replayable task. Do not render decorative or inert controls that look
  clickable; every visible control must navigate, update deterministic state,
  or produce an observable local DOM state such as a menu or dialog.
- Route all persistent state through the declared localStorage namespace and `window.__WEBANYTHING__`.
- Route time through `getClock()` and deterministic randomness through `random()`. Do not call `Date.now()`, `new Date()`, `Math.random()`, or `crypto.randomUUID()` in candidate code.
- Implement `/reset.html`, a visible reset control, and repeatable reset behavior.
- Preserve state across reloads and avoid modifying objects outside each task's `allowed_state_changes`.
- Match the frozen reference's layout, spacing, component bounds, density, column proportions, and content policy. The full frozen reference bundle is non-release evidence and must never be copied into the candidate as-is.

Use a framework only if every dependency is vendored into the site. Prefer small static HTML, CSS, and JavaScript candidates because the verifier runs without a package install or application backend.

## Self-test without claiming acceptance

Run the static server and replay every action from `tasks.jsonl`. Check every
declared route and required selector, inventory every visible interactive
element against `required_action_ids`, two consecutive resets, reload
persistence, and the browser network log. Builder self-tests are diagnostic
only; the independent verifier owns acceptance.

Before finishing, report:

- candidate path;
- routes and tasks implemented;
- commands run and exact pass/fail status;
- any contract blocker without weakening or rewriting the contract.

Visual repair evidence is diagnostic input, not permission to edit the frozen
reference or reacquire the live source. Apply at most the orchestrator-authorized
bounded repair rounds in the original trace.
