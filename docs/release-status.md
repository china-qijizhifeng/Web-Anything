# Public release status

Snapshot date: 2026-08-24

This is a historical status snapshot. The current package and CLI are named `webanything`; see the repository README for the current interface.

## Integrated

- installable `nexgarden` Python CLI;
- run profiles and public JSON schemas;
- deterministic discovery-package to Site-Contract compiler;
- isolated Codex SDK capture and continuation runner with secret redaction;
- WebHarbor Builder Skill with a 127-line progressive-disclosure entrypoint;
- standalone read-only Interaction Verifier;
- generic multi-site batch declaration validator and batch supervisor;
- Builder repair-package continuation command;
- release gate with accepted-evidence checks, private-path cleanup boundary,
  byte-identical seed/runtime reset, candidate fingerprint, and frozen manifest;
- fresh E2B preview deployment adapter;
- release scanner, packaging metadata, tests, and GitHub Actions workflow.

## Verified in the consolidation environment

- Skill structural validation: passed;
- Python CLI/schema/release tests: 11 passed;
- Codex SDK trace tests: 37 passed;
- Interaction Verifier suite: reached 100% with no failure markers after the
  correct Playwright browser directory was supplied;
- Python compilation: passed;
- release scan: passed with zero findings;
- `nexgarden doctor`: passed on the target Linux host.

## Still required before public push

- project-owner decision and authority confirmation for the repository license;
- a truly independent Explorer executable rather than the current combined SDK
  explore/build turn;
- automatic discovery-package emission wired into the Contract compiler;
- complete post-deploy workflow replay rather than gateway/homepage checks;
- one clean end-to-end build from a new URL using only this staging tree;
- GitHub repository URL, branch policy, release name, and maintainer metadata.

The staging tree must not be advertised as a fully autonomous arbitrary-site
release until these items are closed or clearly labeled as alpha limitations.
