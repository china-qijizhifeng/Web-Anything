# Source-visible reference and repair

The visual reference is prepared by an independent authenticated capture worker.
It contains a source-visible screenshot for layout guidance, a separate
content-neutral comparison image for the verifier, text-free DOM rectangles,
aggregate layout metrics, thresholds, and hashes. Authentication state, raw DOM,
cookies, local browser storage, and credentials remain outside Builder authority.

## Build use

- Read `synthetic_visual_reference.json` and its hash-bound route artifacts.
- Use `synthetic_route` to map each reference surface to the candidate route.
- Follow `content_policy.mode` from the frozen contract.
- In `fictional`, treat source text, identities, and media as layout guidance;
  use fictional content and local synthetic or licensed assets. The verifier
  compares the content-neutral derivative.
- In `source_fidelity`, reproduce visible source text, brand, identities, media,
  chrome, colors, landmarks, columns, density, and spacing. Bounded media may be
  derived from the frozen screenshot and saved locally. The verifier compares
  the rendered candidate directly with the source-visible screenshot.
- In every mode use semantic DOM and working interactions. Never serve the whole
  source-visible screenshot as a page-sized overlay or copy the complete frozen
  reference bundle into the candidate.
- Never access the live source or request authentication material.

## Repair use

Visual repair packages contain route-level scores, thresholds, category density
deltas, and bounded mismatch rectangles with `x`, `y`, `width`, `height`,
`category`, and `kind`. Start with those coordinates instead of applying
route-wide style changes.
Modify only candidate files under `sites/<site_id>/`. Keep the contract and visual
reference unchanged, preserve deterministic behavior, and rerun functional
self-tests after every layout repair.

The orchestrator owns the maximum repair count, candidate refreeze, functional
verification, visual verification, and release. A `visual_review_required`,
`auth_expired`, `source_blocked`, `capture_failed`, or `sanitize_failed` state is
not a Builder task and must not be relabeled as completed.
