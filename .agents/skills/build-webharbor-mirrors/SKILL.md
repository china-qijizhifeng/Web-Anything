---
name: build-webharbor-mirrors
description: Build or repair a source-grounded, high-fidelity, interactive WebHarbor mirror from a public URL in an isolated workspace. Use for URL intake, bounded capture, local reconstruction, deterministic SQLite state, visual/runtime verification, or applying a machine-readable Builder repair package. The Builder may edit only its candidate workspace; use the standalone interaction verifier for read-only acceptance.
---

# Build WebHarbor Mirrors

Create one deterministic local site from public, bounded evidence. The candidate
must preserve the source site's observable visual hierarchy and interaction
semantics without making upstream state changes.

## Ownership boundary

- Work only in the isolated workspace supplied by the orchestrator.
- The Builder is the only role allowed to edit candidate code, assets, or data.
- Never edit the source repository, production Skill, verifier, or another run.
- Verifier output is input data. Do not rewrite it to manufacture acceptance.
- Do not inspect, print, persist, or mention credentials, cookies, storage state,
  authorization headers, or private account evidence.

## Select the run profile

- `task-driven`: implement declared workflows and visible return paths.
- `immersive`: resolve every in-scope active-looking control with a natural local
  result; generic fallbacks and defensive scope pages are failures.
- `authenticated`: integrate anonymous and authorized logged-in states using a
  deterministic virtual identity. Read
  [local-authenticated-state.md](references/local-authenticated-state.md) and
  [authenticated-upstream-visuals.md](references/authenticated-upstream-visuals.md).

Read [full-site-completeness.md](references/full-site-completeness.md) for
immersive work and [reviewable-workflow.md](references/reviewable-workflow.md) for
task-driven work.

## Initial URL build

For a new public URL, read [automation.md](references/automation.md) and run:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh build https://example.com/ \
  --site example --max-pages 12 --max-assets 160
```

The command performs intake, robots and access checks, bounded capture,
reconstruction, deterministic seed creation, catalog registration, runtime
checks, matched visual verification, quality checks, and packaging. Resume the
same command after a genuinely resumable infrastructure failure.

Do not bypass robots, WAF, authentication, or source denials. Use unauthenticated
GET-only acquisition unless the orchestrator explicitly provides an authorized
private visual profile. Never POST to an upstream form.

Read [upstream-capture.md](references/upstream-capture.md) when capture fails.
Historical recovery is permitted only under the source-pinning rules in
[historical-fallbacks.md](references/historical-fallbacks.md).

## Build requirements

1. Preserve authentic HTML structure, CSS, fonts, icons, images, and responsive
   geometry when public source evidence is available. Never serve screenshot
   crops as page assets.
2. Localize required runtime resources. The finished candidate must not depend
   on undeclared upstream requests.
3. Implement canonical routes, queries, navigation, menus, forms, pagination,
   state changes, and return paths declared by the Site Contract.
4. Every active-looking control must have a meaningful local outcome for the
   selected profile. Do not use a universal fallback route to simulate coverage.
5. Use deterministic SQLite seed data. Runtime changes go only to the runtime
   database; reset must restore byte-identical seed bytes.
6. Synthetic content is allowed only when the Site Contract permits it and the
   record carries source-grounded provenance. Never synthesize private user data.
7. Keep homepage `/` inside visual and interaction acceptance. Do not redirect it
   to hide a failed entry page.
8. Record limitations honestly. A blocked source can remain blocked; it cannot
   be relabeled as complete.

Read [evidence-contract.md](references/evidence-contract.md) before writing final
status and [manifest-schema.md](references/manifest-schema.md) before changing the
target manifest or catalog.

## Local verification before handoff

Run the relevant deterministic checks through `scripts/run.sh`:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh verify-fidelity --help
.agents/skills/build-webharbor-mirrors/scripts/run.sh verify-quality --help
.agents/skills/build-webharbor-mirrors/scripts/run.sh audit-candidate-resources --help
```

The candidate is not complete merely because its process starts or routes return
HTTP 200. Current runtime, visual, resource, workflow, SQLite mutation, reset, and
external-request evidence must agree.

The standalone interaction verifier runs after Builder handoff. It triangulates
the Site Contract, source inventory, rendered browser controls, and actual Flask
routes. Do not invoke it as a way to change the candidate.

## Repair continuation

For an existing run, read
[interaction-closure-loop.md](references/interaction-closure-loop.md). Consume the
provided `builder-repair-package.json`, repair only the reported route/control
families, and rerun all affected checks. Preserve the original workspace and
evidence lineage.

Do not lower thresholds, remove failing routes, disable valid controls, replace
real pages with placeholders, or edit verifier artifacts. If a repair reveals a
new dependent route or menu item, implement and verify that result as well.

## Final handoff

Before reporting completion:

- stop temporary processes;
- prove seed immutability and byte-identical runtime reset;
- confirm no unexpected external requests;
- remove temporary authentication material and private screenshots;
- leave machine-readable build, fidelity, quality, runtime, and provenance files;
- report exact status, site ID, candidate path, evidence paths, checks run, and
  remaining limitations.

The orchestrator—not the Builder—runs independent interaction acceptance,
release freezing, deployment, and public re-verification.

For trace finalization and process ownership, read
[trace-finalization-and-runtime.md](references/trace-finalization-and-runtime.md).
