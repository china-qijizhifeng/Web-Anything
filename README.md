# WebAnything

[English](README.md) | [简体中文](README.zh-CN.md)

<p align="center">
  <img src="assets/webanything-concept-logo.png" alt="WebAnything concept logo" width="560">
</p>

WebAnything is a research codebase for constructing executable, resettable Web-agent environments from source-site evidence. This repository packages the construction pipeline and a frozen inventory of 244 historical website artifacts. The 244 website source trees and third-party media remain in their existing artifact store; they are not bundled here.

The Python package and command-line entry point are both named `webanything`.
New runs use `webanything` schema IDs, runtime names, and `WEBANYTHING_*` provider variables. Existing frozen run directories retain their original names and hashes; continue those runs with the revision that created them.

## Workflow overview

[![Conceptual WebAnything workflow from website URL through evidence gathering, reconstruction, independent verification, repair, and release](assets/webanything-workflow-concept.png)](assets/webanything-workflow-concept.png)

Click the diagram to view it at full resolution. It is a conceptual overview; see [architecture](docs/architecture.md) and [known limitations](docs/limitations.md) for the implemented pipeline and coverage boundaries.

## Quick start

WebAnything needs two different URLs: a **Responses-compatible model API URL**
for the Builder and Verifier, and the **source website URL** to reconstruct.
The model service must support the Responses API and the model ID you select.

Install the project and browser once from the repository root:

```bash
uv sync --locked --extra dev --extra browser --extra visual --extra builder
uv run python -m playwright install chromium
npm --prefix tools/codex-mirror-trace ci
```

Run setup once. It asks for your Responses API base URL (usually ending in
`/v1`), provider model ID, and API key. The key prompt hides what you type.
By default, setup installs a personal WebAnything skill for Codex:

```bash
uv run webanything setup
```

To install the same skill for both Codex and Claude Code, use
`uv run webanything setup --agent both`. Start a new agent chat and ask, for
example: **“Rebuild https://example.org so I can find the second example and
open it.”** For broad coverage, ask it to rebuild the site in **Full** mode.
The skill selects JIT when a task is given and runs the CLI; this is a local
agent skill, not an MCP server. Its installation references this checkout, so
rerun setup if you move it. The agent needs terminal access to this machine.

The key is saved outside the repository in `~/.config/webanything/provider.json`
by default, with owner-only permissions. Neither agent skill contains the key.
For CI or other automation, set `WEBANYTHING_API_URL`, `WEBANYTHING_API_KEY`,
and optionally `WEBANYTHING_MODEL` through your secret manager. Never put a
real key in a command, run directory, or committed `.env` file.

You can also pass the **source website URL** directly to the CLI:

```bash
uv run webanything create https://example.org/ --mode full
uv run webanything create https://example.org/ --mode jit --task "Find the second example and open it"
```

`create` uses the model selected during setup and `high` reasoning effort.
Without setup, its fallback model ID is `gpt-5.6-sol`; override it with
`--model your-model-id` and use `--reasoning-effort` if needed.
`full` explores discoverable public routes and controls and requires an
independent coverage check. `jit` builds and checks the complete interaction
path for the supplied task. Site IDs, requirements, and run directories are
derived automatically. On completion, `webanything preview <run-dir>` serves
the generated site at `http://127.0.0.1:8000/` for local inspection. The
command prints the run directory and authoritative status; a generated page
alone does not mean verification passed. See [the simple interface guide](docs/simple-interface.md)
for configuration paths, environment variables, and run behavior.

To preview one bundled example in a container:

```bash
docker build -t webanything .
docker run --rm -p 8000:8000 \
  -e WEBANYTHING_SITE=uswds-docs-demo webanything
```

The image starts only the selected site. Builder previews and batch checks use
the same lightweight runtime; reset and health control endpoints start only
when a verification job needs them.

## What is included

| Path | Purpose |
| --- | --- |
| `src/webanything/` | CLI, discovery, contracts, release, replay and synthetic modes |
| `tools/coupled-pipeline/` | Source-coupled Builder, independent Verifier, and repair loop |
| `tools/codex-mirror-trace/` | Captured evidence and SDK trace utilities |
| `pipeline-tools/interaction-verifier-v1/` | Candidate interaction checks |
| `.agents/skills/build-webharbor-mirrors/` | Builder instructions and supporting scripts |
| `scripts/` | Batch, deployment and inventory utilities |
| `data/sites244.json` | Frozen selected artifact paths, source URLs, status and `app.py` hashes |
| `sites/uswds-task-accordion/` | Runnable, task-focused USWDS Accordion reconstruction with font notices |
| `sites/uswds-docs-demo/` | Two-page USWDS reconstruction demo with documented verification limits |
| `sites/mermaid-live-task-svg/` | Runnable Mermaid editor task demo: edit three nodes and export SVG; see the [case study](docs/case-studies/mermaid-live-editor.md) |
| `tests/` | Unit and protocol checks |

