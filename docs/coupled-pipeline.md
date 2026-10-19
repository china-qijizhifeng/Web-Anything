# Source-coupled Explorer/Builder route

The source-coupled route preserves WebAnything's earlier high-fidelity production
method as an explicit compatibility mode. One SDK Builder owns an isolated
browser and workspace, explores the official source while building, checks the
local result, and returns to the source when concrete visual or interaction
evidence is missing. A separate SDK context then performs read-only final
verification against the frozen candidate.

This route is useful when a pre-frozen generic capture does not contain enough
information for a deep, multi-route site. It is intentionally opt-in: it costs
more browser/model time and the live source can still pause the run for login,
WAF, CAPTCHA, consent, or regional access.

```text
URL + site requirements + optional authorized browser state
                         |
                         v
isolated git workspace -> coupled Explorer/Builder
                         |
                         v
passed Builder report -> independent read-only Verifier
                         |                     |
                         | failed              | passed
                         v                     v
repair package -> same Builder checkpoint    complete
```

## Install

Install the normal WebAnything dependencies, including the Codex SDK package:

```bash
python -m pip install -e '.[builder,browser,visual,dev]'
npm --prefix tools/codex-mirror-trace ci
```

Export a Responses-compatible provider directly or keep it in an untracked
environment file:

```dotenv
WEBANYTHING_RESPONSES_API_KEY=your-provider-key
WEBANYTHING_RESPONSES_BASE_URL=https://your-provider.example/v1
```

## Run from a new URL

Write a small requirements file describing the expected route families and
workflows. Then start the complete bounded loop:

```bash
webanything coupled-pipeline https://www.example.com/ \
  --site example \
  --run-dir runs/example-coupled-r1 \
  --requirements-file examples/coupled-requirements.md \
  --env-file /private/path/webanything-provider.env \
  --model gpt-5.6-sol \
  --reasoning-effort xhigh
```

The command clones the current WebAnything revision into
`<run-dir>/workspace` once. Later invocations preserve that workspace and
resume the existing candidate/evidence instead of recreating it.

For a source page that needs a user-authorized browser session, pass its
private Playwright storage-state path:

```bash
webanything coupled-pipeline https://www.example.com/account \
  --site example \
  --run-dir runs/example-coupled-r1 \
  --requirements-file examples/coupled-requirements.md \
  --storage-state /private/path/example-storage-state.json \
  --env-file /private/path/webanything-provider.env
```

The path is injected at runtime. Its contents are not opened by the launcher,
copied into the workspace, written to the run manifest, or passed through the
prompt. The Builder and Verifier may only give the path to Playwright. Real
account writes, purchases, posts, likes, follows, CAPTCHA/MFA bypass, and login
submission remain prohibited. Released authentication and mutations must use
fictional deterministic local state.

## Completion and recovery

The supervisor defaults to 12 cycles and a 90-minute limit for each Builder or
Verifier turn. It preserves valid work across timeouts. Builder and Verifier
SDK thread IDs and session history are kept separately under the private
`<run-dir>/.session/` directory. A retry first resumes the matching SDK thread.
If that thread cannot be opened and no new event has arrived, the runner starts a
fresh thread from the durable candidate, evidence, supervisor status, and repair
handoff. Once an event has arrived, retries remain under supervisor control;
Verifier retries within one supervisor run also reuse the same evidence
directory. Changing the source,
task, model, or frozen candidate invalidates the matching Verifier checkpoint.
Before accepting a resumed result, the Verifier must write a fresh report and
finish a completed SDK turn with an unchanged candidate. An incomplete Verifier
infrastructure turn is resumed without modifying the candidate. A completed
failed Verifier must write a machine-readable repair package, which is copied
into the workspace and returned to the same Builder. `source_blocked` and
`auth_required` pause only that site for user action.

Before model work, the supervisor sends a small HTTP/SSE Responses probe to
the configured provider. This catches HTTP 426, 503, and basic stream failures
early. The SDK configuration disables WebSocket transport for the provider.
The Verifier also checks task-contract structure and runs the deterministic
double reset before freezing the candidate. The model still performs the
independent browser replay and visual checks. Provider credentials and SDK
sessions stay in the private run directory and are excluded from the archive.
Set `WEBANYTHING_PROVIDER_PREFLIGHT=0` only for an offline or mocked run.

The authoritative status is `<run-dir>/auto-supervisor-status.json`:

- `builder_running` or `verifier_running`: active work;
- `complete`: independent Verifier passed and candidate hash was unchanged;
- `source_blocked` or `auth_required`: source access needs user action;
- `blocked_retry_limit`: the bounded automatic repair budget was exhausted.
- `provider_unavailable`: the provider transport probe failed before model work.

Process liveness, HTTP 200, screenshots, or a Builder self-test do not count as
completion. Release and E2B deployment remain separate existing WebAnything
gates and should run only after `complete`.

## Runtime options

The command accepts `--timeout-minutes`, `--max-cycles`, `--model`, and
`--reasoning-effort`. The shell runner also recognizes the corresponding
environment variables:

```text
WEBANYTHING_COUPLED_TIMEOUT_MINUTES
WEBANYTHING_COUPLED_MAX_CYCLES
WEBANYTHING_COUPLED_MODEL
WEBANYTHING_COUPLED_REASONING_EFFORT
```

Different run directories can be scheduled concurrently with `webanything
batch`. A per-run file lock prevents two Builders from modifying the same
candidate at once.

## E2B high-concurrency backend

`e2b-coupled-batch` moves complete site pipelines off the controller host. It
does not replace the high-fidelity method with a staged capture/build route:
each site gets one isolated E2B sandbox and runs the same coupled
Explorer/Builder, independent read-only Verifier, and bounded repair loop.

Create a batch spec using `schemas/e2b-coupled-batch-v1.schema.json` (see
`examples/e2b-coupled-batch.json`) and run:

```bash
webanything e2b-coupled-batch \
  --spec /private/jobs/e2b-coupled-batch.json \
  --env-file /path/to/private/provider.env \
  --state-dir /path/to/WebHarbor/generated/e2b-coupled-state
```

The batch limit controls concurrently active sandboxes, not concurrent writers
inside one site. Each worker must have a unique `run_dir`; duplicate writers
are rejected before an E2B call. The controller status is written atomically to
`<state-dir>/batch-summary.json`, with per-site status and logs under
`<state-dir>/workers/`.

The default `bootstrap: true` mode installs Node/npm when absent, then installs
WebAnything, the Codex SDK and Chromium inside each sandbox. It works with the
existing Python E2B preview template but has substantial startup cost. For
sustained batches, build a template with
these immutable paths and set `bootstrap: false`:

```text
/opt/nexgarden-runtime/venv/bin/python
/opt/nexgarden-runtime/node_modules/@openai/codex-sdk/dist/index.js
```

The controller uploads the current Git worktree without `.git`, `.venv`,
`node_modules`, ignored files, `.env`, PEM files, or keys. Existing run
checkpoints are uploaded before execution and downloaded afterward. The private
`.session` directory and Playwright storage state are never included in a
checkpoint or result archive. A supplied storage state is placed only in the
site sandbox, then the sandbox is destroyed unless
`keep_failed_sandboxes: true` is explicitly selected for diagnosis.

An E2B interruption or controller retry can continue from the downloaded
`run_dir`; it creates a new sandbox and restores that checkpoint. Because the
private `.session` directory is not exported, cross-sandbox continuation uses
the candidate and evidence checkpoint in a fresh SDK thread. A completed
site still means the existing independent Verifier returned `complete`, not
that the E2B command merely exited or returned HTTP 200. Deployment remains a
separate release-gated operation.
