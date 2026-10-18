# Public provenance audit

Audit date: 2026-10-18

## Decision

The WebAnything release tree has no non-empty exact, high-similarity, or
medium-similarity match to the pinned public WebHarbor tree. The public branch
was rebuilt from that tree as new root history on 2026-10-17, excluding the
earlier runtime files and commits.

The repository snapshot names an internal source commit whose author history is
not present in this checkout, so the audit cannot independently verify the
rights holder from Git history alone. On 2026-10-18, the project owner
explicitly confirmed ownership or authorization covering the current
implementation and authorized release under MIT. The root `LICENSE` is now the
effective license for WebAnything-authored code.

## Fixed comparison inputs

| Input | Revision |
| --- | --- |
| WebAnything base `HEAD` | `0571decd5592d1a7ba7e6b93ea44f3fd975c5f7c` |
| WebAnything initial import | `90fb918ca1fc3ef5071fbecc2868e339217fb875` |
| Public WebHarbor source | `7269be89c93b72cf1d8596b4fde65ba1c7453c15` |
| Current public WebHarbor default branch, supplemental | `381dbd9882680806c81ed626d226f114c464a321` |

The public WebHarbor tree at the fixed revision contains 667 files and no root
`LICENSE`, `LICENSE.md`, `COPYING`, or `NOTICE` file.

## Current working-tree result

The audit covered 337 tracked and untracked repository files while excluding
its own generated JSON reports.

| Classification | Files | Meaning |
| --- | ---: | --- |
| Non-empty exact public match | 0 | No byte-identical non-empty file found |
| High public similarity, at least 0.80 | 0 | No high textual match found |
| Medium public similarity, at least 0.50 | 0 | No medium textual match found |
| Empty placeholder match | 5 | Empty `.gitkeep` or empty queue files; no expressive content |
| Legacy name only or unresolved | 109 | Contains `WebHarbor`, `WebSyn`, or `NexGarden`, but no material content match was detected |
| No material public overlap detected | 185 | Text comparison stayed below 0.50 |
| Binary with no exact match | 38 | Hash differs; semantic or visual derivation was not assessed |

The rewritten `Dockerfile` has 0.121252 similarity to the same-path WebHarbor
file. The three earlier runtime files are deleted and their replacement is
`src/webanything/runtime.py`.

The 109 naming hits are compatibility identifiers, historical documentation,
provenance records, prompts, and the still-named
`.agents/skills/build-webharbor-mirrors` skill. A name hit is not source-code
copying, but these names should be reviewed separately before presenting a
fully WebAnything-branded release.

As a supplemental drift check, all 16,288 files in the current public
WebHarbor default branch were included in an exact Git-blob comparison. Only
the same five empty placeholders matched. A textual comparison against its 64
root, agent, documentation, and script files found zero exact, high, or medium
matches. The large generated site corpus was not included in fuzzy text
matching, so this supplemental check does not replace the fixed-source audit.

## Pre-rewrite Git-history result

Before the clean-root rewrite, every one of the 39 reachable commits was
scanned against the same pinned WebHarbor tree. Every old commit contained the
same four material matches; no second set of materially similar files was
found.

| Historical file | Best same-path similarity | Current working tree |
| --- | ---: | --- |
| `control_server.py` | 0.941395 | Deleted; replaced by WebAnything runtime |
| `site_runner.py` | 0.938661 | Deleted; replaced by WebAnything runtime |
| `Dockerfile` | 0.766067 | Rewritten; current similarity 0.121252 |
| `websyn_start.sh` | 0.715309 | Deleted; startup now invokes the package runtime |

The earlier initial import was an ancestor of the previous `origin/main`.
Deleting the files in an ordinary follow-up commit would therefore have been
insufficient. The 2026-10-17 release replaced that branch with clean root
history. The adjacent history JSON is retained as the pre-rewrite audit record;
it does not describe the ancestry of the rewritten branch.

## Internal-source record

`provenance/source_snapshot.json` says the staging tree was created from
`public-release/NexGarden` at commit
`ac48c8c62377e4724f2fc00c7c5f441aac614103`, followed by allowlisted working
tree additions. That Git object is not available in this repository. The
initial WebAnything commit records `Codex <codex@local.invalid>` as author and
committer, which documents packaging activity rather than the authorship of
the imported implementation.

The engineering audit cannot convert this incomplete internal history into
independent proof of ownership. The release decision records the project
owner's explicit authorization. Stronger supporting evidence may additionally
include:

- the original repository history showing that the relevant contributors
  authored the imported implementation and agreed to MIT;
- a signed contributor or employer rights statement covering those files; or
- independently rewritten replacements with review notes that identify the
  specification used and exclude the old implementation from the new history.

See [`MIT_LICENSE_DECISION.md`](MIT_LICENSE_DECISION.md) for the effective
license decision and its scope.

## Third-party material

The task demonstrations remain governed by their own notices. The USWDS case
includes CC0/public-domain site terms and separate OFL/Apache font licenses.
The Mermaid case includes its own source and trademark notice. The root MIT
license does not replace those terms.

## Method and limitations

`scripts/audit_public_provenance.py` records SHA-256 hashes, exact matches,
same-path similarity, likely moved or renamed text candidates, legacy names,
the local revision, and the fixed upstream revision. Text similarity uses
token-shingle candidate selection followed by `difflib.SequenceMatcher`.

This method can find direct or lightly edited public overlap. It cannot prove
copyright ownership, detect every conceptual rewrite, infer authorship from a
Git committer field, compare opaque binary assets semantically, or search
private and unpublished source trees. The supplemental current-upstream check
uses exact hashes for the full tree and fuzzy matching for its 64 core files;
it does not fuzzily compare every generated site file.

Reproduce the two checked snapshots after checking out the pinned WebHarbor
tree locally:

```bash
python scripts/audit_public_provenance.py \
  --upstream-root /path/to/WebHarbor-at-7269be8

python scripts/audit_public_provenance.py \
  --upstream-root /path/to/WebHarbor-at-7269be8 \
  --local-ref 90fb918 \
  --output docs/licensing/public-provenance-history-audit.json

for revision in $(git rev-list --all); do
  python scripts/audit_public_provenance.py \
    --upstream-root /path/to/WebHarbor-at-7269be8 \
    --local-ref "$revision" \
    --output "/tmp/webanything-$revision-audit.json"
done
```

The detailed machine-readable results are in
`public-provenance-audit.json` and
`public-provenance-history-audit.json`; the latter records the pre-rewrite
initial import snapshot. The history loop was run before the branch rewrite.
