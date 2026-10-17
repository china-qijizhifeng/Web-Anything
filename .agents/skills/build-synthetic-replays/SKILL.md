---
name: build-synthetic-replays
description: Build or repair source-faithful, fully local benchmark sites from a frozen DOM/CSS/font/media capture, SQLite seed, visual reference, and replay tasks. Use when build_mode is benchmark_synthetic_replay.
---

# Build Synthetic Replays

Build a real Flask/SQLite application from the orchestrator-owned frozen replay bundle. This mode is for the high-fidelity, fully interactive route; it does not replace the static `benchmark_synthetic` route.

## Read the frozen authority

1. Read `.webanything/replay-input/synthetic_replay_contract.json` completely.
2. Read its capture manifest, every listed local capture file, SQLite seed, tasks, and visual reference. URL-first route JSON contains shadow DOM, computed layout, stable action metadata, and local navigation targets; `capture/asset-map.json` maps hashed source identities to local resources. Treat them and each rendered HTML snapshot as authoritative.
3. Read [runtime-state-and-reset.md](references/runtime-state-and-reset.md).
4. Read [capture-and-release-boundaries.md](references/capture-and-release-boundaries.md).
5. Work only inside `sites/<site_id>/`.

Never edit `.webanything/replay-input`, verifier code, freeze records, or release evidence. A contract change requires a new run.

The contract `coverage` block is a release authority, not a suggestion. For
`release_tier: product_complete`, implement every `required_route_kinds` route
and every `required_workflows` journey even when a route was synthesized from
the detected product profile rather than captured directly from the source.

## Build the candidate

Build in this order so an interrupted turn leaves a complete baseline instead
of an unsealed partial site:

1. copy the contract, tasks, seed, canonical runtime, capture pages, and local assets;
2. implement the Flask/SQLite runtime and every declared evaluator;
3. write `replay-provenance.json` and run static compilation;
4. run the bounded self-test pass below;
5. hand the candidate back to the independent verifiers promptly.

- Preserve the orchestrator-seeded `app.py` captured-route handlers: they serve the frozen DOM, local assets, and `computed_styles`. Inject semantics there and retain at least 60% of visible stable node IDs; visual repair may change only existing frozen nodes' CSS, local asset URLs, visibility, and layout—replacement grids, duplicate content layers, model-redrawn pages, and screenshot overlays are invalid. Preserve declarative `template[shadowrootmode="open"]` content and each captured video's route-local poster; never replace every inert `blob:` URL with one arbitrary media file. If playback needs a source, select only a capture asset whose `route_ids` contains the current route and keep the poster as its deterministic initial frame.
- Do not access the live seed URL. The frozen capture and visual reference are the only source inputs available to Builder.
- Do not execute or copy upstream JavaScript. Reimplement interaction behavior locally.
- Copy `synthetic_replay_contract.json`, `tasks.jsonl`, and the SQLite seed into the candidate byte-for-byte at their declared paths.
- Copy `runtime/webanything_replay_runtime.py` into the candidate and use its prefix and atomic-reset helpers rather than reimplementing them.
- Use Flask plus SQLite. Persistent mutations must go to `instance/`; immutable seed bytes stay under `fixtures/`.
- Implement local synthetic login only. Never copy source cookies, tokens, account identifiers, passwords, or browser storage.
- Make authentication a real user-visible journey, not evaluator-only state. The
  initial route must contain one visible clickable `[data-webanything-auth-entry]`
  that opens the declared login task route. After a successful login, the
  initial route must render a visible
  `[data-webanything-auth-state="authenticated"]` element with synthetic user UI;
  both the DOM state and evaluator result must persist across reload. Logout
  must restore the visible login entry. Forms and redirects must remain under
  exactly one deployment prefix when routed through `/p/<port>/`.
- Implement every task, its `success` postconditions, and its evaluator. An
  evaluator must read the same business DOM, URL, or SQLite state named in the
  task; recording only a click or `replay_events` row never satisfies a
  product workflow.
- A product-complete `video_portal_v1` must expose feed/search/detail, playback, comments, favorites, history, profile, auth, and reset; persist each mutation.
- A product-complete `commerce_portal_v1` must expose home/category/search, detail/cart/orders/profile/auth/reset; persist cart additions and checkout orders.
- A product-complete `travel_portal_v1` must expose home/browse/search, flight detail/bookings/profile/auth/reset; persist searches and bookings.
- A product-complete `content_portal_v1` must preserve captured content routes, expose semantic navigation/bookmark/auth/reset, and persist bookmarks.
- A `static_landing_v1` is complete only for its captured home surface and must
  expose each state action through its declared result DOM plus SQLite event.
