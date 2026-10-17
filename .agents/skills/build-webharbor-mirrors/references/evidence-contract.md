# Evidence contract

Evidence lives under `mirror_factory/evidence/<site>/`. JSON is sorted and includes
the manifest SHA256, site ID, phase, generation time, status, and pinned source
revision when applicable.

| File | Minimum passing evidence |
| --- | --- |
| `intake.json` | Normalized/final URL, robots response hash, entry-page hash and metadata, authorization classification, bounded generated scope, exclusions, and no failures. |
| `upstream.json` | Authorization basis, bounded scope, robots responses, final URLs, status/media type/size/SHA256 for pages and assets, capture spec hash, no entry-page failure, and explicit optional navigation/query/asset failures for later fidelity assessment. A JavaScript-only shell may additionally contain a serialized desktop DOM tied to its source-page SHA256, the exact captured resource SHA256 set, and all blocked uncaptured/non-GET requests. |
| `reconstruction.json` | Capture evidence hash, replay compiler version, page/asset/alias/task counts, captured-DOM prerender count/tool, planned/captured/skipped GET search/filter counts, deterministic bundle path/size/SHA256, and explicit workflow gaps. |
| `import.json` | Repository, ref, revision, source path, destination, and site-tree SHA256. |
| `asset.json` | HF repository/revision/file, byte size, tar SHA256, and extracted seed SHA256. |
| `seed.json` | Python runtime, seed path/size/SHA256, and second-import byte identity. |
| `audit.json` | Required files, Python syntax, tasks, isolation, runtime data, SQLite integrity, and tree SHA256. |
| `runtime-local.json` | Supplemental local HTTP, dirty-state, control reset, health, and equal-hash evidence. It does not advance phase. |
| `runtime.json` | Image identity, selected port, HTTP checks, control health, reset response, and equal runtime/seed hashes. |
| `fidelity.json` | `webharbor-fidelity-v19` and fidelity contract version 3; mandatory, non-excludable local `/`; matched route/viewports and explicit upstream/local URLs; hash-verified captured-replay/local screenshot hashes; source mode; matched and runtime JavaScript modes; static baseline normalizations; bounded settle evidence; typed blocked uncaptured requests; visible broken-image, stylesheet, missing-placeholder, request-failure, HTTP-error, CSP, nonblank-retention, landmark, external-request, structural, and visual checks. A historical static reference explicitly marks upstream resource inspection not applicable while retaining local integrity checks. |
| `coverage.json` | Required only for full-page, full-site, canonical-homepage, "everything clickable", or pure-hosted preview claims. Canonical `/` status and local-root mapping; route inventory from visible same-origin links, navigation, CTAs, cards, forms, sitemap hints, and captured pages; control inventory at the declared desktop viewport; classified route/control outcomes; official supplemental surface inventory; implemented local click-through counts separate from departure/scope notices; representative-entry disclosure; unimplemented links/controls; and measured coverage/click-through thresholds. |
| `quality.json` | `webharbor-quality-verifier-v1` and quality contract version 1; current `fidelity.json` identity; hash-pinned `sites/<site>/mirror_contract.json`; zero screenshot-derived assets; SQLite record and asset hash checks; declared GET/POST checks plus local-only browser navigate/fill/select/click/expect results; SQLite proof for every declared state change; no blocking interaction gaps; explicit `post_workflow_reset` hash evidence; and final `full_fidelity` or disclosed `synthetic_grounded_data` grade. |
| `asset-package.json` | Deterministic local tar path/size/SHA256/member count and explicit publication status. |
| `review.json` | Reviewed task count, verifier paths, pass results, and reviewer attribution. |
| `automation.json` | Ordered stage status, timestamps, bounded outputs/errors, final candidate tier, and the remaining reviewer gate. |
| `result.json` | Compact terminal status, site/port/counts on success, or failed stage/class/resumability/error on rejection or failure, plus evidence paths. If the mirror includes runtime performance fixes such as asset caching, compression, or deliberate script disabling, record the exact optimization, before/after browser metrics, affected routes, and the post-fix evidence hashes. |

Evidence is immutable with respect to its inputs: when the manifest, source tree,
seed database, tasks, assets, or image changes, rerun the affected phase and replace
its evidence. A later phase must not conceal a stale or failed earlier phase.

A fidelity `status=passed` is current only when its tool is
`webharbor-fidelity-v19`, `fidelity_contract_version` is 3, its pinned spec and
emitted results both cover local `/`, neither the spec nor evidence excludes `/`,
its captured or historical source hashes still match, `require_complete_resources` and
`runtime_javascript_check` are true, `minimum_nonblank_retention >= 0.5`, every
route has stable resource-integrity evidence, and `failures` is empty. Queue
status tools must downgrade any older proof to `stale_fidelity`; a current v19
failure is `fidelity_failed`, not stale and not passed.

A quality `status=passed` is current only when the contract and fidelity evidence
hashes still match, design/data/interaction each pass, `failures=[]`, and the grade
is `full_fidelity` or `synthetic_grounded_data`. A current visual pass without
quality evidence is `stale_quality`, never a completed high-fidelity mirror.
`grade=rejected` remains inspection-only regardless of HTTP health or passing
subpages. Deployment handoff additionally requires a current real-browser entry
page audit; HTTP 200 alone is never sufficient.

A full-site completeness claim is current only when `coverage.json` exists, hashes
the canonical entry and current contract/fidelity/quality evidence, classifies every
visible same-origin route and desktop control in scope, separates official
supplemental surfaces from third-party externals, and records no unclassified scope
notices. A user-facing "everything clickable" or pure-hosted claim additionally
requires click-through metrics proving which official visible controls open
meaningful local pages/workflows; `__webharbor_external`, scope notices, protocol
handlers, policy-blocked downloads, and source no-ops are not implemented controls.
A representative local `/` mapping is partial evidence and cannot be promoted above
`representative_entry_mirror`.

For documentation surfaces, current completeness evidence must include sidebar and
table-of-contents link inventories plus browser click proofs for every captured
visible docs route. Direct HTTP checks for a few sampled docs pages do not prove
documentation interaction fidelity. If most visible docs sidebar entries remain
departure notices, classify the docs surface as partial even if the homepage,
pricing page, and one guide pass.

For Docusaurus, Next.js docs, and other SPA-backed documentation, HTTP 200 is only
transport evidence. The browser evidence must prove the page does not hydrate into
Not Found, a blank shell, an error boundary, missing chunks, CSP failures, or
external requests. If captured server-rendered HTML is meaningful but uncaptured
client runtime corrupts it, preserve the hash-verified SSR content, disable only
the broken runtime, and record the exception. Allow a narrow hash-verified local
script exception only when browser evidence proves zero external requests,
request failures, HTTP errors, and console/CSP errors.

For SSR-captured display pages whose local interaction is fully provided by HTML,
CSS, links, and server routes, nonessential framework hydration may be disabled as
a preview-performance repair only when the exception is route-scoped, hash-pinned,
applied equally to replay/contract expectations, and followed by current fidelity,
quality, coverage, docs/browser, and resource-audit evidence. This exception must
not be used for controls that require client JavaScript to satisfy the declared
workflow.

For source-grounded generated records, evidence must distinguish real captured
content from deterministic placeholders or hydrated fallbacks at the record level.
An empty or blocked upstream detail response may justify a local workflow/schema
mirror, but not a faithful detail-content mirror. The quality grade must remain
`synthetic_grounded_data`, and preview handoff must disclose which visible record
fields were generated or reused from fallback evidence.

Do not store credentials, API tokens, hidden task answers, scraped personal data, or
live production responses in evidence.
