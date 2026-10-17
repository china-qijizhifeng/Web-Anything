# MIT release draft — not an effective license

The project owner selected MIT as the intended license on 2026-10-17. This
document prepares that change locally. It does not grant rights, replace
`LICENSE-PENDING.md`, or authorize publication under MIT.

## Source-code overlap audit and replacement

[`docs/provenance.md`](../provenance.md) records
[`aiming-lab/WebHarbor` commit `7269be8`](https://github.com/aiming-lab/WebHarbor/tree/7269be89c93b72cf1d8596b4fde65ba1c7453c15)
as an input to this repository. The complete reproducible result is recorded in
[`PROVENANCE_AUDIT.md`](PROVENANCE_AUDIT.md) and the adjacent JSON reports. The
percentages below are normalized-text `difflib.SequenceMatcher` similarities,
useful for identifying overlap; they are not copyright conclusions.

| Removed or replaced file | Similarity to WebHarbor | Resolution prepared locally |
| --- | ---: | --- |
| `control_server.py` | 94.1% | Removed; batch control now lives in the new `src/webanything/runtime.py` implementation |
| `site_runner.py` | 93.9% | Removed; the new runtime serves a site directly |
| `Dockerfile` | 76.6% | Rewritten to install and run the WebAnything package; current similarity is 12.1% |
| `websyn_start.sh` | 71.5% | Removed; container startup now calls the package runtime directly |

At that upstream commit, WebHarbor had no root `LICENSE`, `COPYING`, or
`NOTICE` file. Its visible history for these files includes contributors
`raibows` and `xiongtao3`. The current WebAnything Git history records the
initial import as one packaging commit; that import is not evidence of
ownership of upstream material.

Builder previews, quality checks, and batch verification have been updated to
call the replacement runtime. The new reset integration test covers site
startup, health reporting, and seed restoration. The current tree has zero
non-empty exact, high-similarity, or medium-similarity public matches under the
documented thresholds. The public branch was rebuilt as clean root history so
the four older files are no longer in its ancestry. The imported code's
authorship chain remains incomplete, so the repository-wide rights review is
not finished.

## Release steps after rights are confirmed

1. Verify the remote default branch still points to the reviewed clean-root
   release and no old release refs remain.
2. Complete the imported-code authorship review described in
   `PROVENANCE_AUDIT.md` before claiming the whole repository is MIT-licensed.
3. Confirm the copyright holder name or names for the new root `LICENSE`.
4. Add the unmodified [MIT license text](https://choosealicense.com/licenses/mit/)
   as `LICENSE`; update both READMEs, `pyproject.toml`, and other references
   to `LICENSE-PENDING.md` in the same change.
5. Keep each third-party asset's existing terms and attribution. The USWDS
   font notices, Mermaid demo notice, and site-content terms are separate
   from the proposed license for WebAnything-authored code.
6. Review the complete diff, run the package and documentation checks, then
   publish the change only after the rights review is complete.

For reference, the proposed `LICENSE` file should use the standard MIT text
with a confirmed copyright line such as `Copyright (c) 2026 <rights holder>`.
Do not place that file at the repository root while the rights question is
open: a root `LICENSE` would present this combined tree as licensed.
