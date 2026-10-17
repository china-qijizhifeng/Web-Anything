# Local Authenticated State

Use this reference when a task needs an account, an authenticated header, an
Issue/comment/reply, a review, a PR, a saved item, or any other permissioned
workflow. This is a local simulation contract. It never authorizes upstream
login, cookie reuse, API calls, or real submissions.

## Modes

Every site that exposes authenticated behavior declares a site-local
`sites/<site>/auth_profile.json` with `schema_version: 1` and one of these modes:

- `default_authenticated`: the review browser starts as a deterministic virtual
  user. Use this by default for task-driven previews and benchmark traces.
- `explicit_login`: the browser starts logged out, but a local login form accepts
  the declared virtual test identity.
- `logged_out`: no local identity is available; authenticated controls stay in
  their source-grounded logged-out state.

The profile contains only non-secret identity and permissions, for example:

```json
{
  "schema_version": 1,
  "mode": "default_authenticated",
  "persona_id": "demo-owner",
  "handle": "demo-owner",
  "display_name": "Demo Owner",
  "email": "demo-owner@local.invalid",
  "roles": ["authenticated", "owner"],
  "permissions": ["issue:reply", "repository:write"],
  "disclosure": "Deterministic local test identity"
}
```

Validate it before seeding or packaging:

```bash
python3 .agents/skills/build-webharbor-mirrors/scripts/validate_auth_profile.py \
  sites/<site>/auth_profile.json
```

Do not store passwords, API keys, OAuth tokens, signed upstream cookies, or
personal data in this file, a seed database, a prompt, an archive, or evidence.
If an explicit-login UX is required, use a fixed local-only test credential
whose verification is implemented entirely by the site adapter; it must not be
accepted by any upstream service.

## Runtime contract

- Initialize the identity and an authenticated session deterministically from
  `instance_seed/<site>.db` or a site-local deterministic seed helper.
- Derive any Flask/session signing material from the site identity and a local
  constant or test environment value. Never copy a production secret.
- Expose a generic `current_local_identity()`/equivalent adapter boundary so
  templates, routes, browser workflows, and verifiers use one identity source.
- Render the authentic logged-in chrome, avatar/handle, permission affordances,
  and account menu from captured structure or source-grounded local markup.
- Keep login, logout, account switching, and permission checks same-origin and
  local. A logout must make the next request genuinely anonymous; a reset must
  restore the declared default mode and identity.
- Persist user-created Issue replies, comments, reviews, edits, PRs, saved items,
  and notifications in SQLite. Seed only source-grounded records and the
  deterministic persona. Do not mutate JSON capture files at runtime.
- The canonical entry page must reflect a successful mutation, including the
  local actor, stable action ID, updated summary/count, result link, and return
  path. Use [canonical state reflection](canonical-state-reflection.md).

The identity is a simulation boundary, not a disclaimer-heavy UI. Do not put
`LOCAL ONLY`, `does not commit upstream`, or implementation warnings into the
product surface merely because the state is local. Keep a concise local/source
disclosure only where the source contract or task requires it; full provenance
belongs in evidence.

## Ownership and permissions

Explorer/task contracts must declare the persona and required permission for
each state-changing action. Preserve product semantics:

- An owned repository may allow a direct local edit, issue reply, or commit.
- A non-owned repository should use the authentic fork/branch/PR path instead of
  pretending the user owns it.
- Authenticated-but-unauthorized actions must keep their real context and show a
  meaningful local permission result, not a generic 200 shell.

Site-specific tables, route names, labels, and permission rules stay in the site
adapter. The shared compiler supplies only the identity/session boundary,
deterministic reset contract, and evidence requirements.

## Required regression evidence

For every authenticated task, record a deterministic desktop browser workflow
that proves:

1. The initial page is authenticated in `default_authenticated` mode, with the
   expected local handle/role visible.
2. The task action is performed through real controls and produces a local result.
3. The canonical entry page reflects the action and links to its result.
4. Logout (or an anonymous context) sees the correct logged-out/permission state.
5. Reset restores the seeded identity, removes user-created rows, and produces a
   byte-identical runtime/seed database comparison.

The quality contract must include the identity mode, persona ID, permission,
state-changing action, SQLite assertion, reset proof, and any source limitation.
The independent audit should exercise the default logged-in workflow and a
bounded logged-out branch when both are visible. It must never enter real
credentials or send a request to an upstream auth endpoint.

## Packaging and trace use

Package the profile, deterministic seed, local session behavior, and workflow
evidence with the mirror. Exclude runtime sessions, cookies, `instance/`, and
temporary login artifacts. Before every SDK trace, reset the site and start
with the declared default identity; bind the trace to the persona ID, permission,
seed hash, and mirror implementation hash.
