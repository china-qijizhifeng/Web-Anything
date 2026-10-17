# USWDS reconstruction trial — 2026-10-14

## Scope and provenance

- Source routes: `https://designsystem.digital.gov/components/overview/` and `https://designsystem.digital.gov/components/accordion/`.
- Model: `gpt-5.6-sol`; reasoning effort: `high`. A tiny Responses API preflight returned this exact model before the run.
- Builder: `webanything coupled-pipeline`, site ID `uswds-docs-demo`, a 60-minute limit per cycle and three automatic cycles.
- Repository input: commit `5f4c730d52efee7ed41011d8ddbdbfb0638f0c7f`, plus the committed experiment requirements. The verification Python environment fix was committed separately as `5f1da008b1366b4d6b3afcda96e7b7c12bf48fb7` during the run.
- The official website's [license](https://github.com/uswds/uswds-site/blob/main/LICENSE.md) describes its original work as public domain / CC0, subject to the stated trademark and third-party exceptions. The example excludes copied seals, logos, photos, and bundled fonts.

## Measured result

The independent verifier froze candidate hash `5d3bed26e6d117ddcb2f89f0cb2c2f227c64fab3de50103fb345db2c8e2a197f` (206 files, including vendored Python dependencies) and confirmed the hash was unchanged after its audit. Its result was **failed** because the accordion route fell below the visual threshold.

| Check | Result |
| --- | --- |
| Overview route visual similarity | `0.964885` / `0.95` — passed |
| Accordion route visual similarity | `0.94467` / `0.95` — failed |
| Browser workflows | 22/22 passed |
| Accordion controls | 30/30 passed |
| Visible actionable local controls | 10/10 meaningful |
| Routes, local resources, external requests, console errors | passed |
| SQLite double reset and candidate immutability | passed |

The accordion page is visibly shorter than the source documentation: the verifier measured 3234 px of local main content versus 6038 px upstream. The missing lower-page guidance and its layout need further work before this trial qualifies as a complete reconstruction. The first independent verification attempt stopped at preflight because it used a system Python without Flask; the second attempt passed preflight and reached the full visual audit. Automatic retries then reached their three-cycle limit. This trial is **not a verified release**.

## Local artifacts

- Committed, dependency-light example: `sites/uswds-docs-demo/`.
- Experiment requirements: `examples/uswds-accordion-demo.md`.
- Ignored local evidence: `runs/uswds-20261014/`, including a tarball of the exact frozen candidate, source and local screenshots, and the independent verifier result. The example in `sites/` removes vendored packages and runtime state, so its file hash differs from the frozen verification candidate.

Re-run the source-grounded pipeline with the command in `docs/coupled-pipeline.md`, passing the above source URL, site ID, requirements file, model and effort. The private provider environment file is supplied at run time and is not part of the repository.
