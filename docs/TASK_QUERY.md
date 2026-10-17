# URL + task query reconstruction

This route uses the existing coupled Explorer/Builder and independent Verifier.
The input is a public source URL plus one user task. The Builder observes the
source workflow and builds the routes and state needed to perform it locally.
The Verifier replays the ordered workflow and checks its terminal state.

```bash
webanything coupled-pipeline \
  https://designsystem.digital.gov/components/overview/ \
  --site uswds-task-accordion \
  --run-dir runs/uswds-task-accordion \
  --task-query 'Find the Accordion usage guidance and expand the second item in the second example' \
  --model gpt-5.6-sol --reasoning-effort high
```

Configure `WEBANYTHING_RESPONSES_BASE_URL` and
`WEBANYTHING_RESPONSES_API_KEY` in the process environment or supply an untracked
provider file with `--env-file`. `--requirements-file` is optional when a task
query is present. Use a new run directory for a different URL or query; a
checkpoint cannot silently change its task. `--initialize-only` writes the
task request and requirements without invoking the model.

The run directory contains `task-request.json` with the exact query and hash,
plus `requirements.md` with task-depth instructions. The Builder must write
`task-contract.json` with the source-observed route and action sequence. The
independent Verifier must write `task-coverage.json` with a passed assertion and
persisted evidence for every step and the final state. The controller checks
matching query hashes, ordered step IDs, evidence paths, and the task workflow
gate. Missing or invalid task evidence changes a claimed pass to a failure and
returns a repair package to the Builder.

For the USWDS example, the task path is: filter or find Accordion on the
[component overview](https://designsystem.digital.gov/components/overview/),
open its documentation, locate the second preview variant (Bordered), and open
its second item (Second Amendment). The terminal state is the visible second
panel with `aria-expanded=true`. The USWDS source page declares its original
work [public domain / CC0](https://github.com/uswds/uswds-site/blob/main/LICENSE.md),
with trademark and third-party exceptions.

The task gate establishes task-workflow coverage. Visual fidelity, resource,
reset, and candidate-integrity gates also apply to the source-observed task
path. Unrelated routes and controls are documented as scope limitations.
The earlier two-page USWDS demo did not pass its full visual gate; see
[`USWDS_DEMO.md`](USWDS_DEMO.md). Do not call that earlier run a verified release.

## Local task replay of the bundled example

Start the task-focused reconstruction with
`python sites/uswds-task-accordion/app.py`, then run
`node examples/uswds_task_replay.cjs http://127.0.0.1:5000 runs/uswds-task-browser`.
The replay script requires a Node Playwright installation. It performs the
same five source and local actions, saves filtered and terminal screenshots,
and writes `browser-replay.json`. This is a browser smoke of the bundled
reconstruction. It does not replace the coupled pipeline's independent final gate.

On 2026-10-15, both the live USWDS source and the task-focused reconstruction
passed all five replay steps. The terminal browser check confirmed that the
Bordered group's “Second Amendment” panel was visible and its button had
`aria-expanded=true`. The earlier `sites/uswds-docs-demo/` remains a separate
historical example and is not the task-focused candidate.

For a second task-driven example, see the [Mermaid Live Editor case study](case-studies/mermaid-live-editor.md). It traces editing a three-node flowchart through preview and SVG download, with the actual browser replay screenshots and task evidence.
