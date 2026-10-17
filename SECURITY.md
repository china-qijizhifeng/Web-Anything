# Security policy

Please report suspected credential exposure or unsafe artifact handling
privately to the maintainers. Do not open a public issue containing secrets.

WebAnything run directories are private by default. Never commit:

- API keys, cookies, authorization headers, or storage state;
- authenticated upstream screenshots or account data;
- SDK event streams before the release scan passes;
- runtime databases containing private or non-deterministic data;
- deployment archives or provider environment files.

The release gate must fail if a sensitive-file scan, candidate hash check, seed
reset check, or artifact binding check is missing or unsuccessful.
