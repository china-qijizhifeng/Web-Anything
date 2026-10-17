# Public release status

Snapshot date: 2026-10-17

WebAnything is an alpha research codebase. The package, CLI, schemas, runtime,
documentation, and current user-facing examples use the `webanything` name.
Some `webharbor-*` schema IDs and the `build-webharbor-mirrors` skill path are
retained as compatibility identifiers for existing frozen evidence.

## Verified release candidate

- the default branch was rebuilt from a clean root history;
- the fixed-source provenance comparison found no non-empty exact, high, or
  medium similarity match to the pinned public WebHarbor tree;
- Dependabot reports zero open dependency alerts;
- the Python package, standalone Interaction Verifier, Node trace utilities,
  compilation check, doctor command, and 244-site manifest validation pass;
- the release scan passes after portable provenance paths are used;
- the repository contains the 244-site metadata inventory, not the complete
  historical site trees or media bundles;
- USWDS and Mermaid examples carry separate third-party notices.

## Alpha scope

- `webanything setup` configures a Responses-compatible model provider and a
  local Codex or Claude Code skill;
- `webanything create URL --mode jit --task "..."` builds and independently
  verifies a task path;
- `webanything create URL --mode full` requests broader discoverable coverage;
- successful page rendering remains intermediate evidence; the authoritative
  result is the verifier and release-gate status;
- source-site terms, content rights, authentication state, and generated site
  redistribution remain the user's responsibility.

## Remaining release decision

The current technical release candidate is ready for an alpha tag. Activating
the planned MIT license requires the project owner to confirm authority to
license the internally developed implementation described in
[`docs/licensing/PROVENANCE_AUDIT.md`](licensing/PROVENANCE_AUDIT.md). The
repository must remain private until that confirmation, the root `LICENSE`,
and the alpha release are committed together.
