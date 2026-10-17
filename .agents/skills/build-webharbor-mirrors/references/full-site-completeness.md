# Full-site completeness contract

Use this reference when the user asks for a full-page, full-site, canonical-homepage,
or "everything clickable" mirror. This contract is stricter than a bounded
selected-route replay. It prevents a narrow passing subset from being described as
a complete website mirror.

## Completion levels

- `selected_route_mirror`: only the declared routes and workflows are complete.
- `representative_entry_mirror`: local `/` maps an exact-origin public route because
  canonical `/` is source-blocked. This is partial and must never be called a full
  homepage or full-site mirror.
- `major_surface_mirror`: canonical `/` and the public primary navigation surfaces
  are mirrored with route/control coverage evidence. This is the default target for
  a "full official website" request.
- `full_public_surface_mirror`: every reachable public same-origin route inside the
  declared crawl policy is either mirrored, policy-blocked, or explicitly abandoned
  with evidence.

Do not use `full_fidelity` by itself to imply full-site completeness. Quality and
fidelity grades apply only to the declared scope unless this reference's coverage
evidence passes.

Do not use route/control classification by itself to imply a usable click-through
experience. A local departure notice, scope notice, unsupported page, or protocol
handler proves that a destination was classified; it does not prove that the
visible control is implemented. For "everything clickable", pure-hosted, or
user-preview requests, evidence must separately measure meaningful local
click-through.

## Required evidence

Write `mirror_factory/evidence/<site>/coverage.json` before claiming
`major_surface_mirror` or `full_public_surface_mirror`.

Minimum fields:

- `canonical_entry`: final canonical `/` URL, status, source SHA256, and whether it
  is actually rendered at local `/`.
- `route_inventory`: every same-origin URL discovered from canonical `/`, header,
  footer, nav menus, visible CTAs, cards, article/listing links, forms with GET
  actions, sitemap hints, and captured pages. Include source page, selector or
  extraction basis, visible text, normalized URL, and classification.
- `route_classification_counts`: counts for `mirrored`, `external`, `policy_blocked`,
  `auth_or_account`, `unsupported_backend`, `duplicate_or_alias`, and `abandoned`.
- `control_inventory`: every visible desktop control at the declared viewport,
  grouped by menu, disclosure, tab, carousel, search, filter, form, file input,
  media, copy/share, and primary action. Include selectors, source page hashes,
  implementation status, and workflow proof IDs.
- `coverage_thresholds`: requested route/control thresholds and the measured
  mirrored or honestly-classified percentages.
- `clickthrough_thresholds`: requested and measured percentages for visible
  official-surface controls that open a meaningful local page or local workflow.
  Count departure notices, limitation pages, `mailto:`, denied downloads, and
  source no-ops separately.
- `official_surface_inventory`: visible cross-origin destinations that are clearly
  controlled by the same site owner, such as official docs, platform, jobs,
  download, status, policy, or support hosts. Include host, source evidence,
  local route/workflow if implemented, and blocker if not.
- `representative_entry`: null unless a representative exact-origin entry is used;
  if present, include the blocker evidence for canonical `/` and mark the site no
  higher than `representative_entry_mirror`.
- `unimplemented_links`: routes that remain clickable only to local limitation
  pages, with exact reasons.
- `unimplemented_controls`: controls that remain static or unsupported, with exact
  reasons.

## Route inventory rules

Start from canonical `/`. Expand only public, same-origin GET destinations inside
the declared depth and page budget. Preserve the canonical hostname boundary:
sibling hosts such as `docs.`, `api-docs.`, `help.`, `talent.`, or third-party ATS
sites are supplemental sources, not automatic same-origin routes.

When the user asks for pure hosted review or "everything clickable", split
cross-origin destinations into:

- `official_supplemental_surface`: clearly same-owner public hosts, such as docs,
  platform, jobs, downloads, status, policy, support, or official app pages. These
  may be promoted into local namespaces only after their own robots/source checks
  and fidelity/workflow evidence. They do not upgrade same-origin full-site
  coverage.
- `third_party_external`: social networks, source-code forges, government
  registries, app stores, mail clients, payment providers, maps, analytics, and
  unrelated vendors. These normally remain departure notices and do not count as
  implemented local click-through.

For each visible internal link, keep one of these outcomes:

- `mirrored`: local route returns the captured or reconstructed destination.
- `duplicate_or_alias`: local route maps to a mirrored canonical route and evidence
  records both URLs.
- `external`: destination is cross-origin and opens a local departure notice.
- `policy_blocked`: robots, WAF, auth, challenge, or rights-basis evidence blocks
  capture.
- `auth_or_account`: route needs login, account state, or private data.
- `unsupported_backend`: public page exists but its useful result requires a live
  backend not reproduced locally.
- `abandoned`: all authorized source ladders were tried and recorded.

For repetitive editorial archives, `bounded_archive_exclusion` is also valid only
under the declared contract in [bounded editorial coverage](bounded-editorial-coverage.md).
It is neither mirrored nor abandoned. Keep it visible in evidence, but exclude it
from the scoped click-through denominator and SDK repair queue after the declared
category index and representative-detail quota pass. Never use it for primary
navigation, current product/docs/API surfaces, job listings, transactional records,
or unique pages.

