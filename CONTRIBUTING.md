# Contributing

Keep changes scoped to one pipeline stage and include machine-readable evidence
or tests. Verifiers must remain read-only. Builder repair should be expressed as
a repair package and applied by the Builder in its isolated workspace.

Before opening a pull request:

```bash
python -m pytest
npm --prefix tools/codex-mirror-trace test
webanything doctor --json
python scripts/release_scan.py .
```

Do not commit run directories, captured private evidence, credentials, or
generated candidate databases.
