# Known limitations

This alpha release does not yet claim a fully autonomous, provider-independent
pipeline for arbitrary websites.

- The SDK trace currently combines exploration and construction in one Builder
  turn. A separate Explorer agent and discovery-to-contract compiler are defined
  by schema but not yet connected as independent executable stages.
- The Builder is WebHarbor/Flask oriented.
- Robots, WAF, authentication, network denial, and broken upstream resources may
  prevent source-grounded capture. WebAnything does not bypass access controls.
- The current E2B adapter validates gateway health, gallery presence, and site
  homepages. Full post-deploy interaction workflow replay is planned.
- `authenticated` exploration requires a user-authorized temporary session and
  additional private evidence handling; credentials are never release artifacts.
- Visual fidelity is evaluated at declared viewports, not every responsive size.
- URL-first replay discovery is deliberately bounded. It follows same-origin
  links and prioritizes explicit `--route` inputs, but it does not claim to
  enumerate an unbounded application, hidden role-only surfaces, or workflows
  that require CAPTCHA/risk-control bypass. A complete result means complete
  coverage of the frozen route/action scope.

These limitations must remain visible in papers, demos, and release notes.
