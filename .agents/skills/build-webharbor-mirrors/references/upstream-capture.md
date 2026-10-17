# Upstream capture contract

Read this file before collecting a live website or claiming visual/interaction
fidelity.

## Boundary

- Use public, unauthenticated `GET` requests only. A bounded stateless search or
  one-option `select`, checkbox, or radio filter GET may be derived from a captured form when every
  value comes from visible/default page data and every field is non-sensitive.
  Never enumerate option combinations. Never issue POST/state-changing
  submissions, create an account, bypass a challenge, replay credentials, or
  collect personal data.
- Fetch and enforce `robots.txt` before any page. A missing, denied, or
  unparsable robots response fails the capture unless a human has recorded a
  site-specific authorization basis in the spec.
- Declare allowed page and asset hosts, page and asset URL patterns, start URLs,
  depth, page and asset counts, response byte limits, and a delay. Redirects must
  stay in scope.
- Treat generated search/filter queries as optional scope. Check robots independently for
  an allowed government sibling search host. Record WAF/robots/query rejection and
  continue only the core pages; never synthesize a result page to make search pass.
- Treat `401`, `403`, `429`, CAPTCHAs, consent walls, and bot challenges as
  blockers. Record them; do not work around them.
- Record why the content may be mirrored. Public availability alone is not a
  copyright license. Prefer government/public-domain content, permissively
  licensed projects, and sources with explicit authorization.
- Reject literal or resolved private, loopback, link-local, and reserved network
  targets in the public URL entry. Validate every redirect before following it and
  require its host to remain in the declared scope.

## Capture

Create a JSON spec under `mirror_factory/upstream_specs/<site>.json`, then run:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh capture-upstream \
  mirror_factory/upstream_specs/<site>.json
```

The command stores response bodies under
`sites/<site>/static/external_cache/upstream/` (HF-managed and ignored by Git)
and writes metadata to `mirror_factory/evidence/<site>/upstream.json`. Evidence
must include the final URL, status, media type, byte size, SHA256, crawl scope,
robots result, failures, and capture-tool version.

For sites exposing stable JSON-LD, generate traceable seed input instead of
copying records by hand:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh extract-jsonld \
  --site <site> --types GovernmentService FAQPage \
    --output sites/<site>/upstream_records.py
```

If the captured pages do not expose useful JSON-LD, generate a verified HTML
content inventory instead of copying visible content without provenance:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh extract-html \
  --site <site> --output sites/<site>/upstream_records.py
```

The HTML extractor verifies every captured page hash and records page titles,
meta descriptions, heading outlines, normalized links, and main-landmark text.
Use those records as traceable seed input; site-specific modeling is still
required and must not collapse the content into a generic portal schema.

For repeated HTML components, use a declarative extraction spec instead of a
site-specific scraping script. `extract-components` supports flat item lists and
nested container/item groups, text fields, repeated text fields, and normalized
`href`/`src` attributes. It verifies the evidence status and each source page
hash before generating a Python record module.

The extractor refuses missing or hash-mismatched captured pages and embeds the
capture evidence hash in the generated module.

Never load captured HTML or JSON directly from a Flask handler. Extract the
stable records needed by the mirror into `instance_seed/<site>.db`. Serve local
images, fonts, and styles from `static/`; do not leave runtime dependencies on
the upstream host.

For a fully automatic read-only candidate, `compile-snapshot` may compile verified
captured HTML and assets into a compressed build-time bundle. The generated app
seeds that bundle into SQLite; HTTP handlers still read only SQLAlchemy models.
This preserves site-specific DOM and visual assets without treating a common
runtime engine as a common visual template. Record all dynamic behavior that
cannot be reconstructed from public GET responses in `workflow_gaps.json`.
The replay compiler stores actual same-site canonical redirects as SQLite route
aliases. It serves only exact captured search/filter query variants; an unknown
query returns a local scope notice instead of falling back to unrelated results.
It localizes and may execute only hash-verified scripts from declared page hosts;
each claimed script-backed control still needs a passing local-only browser
workflow. Preserve external SVG sprite fragments when rewriting `href` or
`xlink:href` so captured icon symbols remain usable offline.

When the raw HTML is only a JavaScript application shell, the automatic pipeline
may serialize one desktop DOM after executing directly referenced captured scripts
and module preloads. The browser routes every HTTP request through the hash-verified
capture map, blocks uncaptured URLs, WebSockets, service workers, and non-GET
requests, and records the source-page SHA256, every used resource SHA256, the DOM
SHA256, console errors, and blocked requests. The compiler and fidelity baseline
must both consume that same rendered record. A DOM that remains structurally blank
is not promoted.

When a captured stylesheet contains remote or root-relative URLs, preserve the
captured original and generate an offline copy from an explicit replacement
spec:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh localize-css \
  mirror_factory/localization_specs/<site>_css.json
```

The command verifies the captured input hash, requires every declared
replacement to match, and rejects remaining external `url(...)` values.

## Reconstruction

1. Inventory the upstream information architecture, visible components,
   responsive breakpoints, typography, colors, spacing, assets, and workflows.
2. Select benchmark workflows explicitly. A static screenshot is insufficient
   for search, filters, detail navigation, forms, saved state, or account state.
3. Reproduce upstream routes and query parameter names where practical. Keep
   destructive or externally consequential actions local and deterministic.
4. Preserve upstream labels and domain records. Do not replace them with generic
   item names, invented scores, or unrelated CRUD controls.
5. Implement each workflow against SQLite and verify reset byte identity.

## Fidelity evidence

Capture matched upstream/local screenshots at the required desktop size. A
mobile viewport may be added to a site-specific spec when responsive fidelity
is in scope. Record:

- viewport, URL pair, screenshot hashes, and capture time;
- route and interaction coverage;
- structural checks for landmarks, headings, forms, links, and result counts;
- visual comparison metric plus reviewed differences;
- unsupported upstream behavior and why it was excluded.

Do not call a site `reviewed` from HTTP 200, task-agent success, or screenshot
similarity alone. Runtime, reset, workflow, deterministic verifier, and fidelity
evidence are separate gates.

Run the bundled browser comparison from an environment containing Playwright,
Chromium, and Pillow:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh verify-fidelity \
  mirror_factory/fidelity_specs/<site>.json
```

The command also fails when a local page is blank, lacks core landmarks, or
requests an external HTTP resource.
