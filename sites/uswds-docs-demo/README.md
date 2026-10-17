# USWDS two-page reconstruction demo

This example was generated from the public [USWDS component overview](https://designsystem.digital.gov/components/overview/) and [accordion documentation](https://designsystem.digital.gov/components/accordion/) by the WebAnything coupled pipeline with `gpt-5.6-sol` and `reasoning_effort=high` on 2026-10-14. It is a local research demo and is not affiliated with the U.S. government.

The source website repository places its original work in the [public domain / CC0](https://github.com/uswds/uswds-site/blob/main/LICENSE.md). That declaration does not cover trademarks or third-party material. This example uses local CSS, system fonts, text content, and a SQLite fixture; it does not package source logos, seals, fonts, photos, or the original CSS bundle.

## Run

From the `webanything` repository root:

```bash
uv sync --locked --extra builder
uv run python sites/uswds-docs-demo/app.py
```

Open `http://127.0.0.1:5000/components/overview/` and `http://127.0.0.1:5000/components/accordion/`. The site creates `runtime.db` from the tracked `seed.db` on first use. `POST /_reset` restores the fixture.

## Result and limit

The independent verifier passed route/resource, interaction, reset, and candidate immutability checks. It failed the visual gate: overview scored `0.964885`, accordion scored `0.94467` against a `0.95` threshold at 1440×900. The accordion page also has less lower-page guidance content than the source. See `docs/USWDS_DEMO.md` for the run record. The committed example omits the generated candidate's vendored Python packages; use the repository's locked environment above.