A local scope notice is acceptable only for classified destinations. A site with
many unclassified scope notices is not complete.

For a user-facing preview, a site with many official-surface controls that only
lead to local departure/scope notices is not click-through complete, even when no
route is unclassified.

For a user-facing preview, visible speed regressions are also incomplete until
triaged. Measure the entry route in a browser, not only with `curl`; record request
count, decoded bytes, load milestones, console failures, and slow resources. Fix
safe runtime bottlenecks such as SQLite asset reads, missing compression, and
nonessential framework hydration on SSR display routes, then rerun the same
coverage and workflow checks. Do not trade interaction fidelity for speed.

For documentation or knowledge-base surfaces, inventory visible sidebar, table of
contents, previous/next, tab, language, version, and in-page anchor controls as
first-class click targets. Do not treat three representative documentation routes
as a usable docs mirror when a visible sidebar still sends most entries to a
departure notice. Either capture and serve each public GET documentation route
inside the declared sidebar inventory, or record the uncaptured item as an
unimplemented control/link with an exact blocker. A quality workflow that checks
only a small docs sample does not prove sidebar-level interaction fidelity.

For Docusaurus or SPA documentation, verify the rendered browser state, not just
the response body. A route that returns HTTP 200 but hydrates to Not Found, a blank
shell, an error boundary, missing JS chunks, or external requests is incomplete.
When the hash-verified server-rendered HTML is the faithful source and the
captured client runtime is unavailable or corrupting, keep the SSR content visible,
disable only the broken runtime, disclose the exception, and prove every visible
sidebar click in a real browser. If one visible docs link is an intentional
cross-site repository or ecosystem link, record it as a classified departure
notice and exclude it from implemented-local click-through counts.

## Control inventory rules

Use a real desktop browser at the declared viewport to inventory visible controls.
Include controls that appear after opening header menus, disclosure panels, tabs,
accordions, carousels, or search overlays. A control is complete only when a
local-only browser workflow proves the expected DOM state or SQLite mutation.

Use these implementation statuses consistently:

- `implemented_local_navigation`: opens a meaningful local mirrored/reconstructed
  page.
- `implemented_local_workflow`: performs a local workflow, with SQLite proof when
  state changes.
- `classified_departure_notice`: opens a local external/scope notice only; this is
  not implemented local behavior.
- `unsupported_policy_blocked`, `unsupported_auth`, `unsupported_protocol`,
  `unsupported_source_noop`, or `unsupported_backend`: visible but not locally
  implemented; record exact evidence.

For forms and search:

- Prefer captured GET semantics and local SQLite-backed result sets.
- Never issue upstream POST.
- Localize POST behavior only when source-pinned interaction recovery proves field
  names, validation, success/failure semantics, visible result text, SQLite state,
  and byte-identical reset.
- For job boards, ATS pages, product listings, or other record-detail pages, do
  not promote source-grounded placeholder titles/descriptions as real data. If a
  public shell or schema is captured but the detail envelope is empty, blocked, or
  only source-derived, grade the site as `synthetic_grounded_data`, label the local
  detail page visibly, and describe it as a workflow/schema mirror rather than a
  faithful content mirror. Keep trying authorized public GET/detail endpoints and
  captured hydrated DOM before accepting that downgrade.

## Representative entry restrictions

Representative root mapping is a fallback, not a success upgrade. If canonical `/`
cannot be faithfully captured, keep the mirror partial unless the user explicitly
accepts a representative surface. The final report must state:

- which public route is rendered at local `/`;
- why canonical `/` failed;
- which historical or alternate authorized sources were tried;
- why the result is not a full homepage or full-site mirror.

## DeepSeek full-page iteration notes

For DeepSeek, do not repeat the previous `deepseek_sdk_trace_v4` narrow scope as a
full-site claim. That run is a selected-route mirror with three official routes and
a supplemental Mokahr workflow. A full-page iteration must begin with a fresh
canonical `/` inventory, expand the visible official same-origin routes, and
classify sibling/supplemental surfaces separately. Treat `api-docs.deepseek.com`,
`download.deepseek.com`, `talent.deepseek.com`, and Mokahr as supplemental origins
with their own robots and scope evidence unless the user explicitly asks to include
them as separate surfaces.

DeepSeek also exposed a regression class: promoting only the four `www.deepseek.com`
routes while rewriting most official CTAs to `__webharbor_external` produces a
technically classified but poor local experience. For future DeepSeek-like repairs,
preserve the canonical homepage fidelity, then promote safe official supplemental
surfaces such as docs, pricing, policies, download/status limitation pages, and
the previously source-pinned recruitment workflow. Do not count GitHub, social,
MIIT/MPS, or `mailto:` controls as official local click-through.

For DeepSeek API docs specifically, the `api-docs.deepseek.com` sidebar is part of
the user-visible docs surface. Capture or honestly block visible entries such as
token usage, rate limits, error codes, agent integrations, thinking mode,
multi-round chat, chat prefix completion, FIM completion, JSON output, tool calls,
KV cache, Anthropic API, create-chat-completion, news, FAQ, and updates before
claiming docs interaction fidelity. Localize each captured sidebar href and prove
browser click-through from the sidebar, not just direct HTTP on `/docs/`,
`/docs/pricing/`, and `/docs/responses-api/`.
