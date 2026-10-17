# Contract, runtime, and evidence rules

## Frozen bundle

The runner exposes the compiled bundle at `.webanything/synthetic-input/`:

```text
synthetic_contract.json
tasks.jsonl
fixtures/
```

Every listed artifact is SHA-256 bound. Copy the bundle into the candidate without rewriting JSON or JSONL. The independent verifier compares the candidate copies with the frozen source.

## Runtime setup

Load `webanything-runtime.js` before application code. Fetch the packaged initial state and create the runtime once:

```javascript
const initialState = await fetch(resolveSiteUrl("/fixtures/initial-state.json")).then(r => r.json());
window.WebAnythingRuntime.create({
  namespace: contract.storage.namespace,
  schemaVersion: contract.storage.schema_version,
  fixedTime: contract.clock.fixed_time,
  randomSeed: contract.random.seed,
  initialState,
  initialUrl: contract.reset.initial_url,
  resolveUrl: resolveSiteUrl,
});
```

The required evaluator API is:

```text
window.__WEBANYTHING__.getState()
window.__WEBANYTHING__.setState(state)
window.__WEBANYTHING__.update(mutator)
window.__WEBANYTHING__.reset(options)
window.__WEBANYTHING__.getClock()
window.__WEBANYTHING__.random()
```

The page API makes implementation consistent, but it is not authoritative by itself. The verifier independently reads DOM, logical URL, raw localStorage, and network requests.

## Candidate metadata

Add `content-provenance.json`:

```json
{
  "schema": "webanything-synthetic-content-provenance-v1",
  "synthetic": true,
  "upstream_assets": [],
  "assets": ["index.html", "app.js", "styles.css"]
}
```

List every authored or generated content asset. Do not include private evidence, upstream snapshots, browser profiles, environment files, or credentials.

For `content_policy.mode=source_fidelity`, use:

```json
{
  "schema": "webanything-synthetic-content-provenance-v1",
  "synthetic": false,
  "source_reference_sha256": "<contract visual_reference.reference_sha256>",
  "upstream_assets": ["app.js", "styles.css", "assets/video-001.jpg"],
  "assets": ["index.html", "app.js", "styles.css", "assets/video-001.jpg"]
}
```

`upstream_assets` lists every candidate file containing content copied or derived
from the frozen source-visible reference. It must be a subset of the complete
`assets` inventory. It does not authorize live source access.

## Deterministic routes

Static hosting uses SPA fallback. Application code must translate a deployment-prefixed browser path such as `/sites/cloudpost/inbox` to the logical contract route `/inbox`, and must translate contract routes back through the same prefix when navigating.
