# Reviewable Workflow Profile

Use this profile when ten or more mirrors need to be available for human review
quickly, while preserving the design, data, and interaction fidelity boundary.

The acceptance boundary is one source-grounded local browser workflow:

1. A visible local entry route.
2. At least one pointer or keyboard action a reviewer can perform.
3. A visible result that is not a limitation or departure notice.
4. A local return or continue path, proved by a second visible assertion.
5. No failed, HTTP-error, broken-image, unloaded-stylesheet, or external runtime
   request on every route visited by the workflow.
6. For a state-changing action, a SQLite assertion and byte-identical seed reset.

Declare it in `sites/<site>/reviewable_workflow.json` with a pinned normal
`mirror_contract.json` workflow, explicit entry/action/result/return roles, and a
nonempty `known_unfinished_scope`. Site-specific selectors and route choices stay
in the site declaration and contract; the shared verifier has no site names.

Run the deterministic gate with:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh verify-reviewable-workflow \
  --workspace <workspace> --site <site> --base-url http://127.0.0.1:<port>/
```

Evidence is written to
`mirror_factory/evidence/<site>/reviewable-workflow.json`. It is not a replacement
for `coverage.json`, `fidelity.json`, or `quality.json`; it is a separate narrower
claim. Gallery entries use tier `sdk_reviewable_workflow` and disclose that broad
coverage remains unfinished.
