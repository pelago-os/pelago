# Meetings release gates

This checklist records requirements, not a release approval. A test count alone
does not establish live-provider, browser, Linux or clean-install acceptance.
Record an immutable commit and evidence location for each completed gate.

| Gate | Required evidence | Current development status |
|---|---|---|
| Public lineage | Public base, unchanged migrations 1–4, additive migration 5, no private history | Passed for v0.2.0: single commit on v0.1.0, release scan clean |
| Upgrade/rollback | Existing v0.1 data preserved, repeat migration safe, restore backup with old code | Passed in the v0.2.0 clean release rehearsal |
| Default-off | No model calls or scheduler with meetings disabled | Automated coverage |
| Mobile flow | 390px, login/identity switch, IME, create/interject/pause/resume/summary/export | Independent browser review passed at 390px |
| Execution safety | Model/session evidence, tool rejection, isolated state, bounded process recovery | Simulated CLI passed; live Hermes run passed (see Hermes) |
| Claude | Exact standalone version/layout, dedicated login, managed policy audit, actual served/tool evidence | Experimental; no real version certified |
| Hermes | Independent runtime/wrapper, no credential copy, import resolution, hashing benchmark, actual receipts | Experimental. Live mock/Hermes meetings passed on macOS arm64 with Hermes 0.21.5 and the NVIDIA free route. Linux, other routes and mixed Claude/Hermes acceptance remain pending. |
| Mixed live meeting | Two real channels, discussion, summary, pause and restart evidence | Not re-run for v0.2.0. Scope accepted by the maintainer: mixed Claude Code, Codex and Hermes crews have run across devices in the private deployment this project grew out of |
| Packaging | Clean archive install, frontend assets, no private paths/data/credentials, licenses | Passed in the v0.2.0 clean release rehearsal |
| Platform claims | Actual macOS and Linux evidence per supported combination | macOS acceptance; Linux has test-suite runs only, no live acceptance claimed |
| Publication | Candidate SHA and limitations reviewed, human final approval | Approved for v0.2.0 with the limits above |

Keep experimental badges, AI-generated summary warnings and original transcripts.
Local process exit is not proof of remote cancellation or stopped billing. Do not
turn unresolved gates into PASS to meet a date. If scope must change, explicitly
record and approve the revised promise before publication.

Run release tests as a non-root user. The runtime gate intentionally rejects root.
