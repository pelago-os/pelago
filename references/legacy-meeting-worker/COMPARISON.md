# Outbound worker versus supervised execution

These are design trade-offs, not a benchmark or a claim that the legacy code meets current Pelago contracts.

## Legacy design preserved here

A worker initiates coordinator requests, so the worker machine need not accept inbound SSH. Local adapters can bridge multiple agent CLIs; session references help conversation continuity. Registration, route matching, lease IDs, attempt IDs and context generations show useful early control-plane concepts. Straightforward polling can be easier to understand and can suit networks permitting outbound traffic only.

Costs include a coordinator API and worker service to operate, polling latency, trust in server-side lease/idempotency enforcement, and runtime-specific assumptions. Execution blocks the polling loop. The presence of generation or completion fields does not by itself prove enforcement. Transport fallback improves connectivity but complicates consistent permissions and model provenance. No persistent local execution ledger or independently verified strong cancellation is demonstrated here.

## Later supervisor design (historical comparison)

The later design used a restricted SSH entry point and separate prepare/activate/renew/cancel/status operations, a durable SQLite journal, attempt binding, generation fencing and process identity/group checks. Those mechanisms make lifecycle evidence and restart/replay handling more explicit; unknown state must not be treated as confirmed cancellation. They also introduce more deployment prerequisites, runtime pins and version coupling, and require controlled inbound reachability.

The comparison comes from a historical source review, not fresh end-to-end tests of the newest Pelago release. It does not imply that all historical supervisor operations are public Pelago APIs, nor that the newer release supports this worker. Current supported features belong in the main product documentation.

## Limits shared by both approaches

Local process cleanup is not proof of remote provider cancellation or stopped billing. Same-user processes are not strong persona isolation. Receipts only establish what their trusted collectors actually observe. Every runnable system needs explicit credentials, authorization, bounded execution and platform-specific validation.

## Why preserve rather than integrate

Outbound pull connectivity and heterogeneous adapter examples remain useful design references. Keeping a sanitized snapshot lets interested contributors evaluate that alternative without adding another maintained execution backend to Pelago. Future demand can justify an independently scoped implementation; no compatibility or maintenance commitment is made here.
