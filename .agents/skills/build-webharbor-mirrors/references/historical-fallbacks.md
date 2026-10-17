# Historical source fallback

Use this reference when live intake stops on robots, 403/429, WAF, challenge,
authentication, or a blank JavaScript shell. A live denial is terminal for live
capture; historical discovery must not retry it with alternate user agents,
cookies, residential proxies, browser stealth, sibling hosts, or archives.

## Source ladder

1. Keep the rejected live `intake.json` and its response hashes.
2. Try one representative public route only when the canonical origin's robots
   file was available, permits the route, and the route uses the exact same
   scheme, hostname, and port.
3. Probe pinned exact-origin indexes:

   ```bash
   run.sh probe-historical-indexes \
     --output mirror_factory/evidence/generated-replacement/index-probe.json \
     [site ... | https://example.test/]
   ```

   The probe checks fixed revisions of `gbenson/webui-dom-snapshots` and
   `ronantakizawa/webui`. It accepts only exact-origin rows with desktop
   dimensions, source HTML/DOM, design code, geometry, and a matched image. The
   report stores response hashes and source hashes but not expiring image URLs or
   raw source bodies.
4. Exhaustively scan the same pinned providers when the filter service is
   unavailable or returns no candidate:

   ```bash
   run.sh scan-parquet-origins \
     --output mirror_factory/evidence/generated-replacement/parquet-origin-scan.json \
     [site ... | https://example.test/]
   ```

   The scanner reads the immutable repository tree, validates the complete shard
   set, then HTTP range-fetches only each Parquet footer and URL column. It records
   repository object IDs, LFS SHA256, Xet/transport ETag, file size, tree/footer/
   column hashes, decoded row counts, exact scheme/hostname/port matches, cache
   reuse, and future retry conditions. `pyarrow==24.0.0` decodes the bounded URL
   columns from sparse local files; image, HTML, CSS, JS, and bbox columns are not
   downloaded during triage. A hit is still only a row locator: fetch and validate
   its same-row HTML-first bundle through the acceptance gate before promotion.
5. Run `discover-historical` for pinned Mind2Web rows. Treat its website-label
   match as a proposal, never as origin proof. Reject a label-only match when the
   represented origin is different or cannot be proven.
6. Search pinned WebLINX chat indexes for a first `load(url=...)` event. Accept a
   result only when the matching demonstration includes the pre-interaction HTML,
   bbox JSON, desktop screenshot, replay URL, viewport, and zoom. Pin each file's
   SHA256 and retain only the selected state.
7. Query Common Crawl only by exact origin and fixed index/record coordinates:

   ```bash
   run.sh probe-common-crawl \
     --output mirror_factory/evidence/generated-replacement/common-crawl-probe.json \
     [site ... | https://example.test/]
   ```

   The bounded probe pins `CC-MAIN-2026-30`, validates it against the published
   collection catalog, queries exact canonical URL variants, and post-filters
   every row by scheme/hostname/port. It retains CDX response hashes and immutable
   WARC filename/offset/length/digest coordinates. A 502/503/504 or unavailable
   provider is evidence of an incomplete lookup, never a candidate. Before using
   a returned coordinate, require the HTML record and every material stylesheet,
   font, image, and matched desktop visual needed by that state. A WARC HTML shell
   without its page-specific CSS is not a mirror.

## Large archive triage

Avoid downloading multi-gigabyte archives before knowing their contents:

1. Pin the dataset revision and file object hash.
2. Read `Content-Length`, `Accept-Ranges`, and the linked object ETag.
3. Fetch the final 65,536 bytes, parse ZIP EOCD, then range-fetch the central
   directory.
4. Count HTML, CSS, JS, JSON, bbox, screenshot, and video entries before selecting
   any payload.
5. Fetch only the local-header plus compressed range for a candidate entry and
   verify CRC32, uncompressed size, and SHA256 after extraction.

The official Mind2Web `test.zip` uses password `mind2web` and prohibits online
redistribution of unzipped test files. Its test records contain website labels and
raw HTML but no canonical page URL. Use them only when independent exact-origin
evidence exists; a label alone cannot map `macys` to `macyswineshop.com`.

The public WebVoyager GPT-4V archive contains task JSON and screenshots but no
HTML, CSS, or JS entries. It is screenshot-only evidence and must not be used to
infer page structure.

## Acceptance gate

Advance a historical candidate only when all of these are true:

- Dataset/repository and full immutable revision are public and pinned.
- Exact represented origin is proven by a source URL field or equivalent signed
  replay metadata; mobile, sibling, apex/`www` substitutions are rejected.
- HTML/DOM supplies structure, copy, controls, links, classes/IDs, roles, and
  geometry before any screenshot is consulted.
- CSS or bounded computed styles are tied to the same captured state.
- A desktop screenshot is tied to the same row and used only for calibration and
  DOM-bounded visual crops.
- No authentication state, personal data, challenge page, block page, or hidden
  answer is retained.
- Capture, compiler, seed, audit, runtime/reset, fidelity, quality, and package gates all
  pass transactionally.

## Abandonment gate

Keep a site rejected when live capture is denied and all pinned searches produce
only screenshot-only, label-only, mobile/sibling-host, missing-CSS, block-page,
unverifiable, or unavailable source bytes. Record every provider, revision, query
hash/error, candidate rejection reason, and the condition that would allow a
future retry. Do not replace the site with a renamed template to increase counts.

Finalize the evidence mechanically after every source report exists:

```bash
run.sh finalize-abandonments \
  --output mirror_factory/evidence/generated-replacement/abandonment-report.json
```

The finalizer requires rejected live intake, zero hosted-index candidates, zero
exact-origin matches in the full pinned Parquet URL columns, zero eligible Common
Crawl candidates, and explicit exact-origin policy in all reports. Any failed gate
leaves the site `unresolved`; the command never changes the replacement queue.
