# Capture and release boundaries

The authenticated capture worker may use a user-authorized source browser while the authorization remains valid. It must remove source cookies, storage, tokens, personal fields, and raw authenticated DOM before freezing the Builder bundle. CAPTCHA, auth expiry, robots denial, and source blocking are capture states; Builder must not bypass or repair them.

The capture manifest lists every allowed DOM, CSS, font, icon, image, and media file with SHA-256. It must declare:

- `external_javascript_execution: false`;
- `source_auth_persisted: false`;
- stable source route to local route mappings;
- every copied file and its content kind.

Builder may consume only these frozen files. It must record capture-derived candidate files in `replay-provenance.json`. The original capture bundle and visual reference are non-release evidence and stay outside `sites/<site_id>/`; only local runtime assets required by the candidate are released.

Release requires current hashes for the contract, capture bundle, visual reference, SQLite seed, and candidate tree. Any Builder repair or seed change invalidates both functional and visual verification. E2B deployment happens only from the hash-bound release manifest.
