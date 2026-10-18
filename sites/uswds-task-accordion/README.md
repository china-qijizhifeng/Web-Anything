# USWDS Accordion task reconstruction

This independent local demo reconstructs the public [USWDS components overview](https://designsystem.digital.gov/components/overview/) and the path to its [Accordion documentation](https://designsystem.digital.gov/components/accordion/). It is not a U.S. government website.

Task query: **Find the Accordion usage guidance and expand the second item in the second example.**

From the repository root, after `uv sync --locked --extra builder`:

```bash
uv run python sites/uswds-task-accordion/app.py
```

Open `http://127.0.0.1:5000/components/overview/`, filter for Accordion, open its page, select “Using the accordion component,” select “Bordered,” then expand “Second Amendment.” The final panel should be visible and its button should have `aria-expanded="true"`. `POST /_reset` restores the runtime database to `seed.db`.

The local task path uses no external runtime requests. Links outside this path may lead to the official documentation. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the bundled font licenses and source provenance. WebAnything-authored code uses the repository [MIT License](../../LICENSE); bundled third-party material keeps the terms listed in the notice.
