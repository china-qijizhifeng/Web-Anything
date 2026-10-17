# Runtime, state, login, and reset

The seed database is immutable authority. Open it only for initialization or verification. Runtime writes belong under `instance/` and must never alter `fixtures/`.

Reset is an atomic replacement:

1. close active SQLite connections;
2. copy the seed to a temporary file in `instance/`;
3. fsync the temporary file when supported;
4. replace the runtime database with `os.replace`;
5. reopen connections only after replacement;
6. redirect `/reset.html` to the declared initial route.

Two consecutive reset calls must leave a runtime database whose SHA-256 is byte-identical to the seed. Do not recreate tables or replay INSERT statements during reset because SQLite page layout may differ.

Source authentication is never a runtime input. Use fixed demo users stored in the seed. `explicit_login` begins logged out; `default_authenticated` begins as the declared demo user. Login, logout, and role switches mutate only local state.

For `session_partitioned`, persist an opaque random session id in a secure local cookie and include it in every mutable row key. The benchmark content and clock remain deterministic; the opaque id is isolation infrastructure and must not enter evaluator-visible state. For `isolated_instance`, reject more than one simultaneous task at the scheduler boundary.

The declared runtime entrypoint is also a deployment contract. Bind to
`os.environ.get("HOST", "127.0.0.1")` and
`int(os.environ.get("PORT", "8000"))`; do not hard-code the self-test port.
Before handoff, launch once with a different available port and verify the
declared health path there. E2B and the independent verifier both inject these
variables.
