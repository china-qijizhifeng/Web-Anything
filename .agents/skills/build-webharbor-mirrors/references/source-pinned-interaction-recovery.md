# Source-pinned interaction recovery

Use this reference when the canonical root cannot be captured faithfully but an
exact-origin public content route can, or when a captured framework form depends on
an external state-changing service.

## Exact-origin representative entry

1. Keep the canonical site in `upstream_url` / `--represented-url`.
2. Use a different `capture_url` only when its scheme, hostname, and port are exact
   matches and its own public GET plus robots checks pass.
3. Map local `/` to that captured entry and disclose which content surface is
   represented. Do not describe it as the blocked homepage.
4. Reject sibling hosts, archives, caches, proxies, authentication workarounds, and
   a route whose core CSS or presentation assets are denied.

## Framework-backed local form

Never issue the upstream POST. Declare the recovery in
`sites/<site>/interaction_workflows.json` only after captured evidence provides:

- a source-page SHA256 and one unique form class;
- required field names and one unique submit control;
- a captured page-host script URL and SHA256;
- the exact external endpoint literal and expected match count;
- visible success and failure text/classes present in that captured script.

Use `script_rewrites` to replace only the exact pinned endpoint with a local
`/_actions/<name>/` endpoint. Use `local_forms` to declare the route, form class,
required fields, submit label, action name, and response DOM semantics. The compiler
must fail closed on any hash, literal count, field, selector, or response-semantic
change.

The compiler may add a CSP-hash-pinned local form bridge when the captured framework
can submit offline but cannot reliably render its post-submit state after unsafe
bootstrap data was removed. Register the bridge in the capture phase, stop the
framework submission, send one local `FormData` POST, and render the source-grounded
success or failure DOM. Do not generalize the bridge to undeclared forms or arbitrary
fetch calls.

## Required proof

Require one local-only browser workflow to navigate, fill, click with
`state_change: true`, and assert the visible success DOM. Follow it with a SQLite
assertion for exactly one stored submission. Restore the runtime database from the
seed and require byte-identical hashes. Keep the form blocking if any browser, DB,
resource, fidelity, or reset proof fails.
