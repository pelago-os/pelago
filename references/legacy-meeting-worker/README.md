# Legacy outbound meeting worker (historical reference)

**Research material, not a supported runnable component. This directory is not connected to current Pelago meetings. Do not deploy it or point it at real accounts.**

This snapshot preserves an earlier design: a worker registers with a coordinator, sends heartbeats, leases a task, dispatches a local adapter and submits a result. It illustrates outbound-only connectivity and heterogeneous local agent integrations. The coordinator implementation is NOT included. Its historical API paths are not current Pelago endpoints.

## Contents

- `worker.py`, `client.py`, `protocol.py`: polling, historical HTTP client and data structures.
- `adapters/`: fake output plus historical Hermes, OpenClaw and Antigravity examples.
- `config.example/registry.json`: fictional routes, not working account configuration.
- `COMPARISON.md`: architectural trade-offs and evidence boundaries.
- `PROVENANCE.md`: source revision, transformations and licensing gate.

There is intentionally no installer, service template, credential example, dependency change, supported-platform promise or quick-start command. The original private history, reviews, runtime state and authentication are excluded. The snapshot is outside application source, tests and installation packaging; maintainers must confirm this remains true when merging.

## Important hazards retained for study

The historical Hermes subprocess command includes `--yolo` and obsolete CLI flags. `deny_interactive` is a protocol label, **not** a tool sandbox. Gateway failures may fall back to subprocess execution. The OpenClaw adapter assumes a fixed historical `host` profile. Antigravity accesses a local conversation cache and creates runtime directories. TLS verification can be disabled by an old development flag. Error text and diagnostic printing may disclose output or endpoint information. Never use real credentials with these examples.

Task execution is synchronous. An HTTP/subprocess timeout does not establish that all descendants or remote provider generation/billing stopped. The worker has no demonstrated durable local execution journal or continuous cancellation controller; lease expiry, authenticated coordinator behavior and replay protection are not established by this source alone. Routing checks are not strong identity isolation. Do not expose its old API without a separate security design.

Any future runnable integration needs a separate project, current platform/API validation, permission hardening and cancellation/replay tests. This archive does not silently add that scope.

## Validation

Before publication, maintainers ran syntax parsing, JSON validation, inventory hashing and secret/privacy scans on this snapshot. No live adapter, account, model, service or production coordinator was invoked. Original tests are not included or represented as passing. Syntax/scans are not proof of correctness, safety or present-day compatibility.

Licensing and provenance are described in `PROVENANCE.md`.