- Preserve every captured `data-action-id`, map anchors to the same declared product route kind without a home fallback, keep them below HTTP 400, and give new controls stable IDs. URL-first capture
  already excludes visible-but-occluded controls with browser hit-testing and records a
  source `hit_point` for each retained action. The visible inventory on each route must
  exactly match `required_action_ids`; do not resurrect excluded carousel layers or
  invent visual-only/dead controls. Preserve normal document hit-testing; if frozen
  layout drift occludes a declared action, use its recorded `hit_point` to repair the
  local layout or add one non-overlapping semantic hit target instead of globally
  disabling pointer events.
- Implement every URL-first captured action task. Preserve `target_local_path`
  navigation locally and give non-navigation controls an observable SQLite event
  or state transition that their evaluator can distinguish from a no-op.
- Implement only declared product routes. Never add a catch-all HTTP 200 page,
  `data-webanything-route="local-target"`, or generic destination placeholder;
  an undeclared route must return 404. Every declared route must render its
  own route-kind DOM and satisfy its contract selectors.
- Make routing and asset URLs work both at `/` and behind a prefix such as `/p/40000/`. The health endpoint must report the effective `X-Forwarded-Prefix`.
- Use the canonical runtime's `scope_html_urls()` helper when captured markup
  needs root-relative `href`, `src`, `action`, or `poster` rewriting. Do not use
  a broad `href|src` regex: it can match the suffix of custom attributes such
  as `data-webanything-auth-href`, causing a doubled `/p/<port>/p/<port>/` URL.
  Native URL attributes and synthetic `data-*` navigation attributes must each
  contain exactly one effective deployment prefix.
- The runtime entrypoint must bind `HOST` and integer `PORT` from the environment,
  with local defaults only when they are unset. The independent verifier selects a
  fresh port, so a hard-coded development port is a release-blocking failure.
- Serve no external runtime requests or real APIs; all assets, fixtures, and `/favicon.ico` are local and return successfully.
- When a self-test starts a background server, retain its exact PID and stop only
  that PID. Never use broad `pkill -f` or `killall` cleanup: it can terminate the
  Builder/controller process and turn a successful repair into a failed trace.
- Route time through the fixed contract clock. Do not use current wall-clock time to generate visible state.
- Write `replay-provenance.json` with schema `webanything-replay-provenance-v1`
  and the exact verifier fields `capture_bundle_sha256`, `seed_sha256`, and
  `candidate_files` (a JSON array). Copy the corresponding hashes byte-for-byte
  from the frozen contract; alternate names such as `frozen_*` are not valid.

## State isolation

Honor `concurrency.isolation` exactly:

- `isolated_instance`: one runtime is one benchmark session; `max_parallel_sessions` must be 1. Multiple sites may still run concurrently in separate runtimes.
- `session_partitioned`: auth and mutable state are partitioned by an opaque browser-session identifier, and concurrent sessions must not observe each other's actions.

Never advertise multi-session support when using one instance-global SQLite state.

## Self-test

Builder self-tests are a bounded smoke pass, not a replacement for the
independent functional and visual stages. After the first successful runtime
smoke test, run at most:

- one full route/action inventory sweep;
- one batched task/evaluator, login/logout, persistence, and double-reset sweep;
- one browser network/console/HTTP and `/p/40000` prefix sweep.

Never call `verify_synthetic_replay_candidate`, `verify_synthetic_visual`, or
their CLI commands from Builder. They require an orchestrator-created fresh
freeze and the supervisor runs them immediately after Builder exits. Keep every
Builder tool command bounded and completed before the final response; do not
leave long-poll or background command sessions pending.

Do not spend the remainder of the Builder turn repeatedly taking screenshots,
measuring individual elements, or tuning pixel differences. The independent
visual verifier owns those comparisons and emits a bounded repair package for
the repair stage. If the bounded smoke pass succeeds, finish and hand off even
when visual fidelity still needs independent verification. If it fails, fix
the concrete functional cause and rerun only the affected sweep.

The bounded sweeps must check:

- every declared route and selector;
- every coverage route kind and core workflow, including each task's semantic
  DOM, URL, and SQLite postconditions before and after reload;
- every visible `data-action-id` and task replay;
- local login and logout;
- the initial-page login entry, normalized post-login URL, visible authenticated
  DOM state, and persistence of that state after reload;
- state persistence across reload;
- two resets are byte-identical to the runtime SQLite seed;
- evaluator positive and negative cases;
- no external request, HTTP error, or console error;
- prefix operation with `X-Forwarded-Prefix: /p/40000`;
- the complete prefixed auth journey: visible entry, login route, submit,
  normalized post-login URL, authenticated DOM state, and reload persistence;
- startup with non-default `HOST` and `PORT` values, followed by a health check;
- exact replay-provenance field names and hashes;
- the candidate tree is unchanged by verification except the excluded `instance/` runtime directory.

Report exact paths and pass/fail evidence. Builder self-tests are diagnostic;
only independent verification and release gates can accept the candidate.
