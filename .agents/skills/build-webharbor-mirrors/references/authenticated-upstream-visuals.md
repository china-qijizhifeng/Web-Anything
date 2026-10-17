# Authenticated Upstream Visuals

Use this reference only after the user explicitly authorizes upstream login-state
screenshots. This expands visual evidence; it does not authorize upstream writes,
credential disclosure, private-data reproduction, or publication of the evidence.

## Boundary

- Keep storage state mode `0600` under an ignored credential directory such as
  `.tempser/`. Never print, copy, archive, commit, deploy, or give it to a Builder.
- Capture only an allowlisted HTTPS origin and bounded routes/viewports. Use
  read-only navigation and explicitly declared UI-state clicks such as opening an
  account menu. Never submit an upstream form.
- Treat screenshots as personalized private evidence even when the user permits a
  Builder to inspect them. Keep them under `.tempser`, mode `0600`, mark them
  `publication_allowed: false`, and never serve them as mirror assets.
- Stage only the screenshot/visual contract into an isolated Builder workspace for
  the duration of the pass. Remove the staged copy after the Builder finishes.
- The Builder must reproduce layout, chrome, spacing, typography, responsive
  behavior, and generic product states. It must map every identity to the declared
  virtual persona and must not reproduce real names, avatars, emails, organizations,
  notifications, private repositories, or personalized copy.

## Capture

Declare anonymous and authenticated states in one spec:

```json
{
  "schema_version": 1,
  "allowed_origin": "https://github.com",
  "routes": [
    {
      "id": "login-desktop",
      "url": "https://github.com/login",
      "state": "anonymous",
      "viewport": {"width": 1440, "height": 1000}
    },
    {
      "id": "home-auth-desktop",
      "url": "https://github.com/",
      "state": "authenticated",
      "viewport": {"width": 1440, "height": 1000},
      "authenticated_markers": ["[aria-label*='account' i]", "a[href^='/settings']"]
    }
  ]
}
```

Run only after recording explicit authorization:

```bash
.agents/skills/build-webharbor-mirrors/scripts/run.sh capture-authenticated-visual \
  --spec .tempser/<site>-authenticated-visual-spec.json \
  --storage-state .tempser/<site>-storage-state.json \
  --output .tempser/<site>-authenticated-visual \
  --allow-personalized-screenshots
```

The output contains viewport screenshots and `visual-contract.json` with hashes,
landmark geometry, control counts, and resource diagnostics. It never contains the
storage-state path or contents.

## Visual Builder Pass

Copy the accepted mirror into a new isolated workspace. Stage the screenshots and
contract under that workspace's evidence directory, then run one Builder with these
constraints:

1. Inspect the staged screenshots directly.
2. Edit only templates, CSS, local visual assets, and minimal presentation helpers.
3. Preserve the accepted auth/session/route/SQLite implementation unless a visual
   state cannot render without a narrowly justified adapter change.
4. Use the virtual identity from `auth_profile.json`; never transcribe personalized
   upstream content.
5. Run the authenticated workflow/reset suite after the visual pass.

Remove the staged screenshots after the Builder terminates. Retain only private
quarantine originals, screenshot hashes, non-personal structural metrics, local
acceptance screenshots, and the Builder result.

## Acceptance And E2B

Capture local desktop and mobile screenshots for each contracted state. Require
HTTP 2xx, no local external requests, no failed/error resources, no console/page
errors, no horizontal overflow, and manual overlap review. Re-run default login,
logout, protected redirect, local login, mutation, canonical reflection, SQLite
persistence, and byte-identical reset.

For a one-site human preview, use a manifest-backed deployment with
`scripts/deploy_preview_gallery_e2b.py`. Package only `sites/<site>`; the deployer
excludes `instance/` and includes `instance_seed/`. Do not include the Builder
evidence directory, storage state, or upstream screenshots. Check gateway health,
gallery presence, proxied home, scoped deep routes, expected authenticated marker,
and favicon from the public HTTPS origin. Record the sandbox ID, lifecycle, URL,
and checks in the build result.
