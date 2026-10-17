# Bounded editorial coverage

Use this contract for public sites whose useful structure is small but whose news,
events, notices, posts, or historical pagination is effectively unbounded. Examples
include schools, institutes, government departments, newsrooms, and blogs. Do not
apply it to product catalogs, API/documentation trees, current job inventories,
transactional records, or unique primary-navigation pages.

The same policy transport supports a `major_surface_first` profile for other site
types. In that profile, use `deferred_rules` for repetitive detail families,
login/account variants, and secondary surfaces. Never defer required routes or core
workflows. Evidence must expose both scoped and raw all-visible click-through, and a
pass may claim only `major_surface_mirror` until the deferred inventory is closed.

## Scope before capture

Declare a `bounded_editorial_archive` JSON policy before increasing a page budget.
Include:

- every canonical homepage, primary navigation/footer page, contact/policy page,
  and important category index in `required_routes`;
- one anchored destination regex per repetitive route family;
- an optional anchored source-index regex so categories do not leak into each other;
- a small deterministic `representative_limit` per family;
- an explicit reason for excluded pagination or unusual archives.

Prefer roughly 3-6 newest visible details per category. Raise this only when the
details express distinct layouts or workflows, not to improve a raw link ratio.
Keep asset and page budgets proportional to the declared surfaces.

## Deterministic selection

Traverse required routes in declared order. On each category index, select the
first unique visible detail links in DOM order until its quota is full. Record later
matches as `bounded_archive_exclusion`; do not visit them, send them to SDK repair,
or count them as failed click-through. Preserve exact URL, source path, link text,
rule ID, quota, and exclusion reason in evidence.

The result must be reproducible from the same local DOM and policy. Do not use
random samples, dates inferred from URL strings, or an LLM to choose representatives.

## Acceptance evidence

`clickthrough-coverage.json` must report:

- policy profile/name and every required route;
- each category's quota, selected representative count, and excluded count;
- all excluded links in `bounded_archive_exclusions`;
- all genuine gaps separately in `unimplemented_links` and `next_candidates`;
- scoped click-through computed without excluded archives;
- a failed gate when a required route is missing, empty, broken, or a limitation.

Existing over-captured pages may remain packaged when deleting them would disturb
hash-pinned provenance. Their presence does not expand the declared completion
scope. A later run may prune them only through a separate, verified migration.

## CLI

Pass the same policy through audit, repair, and publication:

```bash
run.sh audit-clickthrough \
  --base-url http://127.0.0.1:41000 \
  --scope-policy requirements/institute.scope.json

run.sh iterate-sdk-preview ... \
  --scope-policy requirements/institute.scope.json
```

The final publisher must recompute the audit with the identical policy. Never edit
coverage evidence by hand or treat an unscoped high raw link count as better proof.
