# Canonical State Reflection

Use this reference for any local workflow that mutates deterministic SQLite
state, such as saving an edit, adding an item, submitting a form, or changing a
setting.

## Contract

The workflow's canonical entry page must reflect the latest local state after a
successful mutation. A deep result page alone is insufficient: an agent or
reviewer should be able to return to the entry route and understand what
changed, why it changed, and where to inspect the result.

The shared pipeline rule is generic. Keep site-specific markup, labels, layout,
and route names in the site's adapter or override. The generic contract should
only require:

- a stable state identifier and human-readable summary;
- a local-only/source-limited disclosure when the state is not upstream state;
- a link to the local result and a link back to the action/entry route;
- deterministic persistence in the site's SQLite database; and
- byte-identical seed reset after the workflow.

## Deterministic regression

For each stateful workflow, record a browser proof with this shape:

1. Open the canonical entry route and perform the action with fixed input.
2. Assert the local result page contains the expected identifier and summary.
3. Navigate to the canonical entry route, assert the same identifier/summary and
   local disclosure are visible, and follow the result link.
4. Return to the entry route and assert the reflection still exists.
5. Assert the corresponding SQLite row and status, stop the owned server, reset
   from `instance_seed`, and compare seed/runtime hashes byte-for-byte.

Do not count a scope notice, departure page, or a result that is reachable only
by remembering a hidden URL as canonical state reflection. If the upstream site
has no equivalent state, label the panel as a local offline demonstration and
keep the implementation isolated to the site adapter.
