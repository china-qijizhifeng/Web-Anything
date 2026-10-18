# MIT license decision

Decision date: 2026-10-18

The project owner explicitly confirmed that they or their organization owns
the current WebAnything implementation, or has authorization from the relevant
rights holders, and authorized its release under the MIT License. The root
[`LICENSE`](../../LICENSE) is therefore the effective license for
WebAnything-authored code.

## Engineering provenance review

The reproducible comparison described in
[`PROVENANCE_AUDIT.md`](PROVENANCE_AUDIT.md) found no non-empty exact, high, or
medium similarity match between the release tree and the pinned public
WebHarbor tree. Earlier materially similar runtime files were removed or
rewritten, and the public branch was rebuilt from clean root history.

The checkout does not contain the complete author history of the internal
source snapshot. The owner's authorization resolves the release-governance
decision; it does not change that technical limitation or turn the similarity
audit into proof of copyright ownership.

## Scope

The MIT License covers WebAnything-authored software and documentation.
Third-party website content, screenshots, fonts, logos, names, trademarks, and
other assets retain their own terms. In particular:

- the USWDS examples include separate public-domain, OFL, and Apache notices;
- the Mermaid example retains its source and trademark notice;
- the 244-site inventory is metadata and does not bundle the corresponding
  historical site source trees or media.

Users remain responsible for checking source-site terms and content rights
before collecting, hosting, sharing, or redistributing a generated site.
