# Getting started

## 1. Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[builder,browser,dev]'
python -m playwright install chromium
npm --prefix tools/codex-mirror-trace ci
webanything doctor
```

## 2. Configure the SDK provider

Export credentials only in the current process or use your own secret manager:

```bash
export WEBANYTHING_RESPONSES_API_KEY='...'
export WEBANYTHING_RESPONSES_BASE_URL='https://your-endpoint.example/v1'
```

Do not place credentials in run configuration, requirements briefs, Site
Contracts, or tracked files.

## 3. Build

```bash
webanything build https://example.com/ \
  --site example \
  --run-dir runs/example-r1 \
  --profile task-driven
```

The trace runner copies this repository into `runs/example-r1/workspace`, starts
the SDK Builder there, and records sanitized lifecycle evidence. Source files
outside that workspace are not Builder-writable.

### Recoverable concurrent workers

`webanything batch` accepts a JSON worker specification. Each site must have a
distinct run directory, status file, log, and Builder workspace. The supervisor
supports a bounded wall-clock watchdog and an optional `retry_command` used for
attempts after the first one:

```json
{
  "schema_version": 1,
  "root": ".",
  "max_concurrency": 3,
  "max_attempts": 3,
  "worker_timeout_seconds": 9000,
  "restart_delay_seconds": 10,
  "max_restart_delay_seconds": 180,
  "workers": [
    {
      "id": "example",
      "cwd": ".",
      "command": [
        ".venv/bin/webanything", "build", "https://example.com/",
        "--site", "example", "--run-dir", "runs/example-r1",
        "--profile", "task-driven", "--timeout-minutes", "120"
      ],
      "retry_command": [
        "scripts/run_codex_trace.sh", "continue", "runs/example-r1",
        "--requirements-file", "examples/timeout-recovery-brief.md",
        "--site", "example", "--timeout-minutes", "120", "--no-reflection"
      ],
      "status_file": "runs/example-r1/run.json",
      "log_file": "runs/batches/example-r1.log"
    }
  ]
}
```

Run it with:

```bash
webanything batch --spec batches/sites.json --max-concurrency 3
```

The status reader accepts `state`, `status`, `latest_execution_status`, or
`execution_status`. `timed_out` and `exhausted` are retryable by default;
`failed` remains terminal and fail-closed. A watchdog expiry terminates the
worker process group, records the attempt, applies exponential backoff, and
uses `retry_command` when configured. Without `retry_command`, the original
command is repeated.

For a Builder, point `retry_command` at the SDK continuation path so the
existing workspace is preserved instead of starting a fresh URL build. For a
Verifier, repeat the same verify command with `--resume`; only passed,
identity-matching checkpoints are reused. A Verifier terminal `failed` result
is not automatically retried because it normally requires the emitted repair
package to go back to the Builder.

`worker_timeout_seconds` is a supervisor wall-clock guard. Set it above the
Builder's `--timeout-minutes` allowance so the SDK runner normally has time to
seal its own `timed_out` evidence before the hard watchdog is needed. It can
also be overridden for all workers with `webanything batch
--worker-timeout-seconds N`.

## 4. Verify and repair

Start the candidate or provide an explicit launch configuration, then run
`webanything freeze`, followed by `webanything verify --batch-size N
--checkpoint-dir PATH --resume`. The verifier writes one atomic checkpoint per
ordered route/control batch. Resume reuses only passed checkpoints whose result
hash, candidate, contract, viewport, verifier version, configuration, assignment,
and plan identity still match; missing, corrupt, failed, timed-out, duplicate, or
stale batches cannot close the exhaustive merge.

If the terminal result fails, pass the emitted repair package to `webanything
repair`. Repair is the explicit write transition: it marks the candidate freeze
and downstream verification/release evidence invalid before the Builder can
change files. Freeze the repaired bytes again and rerun every affected verifier.
Do not manually edit a frozen candidate; mutation is rejected at verification
and release.

### Synthetic benchmark route

Use `benchmark_synthetic` when the output must be deterministic, static, locally
stateful, and independently task-verifiable. Content can be either `fictional`
or an explicit frozen `source_fidelity` reproduction:

```bash
webanything build https://seed.example/ \
  --site demo \
  --run-dir runs/demo-r1 \
  --build-mode benchmark_synthetic \
  --profile benchmark_synthetic \
  --synthetic-contract contracts/demo/synthetic_contract.json
```

Compile the contract before dispatch with `webanything synthetic-contract`.
When structural fidelity is required, first use the isolated Capture Worker to
create and freeze a source-visible reference plus its content-neutral comparison
artifact, and bind that manifest in the contract.
Freeze with matching `--build-mode benchmark_synthetic`, then run both
`webanything verify-synthetic` and `webanything verify-synthetic-visual` before
`webanything release-synthetic`. The functional Verifier opens fresh browser
contexts, reads raw localStorage/DOM/URL/network state, replays every task,
checks two resets and `/reset.html`, and rejects candidate mutation or external
requests. For fictional contracts the visual Verifier compares content-neutral
pixels; for `source_fidelity` it compares the rendered candidate directly with
the frozen source-visible screenshot. Both modes also measure DOM geometry,
component density, column proportions, and spacing against the same frozen
reference on every repair round. Those structure metrics are independent gates
for fictional mode and diagnostic contributors to the screenshot-dominant
composite in `source_fidelity` mode.

For a site that needs login, the user creates a Playwright storage-state file
once for the current login session. Seal it outside the repository with a
mode-0600 AES key, pass the encrypted profile only to the Capture Worker, and
delete the plaintext after a successful seal. Cookie expiry, CAPTCHA, risk
control, or a robots denial pauses only that site; these conditions are never
passed to the Builder as repair work. See
[Synthetic Visual Pipeline](synthetic-visual-pipeline.md) for the command flow,
state machine, hash gates, and concurrent supervisor format.

Because the frozen screenshot is source-visible, use a dedicated low-risk
capture account and assume anything visible in the viewport is readable by the
Builder. The screenshot is not copied into the released candidate.

## 5. Seal and deploy

Use `webanything seal` to rescan the trace and bind recorded artifacts, then use
`webanything release` with the frozen candidate and either explicit
`--verification` files or `--verification-dir`. Release is the authoritative,
read-only aggregation step. It fails closed unless current hash-bound Fidelity,
Quality, exhaustive Interaction Closure, resource/privacy, candidate
immutability, and byte-identical runtime-reset gates each appear exactly once.
It writes `release-gate-result.json` first and writes the v2 frozen manifest only
after that gate passes.

Deployment requires an explicit E2B template, environment-provided E2B
credentials, and the hash-bound v2 manifest plus its unchanged authoritative gate.
Set `runtime` to `static` in the gallery manifest for synthetic candidates;
the deployer uses the packaged static server and the same supervised restart
loop used for Flask sites. The current preview deployer checks gateway health and homepages; full workflow
replay from the public gateway remains a release-preview limitation.