The source was assembled from the historical `public-release/NexGarden` tree in the existing WebHarbor artifact store at commit `ac48c8c62377e4724f2fc00c7c5f441aac614103`, plus the explicitly listed working-tree changes in [`provenance/source_snapshot.json`](provenance/source_snapshot.json). A historical project-document snapshot, with its release-status note updated, is retained at [`docs/WEBANYTHING_UPSTREAM.md`](docs/WEBANYTHING_UPSTREAM.md).

## Reproduce the code and audit the inventory

Use Python 3.12, Node.js 18+, `uv`, and Chromium. From the repository root:

```bash
uv sync --locked --extra dev --extra browser --extra visual --extra builder
uv run python -m playwright install chromium
npm --prefix tools/codex-mirror-trace ci
uv run webanything doctor --json
uv run pytest tests pipeline-tools/interaction-verifier-v1/tests
npm --prefix tools/codex-mirror-trace test
uv run python scripts/verify_site_inventory.py
```

The last command validates the frozen 244-site manifest without downloading website content. To verify all selected site entrypoints against an existing artifact store:

```bash
uv run python scripts/verify_site_inventory.py \
  --webharbor-root /path/to/WebHarbor
```

`--webharbor-root` must contain both `sites/` and `generated/` as listed in the manifest. See [`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md) for how to regenerate the manifest, build a new site, and interpret the historical evidence.

## Evidence boundary

The inventory selects 244 unique site IDs from several historical batches. A directory and matching `app.py` hash establish artifact identity; HTTP 200 and a screenshot establish only startup or rendering observations. They do not establish a common functional acceptance standard, release-gate pass, or that all 244 sites were built by one identical pipeline version. The 2026-10-09 audit checked 113 sites more deeply and left 131 at the basic observation level.

## Copyright, licenses, and responsible use

The 244-site inventory is metadata, **not a bundle of 244 reconstructed websites**. This repository includes three limited demos under `sites/`, along with selected screenshots, fonts, and other example assets. It does not include the complete source trees or media for the 244 historical sites.

WebAnything-authored code is released under the [MIT License](LICENSE). The project owner confirmed authority to make this release on 2026-10-18. This repository-level license does not replace the separate terms, notices, or attribution requirements that apply to third-party material.

Third-party website content, screenshots, fonts, logos, names, and trademarks retain their respective owners' rights and applicable terms. The included demos have their own notices: [USWDS Accordion](sites/uswds-task-accordion/THIRD_PARTY_NOTICES.md), [Mermaid editor](sites/mermaid-live-task-svg/THIRD_PARTY_NOTICES.md), and the [two-page USWDS demo](sites/uswds-docs-demo/README.md). These examples are independent research reconstructions and do not imply affiliation with or endorsement by the source projects.

Use WebAnything only for sites and content you are authorized to inspect and reconstruct. Before collecting, sharing, hosting, or reusing a generated site, check the source site's terms and the rights in its content and assets. A public URL, a generated output, or a passing technical verification does not itself establish permission to redistribute source material.

## Case study: from query to working page

> **Task query:** 在 Mermaid Live Editor 中，把默认流程图改为「草稿 → 审核 → 发布」，确认预览显示三个节点，然后导出 SVG。

[![The reconstructed Mermaid editor showing the three-node diagram in its code pane and live preview](docs/case-studies/evidence/mermaid-live/step-03-edit.png)](docs/case-studies/mermaid-live-editor.md)

The reconstructed page lets a user edit the diagram, confirm the preview, and download an SVG through **Actions → SVG**. The [full case study](docs/case-studies/mermaid-live-editor.md) shows the browser steps, exported file, and verification evidence, including the original supervisor limitation.

## Reconstruct for a user task

Use `webanything create URL --mode jit --task "..."`. The task run records a
source-grounded sequence of actions, builds the routes and state needed to
complete it, and requires an independent browser replay of every step before
reporting completion. See [`docs/TASK_QUERY.md`](docs/TASK_QUERY.md) for the
evidence format and USWDS example. The older `coupled-pipeline` command remains
available for explicit site IDs, requirements files, and run directories.
