# Captured catalog recovery

Use this reference when a captured official script or hydrated page contains a
listing/detail catalog that the initial page compiler did not materialize.

## Rules

1. Pin and verify the captured resource SHA256 before parsing it.
2. Prefer structured payloads inside the captured bytes. Use a JavaScript parser
   when practical; if the payload is passed to `JSON.parse`, decode the JavaScript
   string literal before parsing JSON and keep exact start/end markers site-local.
3. Parse the capture only while building an empty seed. Store every visible
   listing/detail field in a site-local SQLAlchemy table. HTTP handlers and normal
   application startup must read SQLite, never the capture file.
4. Preserve source order, identifiers, categories, locations, full HTML detail
   bodies, and source detail/submit URLs. Do not invent missing JD sections.
5. Render a real index, every captured detail route, an application/back path, and
   a 404 for unknown identifiers. Keep upstream POST disabled; implement only a
   source-pinned local workflow with reset proof.
6. Add deterministic checks for capture hash, capture/SQLite row equality, every
   index href, representative short and long details, and all declared routes.
7. Classify the extractor and field mapping as a site override. The reusable
   compiler owns the list/detail/SQLite contract, not site-specific byte markers.

For nested SDK trace workspaces, every evidence command must discover the nearest
ancestor containing both `sites/` and `mirror_factory/`. Do not assume a fixed
`parents[N]`: it can silently read or write an older sibling evidence tree.

Browser workflow verification must treat a local `target=_blank` destination as a
popup: capture the new page, keep the same-origin network gate, wait for its exact
path and query, then continue assertions on that page.
