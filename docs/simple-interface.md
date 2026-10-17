# Simple Full/JIT interface

Install from a Git checkout with Python 3.11+, Node.js/npm, and Chromium:

```bash
python -m pip install -e '.[builder,browser,visual]'
python -m playwright install chromium
npm --prefix tools/codex-mirror-trace ci
```

Set up a Responses API connection and install the personal agent skill once:

```bash
webanything setup
```

Setup prompts for the API base URL, model ID, and a hidden API key. It installs
the skill for Codex by default; `--agent claude` targets Claude Code, and
`--agent both` targets both. The skill points at this checkout's Python
environment, so rerun setup after moving the checkout. Start a new agent chat
and ask: “Rebuild https://example.org so I can find and open the second
example.” The agent chooses JIT for a task, Full for explicit broad coverage,
and uses the commands below. Agents need local terminal access. The skill
contains no API key. For non-interactive setup, pass `--api-url`, `--model`,
and `--api-key-env` (the name of an environment variable containing the key).

The older `webanything configure --api-url URL` command remains available when
only the provider connection should be saved.

Use a public website URL with one of two modes:

```bash
webanything create https://example.org/ --mode full
webanything create https://example.org/ --mode jit --task 'Find a guide and open its second example'
```

Full covers the publicly discoverable, same-origin routes and first-order
controls from the entry URL. The Builder must record exclusions and the
independent Verifier must fail on missing in-scope coverage. JIT requires a
task query and covers the source-observed route and interaction sequence needed
to complete it. Both modes require the existing reset, resource, visual, and
candidate-integrity gates. Authentication, access restrictions, and unbounded
route families can stop or limit a run; `full` is not a promise to discover
hidden or infinite content.

The output appears under `runs/<derived-site-id>/`. Repeating the same URL,
mode, and task selects the same checkpoint and resumes the pending Builder or
Verifier stage after an interruption. A different task selects a separate run.
The command prints `complete` only after the independent Verifier's
strict result; other states identify incomplete, failed, or blocked work.
Inspect a generated candidate locally with:

```bash
webanything preview runs/<derived-site-id>
```

The preview URL is `http://127.0.0.1:8000/` by default. It is a local preview,
not a deployment or public release.

For automation, set both `WEBANYTHING_API_URL` and `WEBANYTHING_API_KEY` in the
process environment instead of saving a config file; set `WEBANYTHING_MODEL`
if you do not pass `--model`. Never put the key in a command-line argument or a
run directory. The configuration location can be changed with
`WEBANYTHING_CONFIG` or `XDG_CONFIG_HOME`. Advanced options such as model,
reasoning effort, run directory, timeout, and retry budget are in
`webanything create --help`; the lower-level `coupled-pipeline` command remains
available.
