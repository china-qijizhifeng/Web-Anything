# Target manifest schema

`mirror_factory/targets.jsonl` contains one JSON object per candidate. IDs and
upstream URLs are unique across the file.

Required top-level fields:

| Field | Contract |
| --- | --- |
| `id` | Lowercase letters, digits, and underscores; final directory name. |
| `display_name` | Human-readable site name. |
| `upstream_url` | Canonical HTTPS site represented by the mirror. |
| `purpose` | Short description of the site's domain and workflows. |
| `source` | Code acquisition contract. |
| `asset` | Seed/image/cache acquisition contract. |
| `initial_stage` | `discovered` or `sourced`. |
| `task_count` | Expected number of agent-facing task definitions. |

Optional `capture` records a live-site reconstruction input. It does not turn a
greenfield implementation into Git-sourced code:

```json
{"spec":"mirror_factory/upstream_specs/site_id.json","evidence":"mirror_factory/evidence/site_id/upstream.json","tool":"webharbor-upstream-capture-v15"}
```

Optional `historical_source` records a pinned benchmark observation used when
it is explicitly separated from current live-site capture evidence:

```json
{"evidence":"mirror_factory/evidence/site_id/historical-source.json","tool":"webharbor-historical-benchmark-capture-v1","dataset":"owner/dataset","revision":"40-char-sha","config":"default","split":"train","website":"site_id","row_index":42,"publication":"not_authorized"}
```

This source never overrides or conceals a robots/WAF rejection in `upstream.json`,
never claims current production drift, and cannot authorize publication.

Provider-specific historical descriptors are fixed provenance, not moving lookup
instructions. `weblinx` records a demonstration and the exact HTML, bbox, and
screenshot dataset paths with a SHA256 for each file. `webui_dom_snapshot` records
the same-origin representative `capture_url`, config/split/row, license, canonical
DOMSnapshot SHA256, stable row-metadata SHA256, and screenshot SHA256:

```json
{
  "evidence": "mirror_factory/evidence/site_id/historical-source.json",
  "tool": "webharbor-historical-benchmark-capture-v5",
  "provider": "webui_dom_snapshot",
  "dataset": "gbenson/webui-dom-snapshots",
  "revision": "40-char-sha",
  "config": "default",
  "split": "train",
  "row_index": 42,
  "capture_url": "https://example.test/public/page",
  "license": "CC0-1.0",
  "dom_snapshot_sha256": "64-char-sha256",
  "row_metadata_sha256": "64-char-sha256",
  "screenshot_sha256": "64-char-sha256",
  "publication": "not_authorized"
}
```

The descriptor's `upstream_url` remains the manifest's canonical URL. A distinct
`capture_url` must be same-origin and public. Historical HTML/DOM, classes,
semantics, text, controls, inline SVG, and geometry are the primary design source;
the paired screenshot is calibration evidence, not page structure.

Source variants:

```json
{"kind":"git","repo":"https://github.com/owner/repo.git","ref":"refs/heads/branch","revision":"40-char-sha","path":"sites/site_id"}
{"kind":"greenfield"}
{"kind":"missing_code"}
```

`git` means code was observed and pinned. `greenfield` means no implementation was
found and the site is available to build. `missing_code` means another artifact or
historical claim exists, but usable source code is absent.

Asset variants:

```json
{"kind":"hf_tar","repo":"owner/dataset","revision":"40-char-sha","filename":"site_id.tar.gz","sha256":"64-char-sha256"}
{"kind":"generated"}
{"kind":"missing"}
```

`generated` requires producing a deterministic seed and a new tarball. `missing`
means a previously expected asset cannot currently be resolved and must not be
treated as sourced.
